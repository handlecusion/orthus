"use client";

import {
  Clock,
  Loader2,
  MessageSquare,
  Pencil,
  Plus,
  RotateCcw,
  Send,
  Square,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  API_BASE,
  type AgentChatMessage,
  type AgentChatSession,
  type AgentDelegateRequest,
  type AgentDelegateTarget,
  type AgentDelegateTargetList,
  type AgentFolderList,
  type AgentWorkItem,
  type AskScope,
  type AssistantResult,
  type RoutedAnswer,
  type SubAnswer,
  type UserInputField,
  type UserInputRequest,
  type WikiAnswer,
  type WikiLinkRef,
} from "@/lib/api";
import {
  CopyMessageButton,
  FRAGMENT_DEEPLINK_RE,
  MessageCitationChips,
  MessageMarkdown,
} from "@/components/agent/message-markdown";
import {
  Banner,
  Card,
  Chip,
  type ChipTone,
  inputClass,
  inputStyle,
  ModeBadge,
  PageHeader,
  Toolbar,
  ToolbarButton,
} from "@/components/ui";
import {
  DELEGATE_PLACEHOLDER,
  DELEGATE_STATE_LABEL,
  DELEGATE_STATE_MESSAGE,
  DELEGATE_STATE_TONE,
  DelegateControls,
  DelegateToggle,
  type AgentRunner,
} from "@/components/agent/delegate-controls";
import {
  filterMentionTargets,
  findMentionFragment,
  MENTION_LISTBOX_ID,
  MentionAutocomplete,
  mentionOptionId,
  parseLeadingMention,
  type MentionFragment,
} from "@/components/agent/mention-autocomplete";
import { useCompactAgentWorkShell } from "@/lib/use-compact-shell";
import {
  readCachedResource,
  removeCachedResource,
  writeCachedResource,
} from "@/lib/use-cached-resource";

// The dispatch-ack prefix a delegated agent_task message starts with while it
// runs (DELEGATE_STATE_LABEL.auto_execute == "실행 시작"). Used to detect a
// running delegation for the live "답변 생성 중" indicator.
const DISPATCH_ACK_PREFIX = `[${DELEGATE_STATE_LABEL.auto_execute ?? "실행 시작"}]`;
const INPUT_REQUEST_PREFIX = "[입력 필요]";

// Terminal result labels that should read as "something stopped" (warn-toned),
// matching the server's _WORK_RESULT_LABELS in orthus/api/routes/agent_work.py.
function isWarnResult(text: string): boolean {
  return text.startsWith("[실패]") || text.startsWith("[거부]");
}

function latestInputRequestWorkId(messages: AgentChatMessage[]): string | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message.role !== "agent" || !message.work_id) continue;
    if (message.text.startsWith(INPUT_REQUEST_PREFIX)) return message.work_id;
    return null;
  }
  return null;
}

// HITL: the most recent still-waiting user_input_request message (structured turn
// -ending question). Mirrors the backend latest_waiting_input_request semantics —
// only the latest waiting question is answerable.
function latestWaitingQuestion(messages: AgentChatMessage[]): AgentChatMessage | null {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (
      message.kind === "user_input_request" &&
      (message.payload as { status?: string } | null | undefined)?.status === "waiting"
    ) {
      return message;
    }
  }
  return null;
}

// Build a local chat message row from a server-persisted UserInputRequest so the
// question bubble renders immediately (the next getAgentChat refetch is the SoR).
function questionMessageFromUir(uir: UserInputRequest): AgentChatMessage {
  return {
    message_id: uir.message_id ?? `hitl-${uir.work_id ?? "q"}-${Date.now()}`,
    role: "agent",
    text: uir.question,
    work_id: uir.work_id ?? null,
    kind: "user_input_request",
    payload: uir as unknown as Record<string, unknown>,
    created_at: new Date().toISOString(),
  };
}

// Strip the `@` folder-picker affordance a user may type into the cwd field:
// "@~/Code" -> "~/Code". The backend strips defensively too
// (orthus/agentwork/service.py::_normalize_agent_task_cwd); cleaning here keeps
// the dispatched path correct and the payload tidy. `~` is left for the daemon.
function normalizeCwd(raw: string): string | null {
  let cleaned = raw.trim();
  if (cleaned.startsWith("@")) cleaned = cleaned.slice(1).trimStart();
  return cleaned || null;
}

// Cap the inline agentic /ask live-stream buffer (chars) so a chatty loop can't
// grow the "생성 중" bubble without bound; oldest text rolls off.
const ASK_LIVE_CAP = 8000;

// Tier i 진행 버블 상태: phase 프레임(검색/근거/작성 단계 라벨) + decompose 하위질문
// 카운터 + agentic 라이브 미리보기 + 최종 답변 morph 본문. `done`은 최종 메시지가
// 스레드에 붙는 것과 같은 렌더에서 진행 버블을 제거하기 위한 플래그다(버블 삭제 후
// 재생성으로 인한 깜빡임 제거). 프레임 유실/순서 뒤섞임과 무관하게 최종 POST JSON이
// SoR이므로, 이 상태는 표시 전용이다.
interface AskProgress {
  stage: string | null;
  evidenceCount: number | null;
  fanoutTotal: number | null;
  subDone: number;
  liveText: string;
  finalText: string | null;
  done: boolean;
}
const IDLE_PROGRESS: AskProgress = {
  stage: null,
  evidenceCount: null,
  fanoutTotal: null,
  subDone: 0,
  liveText: "",
  finalText: null,
  done: false,
};

// 진행 버블 단계 라벨. decompose 신호(fanout total/sub_answer_ready)가 phase 단계보다
// 우선한다(fan-out 중 leaf는 phase 프레임을 내지 않으므로 충돌 없음).
function progressLabel(p: AskProgress): string {
  if (p.fanoutTotal != null || p.subDone > 0) {
    const total = p.fanoutTotal != null ? String(p.fanoutTotal) : "?";
    return `하위 질문 ${p.subDone}/${total} 완료`;
  }
  if (p.stage === "searching") return "검색 중…";
  if (p.stage === "grounded") {
    return p.evidenceCount != null ? `근거 ${p.evidenceCount}개 확보` : "근거 확보";
  }
  if (p.stage === "answering") return "답변 작성 중…";
  return "답변 생성 중…";
}

// SWR 캐시 key — 세션 목록 + 세션별 메시지 스레드(재방문 즉시 페인트 seed).
const CHATS_CACHE_KEY = "agent:chats";
function chatCacheKey(sessionId: string): string {
  return `agent:chat:${sessionId}`;
}

// 현재 세션 스레드가 아직 준비되지 않았을 때 파생 messages로 쓰는 고정 빈 배열
// (매 렌더 새 배열이면 downstream useMemo가 계속 재계산된다).
const NO_MESSAGES: AgentChatMessage[] = [];

// CHAT 스레드는 "어느 세션의 메시지인지"를 메시지와 원자적으로 묶어 저장한다.
// currentSessionId만 먼저 바뀌고 스레드가 아직 이전 세션 것인 로딩 창에서, 이전
// 세션의 메시지가 새 세션의 캐시 key로 write-back되는 오염을 구조적으로 막는다.
interface ChatThread {
  sessionId: string | null;
  messages: AgentChatMessage[];
}

// S4 command-split: fragment states that no longer change on their own — the batch
// poll drops them so a converged chip strip stops hitting the network. Mirrors the
// backend review-terminal semantics: `request_more_data` (a held command-split
// fragment — the feature's own primary output) and `draft_for_review` (an email
// draft) never advance without an explicit operator decision, so leaving them out
// made held chips poll every 5s forever. Approving one happens on the /agent-work
// detail surface, so a slightly stale chat chip is acceptable.
const FRAGMENT_TERMINAL_STATES = new Set<string>([
  "resolved",
  "dismissed",
  "rejected",
  "request_more_data",
  "draft_for_review",
]);

// Chip labels/tones for the lifecycle states the shared DELEGATE_STATE_* maps (which
// only cover the four gate outcomes) don't carry. Badge SoR is still `state`.
const FRAGMENT_STATE_LABEL: Record<string, string> = {
  pending: "접수됨",
  classified: "분류됨",
  resolved: "완료",
  dismissed: "종료",
  rejected: "거부됨",
};
const FRAGMENT_STATE_TONE: Record<string, ChipTone> = {
  pending: "neutral",
  classified: "neutral",
  resolved: "pass",
  dismissed: "muted",
  rejected: "fail",
};

export default function AgentWorkPage() {
  const searchParams = useSearchParams();
  const wikiSlugFilter = searchParams.get("wiki_slug")?.trim() || null;
  const scopeParam = searchParams.get("scope")?.trim();
  const scopeFilter: "personal" | "company" | null =
    scopeParam === "personal" || scopeParam === "company" ? scopeParam : null;
  const compactShell = useCompactAgentWorkShell();

  const [chatDraft, setChatDraft] = useState("");
  const [chatting, setChatting] = useState(false);
  // Tier i 진행 버블: phase 단계 라벨 + decompose 카운터 + agentic 라이브 미리보기 +
  // 최종 답변 morph 본문(AskProgress 주석 참조). IDLE이면 스트리밍 아님.
  const [askProgress, setAskProgress] = useState<AskProgress>(IDLE_PROGRESS);
  const [error, setError] = useState<string | null>(null);
  // 중단(Stop): orchestrate 턴이 진행 중이면 제출 버튼이 중단 버튼으로 바뀐다.
  // 클라이언트 분리만 수행한다 — fetch abort + SSE close. 서버측 오케스트레이션
  // 취소는 시도하지 않는다(서버는 계속 진행할 수 있고, 이 턴의 결과는 클라이언트
  // 에서 폐기된다). stoppable은 중단 가능한 orchestrate 턴에만 true(위임/HITL
  // 경로는 기존 disabled 버튼 유지), aborted는 진행 버블을 "생성이 중단되었습니다"
  // 상태로 고정하는 플래그다(어시스턴트 메시지로 대체하지 않음).
  const abortRef = useRef<AbortController | null>(null);
  const liveEsRef = useRef<EventSource | null>(null);
  const [stoppable, setStoppable] = useState(false);
  const [aborted, setAborted] = useState(false);

  // DB-backed conversation sessions. HISTORY = sessions list; CHAT = the
  // current session's message thread. No sessionStorage — everything persists.
  // 재방문 시에는 SWR 캐시를 seed로 즉시 페인트하고(스피너 없음) 서버 응답으로
  // 조용히 갱신한다 — 로컬 append/폴링이 잦아 훅 대신 read/write 헬퍼를 쓴다.
  const [sessions, setSessions] = useState<AgentChatSession[]>(
    () => readCachedResource<AgentChatSession[]>(CHATS_CACHE_KEY) ?? [],
  );
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(
    () => readCachedResource<AgentChatSession[]>(CHATS_CACHE_KEY)?.[0]?.session_id ?? null,
  );
  // 늦게 도착한 세션 상세가 다른 세션으로 전환된 화면을 덮지 않도록, 현재 세션 id를
  // ref로도 들고 있는다(await 이후 최신 커밋 값 비교용).
  const currentSessionRef = useRef<string | null>(currentSessionId);
  useEffect(() => {
    currentSessionRef.current = currentSessionId;
  }, [currentSessionId]);
  const [thread, setThread] = useState<ChatThread>(() => {
    const first =
      readCachedResource<AgentChatSession[]>(CHATS_CACHE_KEY)?.[0]?.session_id ?? null;
    return {
      sessionId: first,
      messages: first ? (readCachedResource<AgentChatMessage[]>(chatCacheKey(first)) ?? []) : [],
    };
  });
  // 화면에는 현재 세션의 스레드만 그린다 — 세션 전환 직후(새 스레드 도착 전)에는
  // 빈 목록/스피너가 보이고, 이전 세션 메시지가 새 세션 화면에 남지 않는다.
  const messages =
    thread.sessionId === currentSessionId ? thread.messages : NO_MESSAGES;
  const [sessionsLoading, setSessionsLoading] = useState(
    () => readCachedResource<AgentChatSession[]>(CHATS_CACHE_KEY) == null,
  );
  const [messagesLoading, setMessagesLoading] = useState(false);

  // 로컬 state가 SoR — 바뀔 때마다 캐시에 write-back해 다음 방문의 seed로 쓴다.
  useEffect(() => {
    if (sessions.length) writeCachedResource(CHATS_CACHE_KEY, sessions);
  }, [sessions]);
  // 스레드 캐시는 메시지가 실제로 속한 세션(thread.sessionId) key로만 write-back한다
  // — currentSessionId key를 쓰면 세션 전환 로딩 창에서 A 스레드가 B key로 샌다.
  useEffect(() => {
    if (thread.sessionId && thread.messages.length) {
      writeCachedResource(chatCacheKey(thread.sessionId), thread.messages);
    }
  }, [thread]);

  // sid 세션의 스레드에 메시지를 붙인다. await 사이에 다른 세션으로 전환됐으면
  // (thread가 이미 교체됨) 조용히 버려 다른 세션 스레드/캐시 오염을 막는다 —
  // 메시지 자체는 서버에 이미 저장돼 있어 해당 세션 재방문 시 로드된다.
  function appendThreadMessage(sid: string, message: AgentChatMessage) {
    setThread((current) =>
      current.sessionId === sid
        ? { sessionId: sid, messages: [...current.messages, message] }
        : current,
    );
  }

  // Slice C: work_ids whose terminal result we've already appended (or that we've
  // decided to stop polling). Keeps a polled work_id from being re-polled forever.
  const [resolvedWorkIds, setResolvedWorkIds] = useState<Set<string>>(new Set());

  // S4 command-split: the queued command fragments of a decomposed answer, keyed by
  // the appended agent message_id. Chat messages persist as plain text, so this
  // in-memory map carries the structured SubAnswer chip data for the current
  // session's decomposed turns. Cleared on session switch.
  const [decomposedFragments, setDecomposedFragments] = useState<
    Record<string, SubAnswer[]>
  >({});
  // S4 command-split: live badge state per fragment work_id, refreshed by the batch
  // GET /agent-work?ids= poll. Overrides the initial (queue-time) `state` on the chip.
  const [fragmentLive, setFragmentLive] = useState<Record<string, AgentWorkItem>>({});

  // 인용 칩: 라이브 턴의 RoutedAnswer에서 뽑은 wiki_links/sources를 appended agent
  // message_id에 매핑하는 in-memory 상태. 채팅 메시지는 평문 텍스트로만 영속되므로
  // 히스토리 재로드/세션 전환 시 인용 복원(영속)은 이번 범위 밖이다 — 후속에서
  // 서버 message payload에 영속해야 리로드에도 남는다.
  const [messageCitations, setMessageCitations] = useState<Record<string, WikiLinkRef[]>>({});

  // Delegation controls — friendly UI alternative to the `위임:` text grammar.
  // When 위임 is on, the send action delegates the typed instruction to a local
  // agent (self or a teammate's enrolled collector node) through the
  // deterministic policy gate.
  const [delegate, setDelegate] = useState(false);
  const [runner, setRunner] = useState<AgentRunner>("codex");
  const [cwd, setCwd] = useState("");

  // Primary picker source: enrolled teammates (self first) + their collector
  // nodes. null = not yet fetched; targetsError or supported=false drops to the
  // raw-email fallback. selectedDeviceId pins the run to one node (null = any).
  const [targets, setTargets] = useState<AgentDelegateTargetList | null>(null);
  const [targetsError, setTargetsError] = useState(false);
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null);

  // Fallback path only (targets unsupported / fetch failed): raw assignee email
  // + the caller's own reported folders.
  const [assignee, setAssignee] = useState("");
  const [folders, setFolders] = useState<AgentFolderList | null>(null);

  const useFallback = targetsError || targets?.supported === false;

  // Self-delegation (fallback path) runs on the caller's own daemon; only then is
  // the `@` folder picker meaningful (a teammate's local folders are never exposed
  // there — their daemon resolves cwd from its own ORTHUS_AGENT_TASK_WORKSPACE).
  const isSelfDelegation = assignee.trim() === "";

  // Resolve the picked target + node into submit values. For a teammate, assignee
  // is their email (fallback to user_id); for self/empty it stays "" (server self
  // semantics). device_id pins to the selected node (null = any daemon).
  const selectedTarget: AgentDelegateTarget | null = useMemo(() => {
    const list = targets?.targets ?? [];
    return list.find((t) => t.user_id === selectedUserId) ?? list[0] ?? null;
  }, [targets, selectedUserId]);

  // On mount: load sessions, then the newest session's thread (if any).
  // 캐시 seed가 있으면 스피너 없이 그대로 두고 백그라운드에서 최신으로 교체한다.
  // 캐시된 첫 세션이 있으면 목록과 그 상세를 병렬로 미리 받아 기존
  // getAgentChats→getAgentChat 직렬 waterfall을 없앤다.
  useEffect(() => {
    let alive = true;

    async function loadInitial() {
      const cachedFirst =
        readCachedResource<AgentChatSession[]>(CHATS_CACHE_KEY)?.[0]?.session_id ?? null;
      const prefetchedDetail = cachedFirst
        ? api.getAgentChat(cachedFirst).catch(() => null)
        : null;
      try {
        const rows = await api.getAgentChats();
        if (!alive) return;
        setSessions(rows);
        const first = rows[0]?.session_id ?? null;
        setCurrentSessionId(first);
        if (first) {
          // 서버의 최신 세션이 초기 seed(캐시 첫 세션)와 다를 수 있으므로 그 세션의
          // 캐시 스레드로 다시 seed하고, 없으면 스피너로 전환한다(이전 세션 스레드가
          // 다른 세션 화면/캐시로 새지 않게 thread를 세션과 함께 원자 교체).
          const cachedThread = readCachedResource<AgentChatMessage[]>(chatCacheKey(first));
          if (cachedThread) {
            setThread({ sessionId: first, messages: cachedThread });
          } else {
            setMessagesLoading(true);
          }
          try {
            const detail =
              (first === cachedFirst && prefetchedDetail ? await prefetchedDetail : null) ??
              (await api.getAgentChat(first));
            if (alive) {
              setThread({ sessionId: first, messages: detail.messages });
              // C7: 리로드 후에도 조각 칩 복원 (아래 rehydratedFragments 참조).
              setDecomposedFragments(rehydratedFragments(detail.messages));
            }
          } finally {
            if (alive) setMessagesLoading(false);
          }
        } else {
          setThread({ sessionId: null, messages: [] });
        }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : "대화 목록을 불러오지 못했습니다.");
      } finally {
        if (alive) setSessionsLoading(false);
      }
    }

    void loadInitial();
    return () => {
      alive = false;
    };
  }, []);

  // Load the delegate targets (teammates + their nodes) on mount — the chat
  // @멘션 자동완성이 위임 토글을 열지 않아도 같은 targets 소스를 쓰기 때문에
  // 토글-지연 로드면 멘션 패널이 영원히 비게 된다(QA 실측). On error
  // (or supported=false) the controls drop to the raw-email fallback path.
  useEffect(() => {
    if (targets !== null || targetsError) return;
    let alive = true;
    api
      .getDelegateTargets()
      .then((res) => {
        if (!alive) return;
        setTargets(res);
        // Default selection: self first, then its default node.
        const first = res.targets[0] ?? null;
        setSelectedUserId(first?.user_id ?? null);
        setSelectedDeviceId(first ? defaultNodeDeviceId(first) : null);
      })
      .catch(() => {
        if (alive) setTargetsError(true);
      });
    return () => {
      alive = false;
    };
  }, [targets, targetsError]);

  // Fallback path only: load the caller's own reported run folders (self).
  useEffect(() => {
    if (!delegate || !useFallback || !isSelfDelegation || folders !== null) return;
    let alive = true;
    api
      .getAgentFolders()
      .then((res) => {
        if (alive) setFolders(res);
      })
      .catch(() => {
        if (alive) setFolders({ supported: false, default: null, recent: [], reported_at: null });
      });
    return () => {
      alive = false;
    };
  }, [delegate, useFallback, isSelfDelegation, folders]);

  // Pick a user: select them and reset to their default node.
  function selectUser(userId: string) {
    const target = (targets?.targets ?? []).find((t) => t.user_id === userId) ?? null;
    setSelectedUserId(userId);
    setSelectedDeviceId(target ? defaultNodeDeviceId(target) : null);
  }

  // Slice C: which work_ids are still awaiting a result. A work_id is pending when
  // it appears on a message (the delegate ack) but no SECOND message carries that
  // same work_id (the terminal result) and we haven't already resolved it locally.
  const pendingWorkIds = useMemo(() => {
    const counts = new Map<string, number>();
    for (const message of messages) {
      if (!message.work_id) continue;
      counts.set(message.work_id, (counts.get(message.work_id) ?? 0) + 1);
    }
    const pending: string[] = [];
    for (const [workId, count] of counts) {
      if (count < 2 && !resolvedWorkIds.has(workId)) pending.push(workId);
    }
    return pending;
  }, [messages, resolvedWorkIds]);

  // Stable dependency key so the poll effect only re-arms when the pending SET
  // changes, not on every messages reference change.
  const pendingWorkIdsKey = pendingWorkIds.slice().sort().join(",");
  const pendingInputWorkId = useMemo(() => latestInputRequestWorkId(messages), [messages]);
  // HITL: the latest still-waiting structured question (composer free-text answers
  // it via hitl-response instead of orchestrate).
  const pendingHitlMessage = useMemo(() => latestWaitingQuestion(messages), [messages]);

  // Slice C: poll each pending work_id for its terminal result and append it once.
  // Only runs while the tab is visible; clears itself when nothing is pending, on
  // unmount, and on session switch (currentSessionId in the deps).
  useEffect(() => {
    const sid = currentSessionId;
    if (!sid || pendingWorkIds.length === 0) return;

    let alive = true;

    const poll = async () => {
      if (!alive || document.visibilityState !== "visible") return;
      for (const workId of pendingWorkIds) {
        if (!alive) return;
        try {
          const message = await api.postChatWorkResult(sid, workId);
          if (!alive) return;
          if (message) {
            setThread((current) =>
              current.sessionId !== sid ||
              current.messages.some((m) => m.message_id === message.message_id)
                ? current
                : { sessionId: sid, messages: [...current.messages, message] },
            );
            setResolvedWorkIds((current) => {
              const next = new Set(current);
              next.add(workId);
              return next;
            });
          }
        } catch {
          // Transient error — keep the loop alive and retry on the next tick.
        }
      }
    };

    const interval = setInterval(() => {
      void poll();
    }, 5000);
    return () => {
      alive = false;
      clearInterval(interval);
    };
    // pendingWorkIdsKey captures the pending set; currentSessionId re-arms on switch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentSessionId, pendingWorkIdsKey]);

  // S4 command-split: work_ids of every queued fragment chip currently on the thread.
  const fragmentWorkIds = useMemo(() => {
    const ids = new Set<string>();
    for (const parts of Object.values(decomposedFragments)) {
      for (const part of parts) if (part.agent_work_id) ids.add(part.agent_work_id);
    }
    return [...ids];
  }, [decomposedFragments]);

  // Only poll fragments not yet in a terminal state (live badges converge, then stop).
  const activeFragmentIds = useMemo(
    () =>
      fragmentWorkIds.filter((id) => {
        const state = fragmentLive[id]?.state;
        return !state || !FRAGMENT_TERMINAL_STATES.has(state);
      }),
    [fragmentWorkIds, fragmentLive],
  );
  const activeFragmentKey = activeFragmentIds.slice().sort().join(",");

  // S4-4a: refresh all active fragment badges in one batch roundtrip (cap 50). The
  // batch endpoint is owner-scoped; unknown/foreign ids simply drop out.
  useEffect(() => {
    if (activeFragmentIds.length === 0) return;
    let alive = true;
    const poll = async () => {
      if (!alive || document.visibilityState !== "visible") return;
      try {
        const items = await api.getAgentWorkItemsByIds(activeFragmentIds);
        if (!alive) return;
        setFragmentLive((current) => {
          const next = { ...current };
          for (const item of items) next[item.work_id] = item;
          return next;
        });
      } catch {
        // Transient — retry next tick; the queue-time state stays on the chip.
      }
    };
    void poll();
    const interval = setInterval(() => void poll(), 5000);
    return () => {
      alive = false;
      clearInterval(interval);
    };
    // activeFragmentKey captures the id set; currentSessionId re-arms on switch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentSessionId, activeFragmentKey]);

  async function selectSession(sessionId: string) {
    if (sessionId === currentSessionId) return;
    setCurrentSessionId(sessionId);
    setResolvedWorkIds(new Set());
    setDecomposedFragments({});
    setFragmentLive({});
    setMessageCitations({}); // 라이브 턴 한정 — 세션 전환 시 인용 칩은 사라진다.
    setError(null);
    setAborted(false); // 이전 세션의 "생성이 중단되었습니다" 고정 버블 제거.
    // 방문했던 세션이면 캐시 스레드를 즉시 페인트하고 서버 응답으로 조용히 교체한다.
    // 캐시가 없으면 이전 세션 스레드를 즉시 비운다 — 잔상 방지 + write-back 오염 차단.
    const cached = readCachedResource<AgentChatMessage[]>(chatCacheKey(sessionId));
    if (cached) {
      setThread({ sessionId, messages: cached });
    } else {
      setThread({ sessionId, messages: [] });
      setMessagesLoading(true);
    }
    try {
      const detail = await api.getAgentChat(sessionId);
      // 응답이 오는 사이 또 다른 세션으로 전환됐으면 화면(스레드+조각 칩)은 덮지 않고
      // (늦게 온 응답이 현재 대화를 지우는 것 방지) 캐시에만 반영한다.
      writeCachedResource(chatCacheKey(sessionId), detail.messages);
      if (currentSessionRef.current === sessionId) {
        setThread({ sessionId, messages: detail.messages });
        // C7: 세션 전환 후에도 조각 칩 복원 (rehydratedFragments 참조).
        setDecomposedFragments(rehydratedFragments(detail.messages));
      }
    } catch (e) {
      if (cached) return; // 캐시 페인트 유지 — 백그라운드 갱신 실패는 조용히 무시.
      setError(e instanceof Error ? e.message : "대화를 불러오지 못했습니다.");
    } finally {
      setMessagesLoading(false);
    }
  }

  async function newChat() {
    setError(null);
    setAborted(false);
    try {
      const session = await api.createAgentChat();
      setSessions((current) => [session, ...current]);
      setCurrentSessionId(session.session_id);
      setThread({ sessionId: session.session_id, messages: [] });
    } catch (e) {
      setError(e instanceof Error ? e.message : "새 대화를 만들지 못했습니다.");
    }
  }

  // 세션 제목 변경 — 낙관적으로 목록을 갱신하고 서버 응답으로 교체한다.
  // 빈 제목(공백만)은 무시한다(제목 없는 세션은 미리보기로 라벨을 대신한다).
  async function renameChat(sessionId: string, title: string) {
    const clean = title.trim();
    if (!clean) return;
    setError(null);
    setSessions((current) =>
      current.map((s) =>
        s.session_id === sessionId ? { ...s, title: clean } : s,
      ),
    );
    try {
      const updated = await api.renameAgentChat(sessionId, clean);
      setSessions((current) =>
        current.map((s) => (s.session_id === sessionId ? updated : s)),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "대화 제목을 변경하지 못했습니다.");
      void refreshSessions(); // 서버 상태로 되돌린다.
    }
  }

  // 세션 삭제 — 낙관적으로 목록/캐시에서 제거하고, 현재 세션을 지웠으면 남은 첫
  // 세션(없으면 빈 화면)으로 전환한다. 실패 시 목록을 복구한다.
  async function deleteChat(sessionId: string) {
    setError(null);
    const previous = sessions;
    const remaining = sessions.filter((s) => s.session_id !== sessionId);
    setSessions(remaining);
    // 삭제한 세션의 스레드 캐시 seed도 비운다(재방문 시 stale 페인트 방지).
    removeCachedResource(chatCacheKey(sessionId));
    // 목록이 비면 write-back effect가 발화하지 않으므로 캐시를 명시적으로 맞춘다.
    writeCachedResource(CHATS_CACHE_KEY, remaining);
    if (currentSessionId === sessionId) {
      const next = remaining[0]?.session_id ?? null;
      if (next) {
        void selectSession(next);
      } else {
        setCurrentSessionId(null);
        setThread({ sessionId: null, messages: [] });
      }
    }
    try {
      await api.deleteAgentChat(sessionId);
    } catch (e) {
      setSessions(previous);
      writeCachedResource(CHATS_CACHE_KEY, previous);
      setError(e instanceof Error ? e.message : "대화를 삭제하지 못했습니다.");
    }
  }

  // Pull the latest session ordering/preview after a message lands so HISTORY
  // stays sorted by updated_at with the freshest preview.
  async function refreshSessions() {
    try {
      const rows = await api.getAgentChats();
      setSessions(rows);
    } catch {
      // non-fatal — the message already persisted; ordering just lags.
    }
  }

  // SSE 프레임 → 진행 버블 상태 반영(orchestrate + HITL 재개 공용). 표시 전용 —
  // 프레임 유실/순서 뒤섞임과 무관하게 최종 POST JSON이 SoR다.
  function applyLiveFrame(frame: Record<string, unknown>) {
    if (frame.type === "phase" && typeof frame.stage === "string") {
      const stage = frame.stage as string;
      setAskProgress((current) => ({
        ...current,
        stage,
        evidenceCount:
          typeof frame.evidence_count === "number"
            ? frame.evidence_count
            : current.evidenceCount,
        fanoutTotal:
          stage === "fanout" && typeof frame.total === "number"
            ? frame.total
            : current.fanoutTotal,
      }));
    } else if (frame.type === "agent_output" && typeof frame.chunk === "string") {
      const chunk = frame.chunk as string;
      setAskProgress((current) => ({
        ...current,
        liveText: (current.liveText + chunk).slice(-ASK_LIVE_CAP),
      }));
    } else if (frame.type === "sub_answer_ready") {
      setAskProgress((current) => ({ ...current, subDone: current.subDone + 1 }));
    }
  }

  // 진행 버블 종료: 스레드 append와 같은 동기 블록에서 호출해 React가 한 렌더로
  // 배치한다 — 임시 버블이 사라지는 프레임과 실제 메시지가 나타나는 프레임이 같아
  // 깜빡임이 없다(기존 finally의 setAskLiveText("") 클리어 방식 대체).
  function finishProgress() {
    setAskProgress((current) => ({ ...current, done: true }));
  }

  // Render a RoutedAnswer into the chat thread (shared by orchestrate and the HITL
  // resume). A turn-ending question takes precedence over the queue-chip ack so the
  // user sees the question, not a "queued for review" chip.
  async function applyRoutedResult(sid: string, result: RoutedAnswer) {
    if (result.user_input_request) {
      appendThreadMessage(sid, questionMessageFromUir(result.user_input_request));
      finishProgress();
      return;
    }
    if (result.mode === "agent_work" && result.agent_work) {
      const aw = result.agent_work;
      const label = DELEGATE_STATE_LABEL[aw.state] ?? aw.state;
      const reason = aw.policy_reason ? ` · ${aw.policy_reason}` : "";
      const ackText = `[${label}] ${aw.title}\n${aw.message}${reason}`;
      const agentMessage = await api.appendAgentChatMessage(sid, {
        role: "agent",
        text: ackText,
        work_id: aw.work_id,
      });
      appendThreadMessage(sid, agentMessage);
      finishProgress();
    } else {
      const agentMessage = await api.appendAgentChatMessage(sid, {
        role: "agent",
        text: formatAgentAnswer(result),
      });
      appendThreadMessage(sid, agentMessage);
      finishProgress();
      // 라이브 턴만: 이 답변의 wiki 인용을 방금 붙인 메시지에 매핑한다(위 state 주석 참조).
      const citationLinks = collectWikiLinks(result);
      if (citationLinks.length) {
        setMessageCitations((current) => ({
          ...current,
          [agentMessage.message_id]: citationLinks,
        }));
      }
      if (result.mode === "decomposed") {
        const queued = queuedCommandParts(result);
        if (queued.length) {
          setDecomposedFragments((current) => ({
            ...current,
            [agentMessage.message_id]: queued,
          }));
        }
      }
    }
  }

  // Flip a waiting question message to answered locally so its bubble disables
  // (the next getAgentChat refetch is the SoR).
  function markQuestionAnswered(sid: string, messageId: string) {
    setThread((current) =>
      current.sessionId !== sid
        ? current
        : {
            sessionId: sid,
            messages: current.messages.map((m) =>
              m.message_id === messageId
                ? { ...m, payload: { ...(m.payload ?? {}), status: "answered" } }
                : m,
            ),
          },
    );
  }

  // Core HITL answer flow: POST the answer, append the server response row, mark the
  // question answered, and render the resume turn. Does NOT manage chatting/error
  // state (the caller does). Opens the live SSE like orchestrate for feedback.
  async function respondToHitl(
    sid: string,
    question: AgentChatMessage,
    opts: { answer?: string; decision?: "approve" | "reject"; values?: Record<string, string> },
  ) {
    const streamId =
      typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : null;
    let es: EventSource | null = null;
    setAskProgress(IDLE_PROGRESS);
    if (streamId) {
      try {
        es = new EventSource(`${API_BASE}/agent-work/chats/stream/${streamId}`, {
          withCredentials: true,
        });
        es.onmessage = (event) => {
          try {
            const frame = JSON.parse(event.data);
            applyLiveFrame(frame);
            if (frame.type === "turn_complete" || frame.type === "decompose_done") {
              es?.close();
            }
          } catch {
            /* ignore malformed frames; the POST JSON is authoritative */
          }
        };
      } catch {
        es = null;
      }
    }
    try {
      const res = await api.hitlRespond(sid, {
        message_id: question.message_id,
        answer: opts.answer ?? null,
        decision: opts.decision ?? null,
        values: opts.values ?? null,
        stream_id: streamId,
      });
      appendThreadMessage(sid, res.response_message);
      markQuestionAnswered(sid, question.message_id);
      await applyRoutedResult(sid, res.routed);
    } finally {
      // 성공 경로는 applyRoutedResult가 done으로 닫았다. 여기서 IDLE로 되돌리면
      // chatting이 아직 true인 동안 진행 버블이 재등장(깜빡임)하므로 리셋하지
      // 않는다 — 다음 턴 시작 시 IDLE로 초기화된다.
      es?.close();
    }
  }

  // Bubble-click entry point (option/approval buttons). Manages chatting/error and
  // draft clearing, then defers to respondToHitl.
  async function handleHitlRespond(
    question: AgentChatMessage,
    opts: { answer?: string; decision?: "approve" | "reject"; values?: Record<string, string> },
  ) {
    const sid = currentSessionId;
    if (!sid || chatting) return;
    setChatting(true);
    setError(null);
    try {
      await respondToHitl(sid, question, opts);
      await refreshSessions();
    } catch (e) {
      setError(e instanceof Error ? e.message : "응답 처리에 실패했습니다.");
    } finally {
      setChatting(false);
    }
  }

  // 위임 toggle 경로와 @멘션 경로가 동일한 payload 기본값(runner/mode/cwd/session)을
  // 공유하도록 뽑아낸 빌더. mentionTarget이 있으면 그 팀원이 담당자로 override된다
  // (toggle ON 상태여도 그 제출 한정으로 멘션이 담당자를 정한다).
  function buildDelegateRequest(
    instruction: string,
    sid: string,
    mentionTarget: AgentDelegateTarget | null,
  ): AgentDelegateRequest {
    const folder = normalizeCwd(cwd);
    let resolvedAssignee: string | null;
    let resolvedDeviceId: string | null;
    if (mentionTarget) {
      // 멘션 대상이 담당자. 본인 멘션은 toggle 경로의 self 의미(null)와 동일하게 보낸다.
      resolvedAssignee = mentionTarget.is_self
        ? null
        : mentionTarget.email ?? mentionTarget.user_id;
      // 노드 핀은 picker가 같은 대상을 보고 있을 때만 유지(다른 사람 노드 오핀 방지).
      resolvedDeviceId =
        !useFallback && selectedTarget?.user_id === mentionTarget.user_id
          ? selectedDeviceId
          : null;
    } else {
      // 기존 toggle 경로 그대로: teammate → email(fallback user_id); self/empty →
      // null(server self semantics). device_id는 고른 노드에 핀.
      resolvedAssignee = useFallback
        ? assignee.trim() || null
        : selectedTarget && !selectedTarget.is_self
          ? selectedTarget.email ?? selectedTarget.user_id
          : null;
      resolvedDeviceId = useFallback ? null : selectedDeviceId;
    }
    return {
      instruction,
      assignee: resolvedAssignee,
      runner,
      mode: folder ? "code" : "knowledge",
      cwd: folder,
      device_id: resolvedDeviceId,
      session_id: sid,
    };
  }

  async function submitChat(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const question = chatDraft.trim();
    if (!question || chatting) return;
    await sendText(question, true);
  }

  // 중단: 진행 중 orchestrate 턴의 fetch를 abort하고 라이브 SSE를 닫는다.
  // 클라이언트 분리만 — 서버측 취소는 시도하지 않는다(주석 위 stoppable 참조).
  function stopGeneration() {
    setAborted(true);
    liveEsRef.current?.close();
    liveEsRef.current = null;
    abortRef.current?.abort();
  }

  // 재생성: 마지막 user 메시지 텍스트로 기존 send 경로를 그대로 재실행한다.
  // 기존 메시지는 삭제/교체하지 않는다 — 기존 흐름대로 새 user/agent 메시지가
  // 스레드에 추가된다. HITL/입력 대기 중에는 send 경로 의미가 달라지므로 무시.
  async function regenerateLast() {
    if (chatting || pendingHitlMessage || pendingInputWorkId) return;
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    const question = lastUser?.text.trim();
    if (!question) return;
    await sendText(question, false);
  }

  // submitChat(컴포저 제출)과 regenerateLast(재생성)가 공유하는 send 경로 본체.
  // fromComposer일 때만 chatDraft를 비운다(재생성이 입력 중 드래프트를 지우지 않게).
  async function sendText(question: string, fromComposer: boolean) {
    // 멘션 위임(FE 결정론): 드래프트가 알려진 팀원 이메일 `@<email>`로 시작하면
    // 기존 구조화 위임 경로(api.delegateAgentTask)로 라우팅한다. 문장 중간 멘션이나
    // 미등록 주소는 null → 평소처럼 orchestrate(멘션은 그냥 텍스트).
    const mentionDelegate = parseLeadingMention(question, targets);

    setChatting(true);
    setError(null);
    setAborted(false);
    // 새 턴 시작: 이전 턴의 진행 상태(done/finalText 포함)를 비운다 — done이 남아
    // 있으면 이번 턴의 진행 버블이 아예 렌더되지 않는다(delegate/HITL 경로 포함).
    setAskProgress(IDLE_PROGRESS);

    try {
      // Ensure a session exists before persisting the first message.
      let sid = currentSessionId;
      if (!sid) {
        const session = await api.createAgentChat();
        sid = session.session_id;
        setSessions((current) => [session, ...current]);
        setCurrentSessionId(sid);
        // 새 세션의 빈 스레드를 세션 id와 함께 seed해 아래 append가 붙을 곳을 만든다.
        setThread({ sessionId: sid, messages: [] });
      }

      // HITL free-text answer: the server appends the user response row (kind
      // user_input_response), so DON'T pre-append the user message here (it would
      // double). Route to hitl-response instead of orchestrate.
      if (pendingHitlMessage) {
        if (fromComposer) setChatDraft("");
        await respondToHitl(sid, pendingHitlMessage, { answer: question });
        await refreshSessions();
        return;
      }

      const userMessage = await api.appendAgentChatMessage(sid, {
        role: "user",
        text: question,
      });
      appendThreadMessage(sid, userMessage);
      if (fromComposer) setChatDraft("");

      if (pendingInputWorkId) {
        const item = await api.continueAgentTask(sid, { text: question });
        const label = DELEGATE_STATE_LABEL[item.state] ?? item.state;
        const detail = DELEGATE_STATE_MESSAGE[item.state] ?? "에이전트 작업으로 접수되었습니다.";
        const reason = item.policy_reason ? ` · ${item.policy_reason}` : "";
        const ackText = `[${label}] ${item.title}\n${detail}${reason}`;
        const agentMessage = await api.appendAgentChatMessage(sid, {
          role: "agent",
          text: ackText,
          work_id: item.work_id,
        });
        appendThreadMessage(sid, agentMessage);
      } else if (delegate || mentionDelegate) {
        // Unified "로컬 실행": no mode toggle. A picked @folder means run there
        // (code mode, needs a cwd); no folder runs in the daemon's default
        // workspace (knowledge mode, no cwd required). Both run locally.
        // 멘션 위임이면 지시문에서 멘션 토큰을 뗀다(채팅 버블에는 원문 그대로 남음 —
        // user message는 위에서 이미 원문으로 append됐다).
        const item = await api.delegateAgentTask(
          buildDelegateRequest(
            mentionDelegate ? mentionDelegate.instruction : question,
            sid,
            mentionDelegate?.target ?? null,
          ),
        );
        const label = DELEGATE_STATE_LABEL[item.state] ?? item.state;
        const detail = DELEGATE_STATE_MESSAGE[item.state] ?? "에이전트 작업으로 접수되었습니다.";
        const reason = item.policy_reason ? ` · ${item.policy_reason}` : "";
        const ackText = `[${label}] ${item.title}\n${detail}${reason}`;
        const agentMessage = await api.appendAgentChatMessage(sid, {
          role: "agent",
          text: ackText,
          work_id: item.work_id,
        });
        appendThreadMessage(sid, agentMessage);
      } else {
        // Orchestrator: compound-question decompose + single-command intake + read→act.
        // Open the live SSE BEFORE awaiting the POST so the decompose wave / agentic
        // frames render in the "생성 중" bubble. Best-effort — if orchestration is off
        // the stream 404s and the JSON answer still arrives.
        const streamId =
          typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : null;
        let es: EventSource | null = null;
        if (streamId) {
          try {
            es = new EventSource(`${API_BASE}/agent-work/chats/stream/${streamId}`, {
              withCredentials: true,
            });
            es.onmessage = (event) => {
              try {
                const frame = JSON.parse(event.data);
                applyLiveFrame(frame);
                if (frame.type === "turn_complete" || frame.type === "decompose_done") {
                  es?.close();
                }
              } catch {
                // Ignore malformed frames; the POST JSON is the source of truth.
              }
            };
          } catch {
            es = null;
          }
        }
        // 중단 배선: abort는 이 fetch만 끊는 클라이언트 분리다(서버측 취소 없음).
        const controller = new AbortController();
        abortRef.current = controller;
        liveEsRef.current = es;
        setStoppable(true);
        try {
          const result = await api.orchestrate(sid, {
            text: question,
            scope: agentAskScope(scopeFilter),
            contextWikiSlug: wikiSlugFilter,
            streamId,
            signal: controller.signal,
          });
          // morph: 최종 답변이 도착하면 진행 버블 본문을 즉시 최종 답변으로 바꾼다
          // (persist POST 동안 유지). 같은 버블이 내용만 바뀌므로 삭제→재생성
          // 깜빡임이 없고, 스레드 append 시 finishProgress와 같은 렌더로 교체된다.
          if (!result.user_input_request && result.mode !== "agent_work") {
            const finalText = formatAgentAnswer(result);
            setAskProgress((current) => ({ ...current, finalText }));
          }
          // Shared renderer: a HITL question, a queued command chip, or a plain
          // answer (with decomposed fragment chips). Reused by the HITL resume.
          await applyRoutedResult(sid, result);
        } finally {
          // 성공 경로는 applyRoutedResult가 done으로 닫았다(진행 버블 ↔ 실제 메시지
          // 같은 렌더 교체). 여기서 IDLE로 리셋하면 chatting이 아직 true인 남은
          // 구간(refreshSessions)에 스피너 버블이 재등장하므로 리셋하지 않는다.
          es?.close();
          abortRef.current = null;
          liveEsRef.current = null;
          setStoppable(false);
        }
      }

      await refreshSessions();
    } catch (e) {
      // 사용자 중단(AbortError)은 실패가 아니다 — 배너 없이 중단 버블만 남긴다.
      if ((e as { name?: string } | null)?.name === "AbortError") return;
      setError(
        e instanceof Error
          ? e.message
          : pendingInputWorkId || delegate || mentionDelegate
            ? "위임에 실패했습니다."
            : "에이전트 응답 실패",
      );
    } finally {
      setChatting(false);
    }
  }

  const pageTitle = scopeFilter === "company" ? "에이전트 · 회사" : "에이전트";
  const pageSubtitle = `chat · history${wikiSlugFilter ? ` · wiki ${wikiSlugFilter}` : ""}`;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title={pageTitle}
        subtitle={pageSubtitle}
        right={
          <Toolbar>
            <ToolbarButton
              icon={<Plus size={14} strokeWidth={1.8} />}
              onClick={newChat}
              tone="primary"
            >
              새 대화
            </ToolbarButton>
          </Toolbar>
        }
      />

      <div
        className={
          compactShell
            ? "grid min-h-0 flex-1 auto-rows-max grid-cols-1 content-start gap-4 overflow-y-auto p-4 pb-[calc(var(--mobile-safe-bottom)+var(--mobile-nav-height)+16px)]"
            : "grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-y-auto p-5 xl:grid-cols-[280px_minmax(0,1fr)] xl:overflow-hidden"
        }
      >
        <section className={compactShell ? "h-fit min-h-0" : "min-h-0 xl:overflow-y-auto"}>
          <HistoryPanel
            sessions={sessions}
            currentSessionId={currentSessionId}
            loading={sessionsLoading}
            onSelect={selectSession}
            onRename={renameChat}
            onDelete={deleteChat}
          />
        </section>

        <section className={compactShell ? "h-fit min-h-0" : "min-h-0 xl:overflow-hidden"}>
          <AgentChat
            aborted={aborted}
            askProgress={askProgress}
            assignee={assignee}
            chatDraft={chatDraft}
            chatting={chatting}
            onStop={stoppable ? stopGeneration : null}
            onRegenerate={regenerateLast}
            cwd={cwd}
            delegate={delegate}
            folders={folders}
            isSelfDelegation={isSelfDelegation}
            messages={messages}
            messagesLoading={messagesLoading}
            decomposedFragments={decomposedFragments}
            fragmentLive={fragmentLive}
            messageCitations={messageCitations}
            pendingInput={pendingInputWorkId !== null}
            pendingHitl={pendingHitlMessage !== null}
            onHitlRespond={handleHitlRespond}
            onFragmentDecided={(item) =>
              setFragmentLive((current) => ({ ...current, [item.work_id]: item }))
            }
            targets={targets}
            targetsError={targetsError}
            selectedUserId={selectedUserId}
            selectedDeviceId={selectedDeviceId}
            onSelectUser={selectUser}
            onSelectDevice={setSelectedDeviceId}
            onAssignee={setAssignee}
            onCwd={setCwd}
            onDraftChange={setChatDraft}
            onRunner={setRunner}
            onSubmit={submitChat}
            onToggleDelegate={() => setDelegate((v) => !v)}
            runner={runner}
          />
          <div className="mt-3 space-y-3">
            {error ? (
              <Banner tone="fail" title="실패">
                {error}
              </Banner>
            ) : null}
            {wikiSlugFilter ? (
              <Card className="p-3 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-body)" }}>
                <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                  <span className="break-words font-[family-name:var(--font-mono)]">{wikiSlugFilter}</span>
                  <Link className="font-semibold" href="/agent-work" style={{ color: "var(--color-text-strong)" }}>
                    해제
                  </Link>
                </div>
              </Card>
            ) : null}
          </div>
        </section>
      </div>
    </div>
  );
}

function HistoryPanel({
  sessions,
  currentSessionId,
  loading,
  onSelect,
  onRename,
  onDelete,
}: {
  sessions: AgentChatSession[];
  currentSessionId: string | null;
  loading: boolean;
  onSelect: (sessionId: string) => void;
  onRename: (sessionId: string, title: string) => void;
  onDelete: (sessionId: string) => void;
}) {
  // 마우스 오버 시 드러나는 제목 변경 / 삭제 컨트롤. editingId는 인라인 이름
  // 편집 중인 세션.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");

  function startEdit(session: AgentChatSession) {
    setEditingId(session.session_id);
    setDraftTitle(session.title || "");
  }
  function commitEdit(sessionId: string) {
    const next = draftTitle.trim();
    setEditingId(null);
    // 제목이 실제로 바뀐 경우에만 서버 호출(빈 값/무변경은 무시).
    const current = sessions.find((s) => s.session_id === sessionId);
    if (next && next !== (current?.title || "")) onRename(sessionId, next);
  }

  return (
    <Card className="flex min-h-[180px] flex-col p-3 xl:min-h-full">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Clock size={16} strokeWidth={1.8} />
          <ModeBadge>HISTORY</ModeBadge>
        </div>
        <Chip tone="neutral">{sessions.length}</Chip>
      </div>
      {loading ? (
        <div className="flex flex-1 items-center justify-center text-center text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          <Loader2 className="animate-spin" size={16} strokeWidth={1.8} />
        </div>
      ) : sessions.length ? (
        <div className="min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
          {sessions.map((session) => {
            const active = session.session_id === currentSessionId;
            const editing = editingId === session.session_id;
            const label =
              session.title || session.last_message_preview || "새 대화";
            const boxStyle = {
              background: active
                ? "var(--color-sidebar-active)"
                : "var(--color-card)",
              border: `1px solid ${active ? "var(--color-text-strong)" : "var(--color-divider-soft)"}`,
              color: "var(--color-text-body)",
            } as const;
            if (editing) {
              return (
                <div
                  className="rounded-[var(--radius-card)] p-2.5"
                  key={session.session_id}
                  style={boxStyle}
                >
                  <input
                    autoFocus
                    className={inputClass}
                    value={draftTitle}
                    onChange={(e) => setDraftTitle(e.target.value)}
                    onBlur={() => commitEdit(session.session_id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        commitEdit(session.session_id);
                      } else if (e.key === "Escape") {
                        e.preventDefault();
                        setEditingId(null);
                      }
                    }}
                    aria-label="대화 제목"
                    maxLength={60}
                  />
                </div>
              );
            }
            return (
              <div className="group relative" key={session.session_id}>
                <button
                  className="w-full rounded-[var(--radius-card)] p-2.5 pr-14 text-left text-[var(--text-body-sm)] transition-colors"
                  onClick={() => onSelect(session.session_id)}
                  type="button"
                  style={boxStyle}
                >
                  <div className="mb-1 flex items-center gap-2">
                    <span
                      className="truncate font-semibold"
                      style={{ color: "var(--color-text-strong)" }}
                    >
                      {label}
                    </span>
                  </div>
                  {session.last_message_at ? (
                    <span
                      className="font-[family-name:var(--font-mono)] text-[10px]"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      {formatDate(session.last_message_at)}
                    </span>
                  ) : null}
                </button>
                {/* 마우스 오버 시에만 노출(키보드 포커스 포함). 클릭 즉시 실행. */}
                <div className="absolute right-1.5 top-1/2 flex -translate-y-1/2 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                  <IconAction label="제목 변경" onClick={() => startEdit(session)}>
                    <Pencil size={14} strokeWidth={1.8} />
                  </IconAction>
                  <IconAction
                    label="삭제"
                    tone="danger"
                    onClick={() => {
                      setEditingId(null);
                      onDelete(session.session_id);
                    }}
                  >
                    <Trash2 size={14} strokeWidth={1.8} />
                  </IconAction>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="flex flex-1 items-center justify-center text-center text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          대화 기록 없음
        </div>
      )}
    </Card>
  );
}

// HISTORY 항목의 hover 액션 버튼(제목 변경 / 삭제). 테두리·배경 없이 아이콘만
// 두고, hover 시 살짝 진해진다. 부모 세션 버튼과 겹치지 않게 클릭 전파를 막는다.
function IconAction({
  label,
  onClick,
  tone,
  children,
}: {
  label: string;
  onClick: () => void;
  tone?: "danger";
  children: React.ReactNode;
}) {
  return (
    <button
      aria-label={label}
      title={label}
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className="flex h-6 w-6 items-center justify-center rounded-[var(--radius-toolbar)] opacity-70 transition-opacity hover:opacity-100"
      style={{
        color:
          tone === "danger"
            ? "var(--color-text-danger)"
            : "var(--color-text-muted)",
      }}
    >
      {children}
    </button>
  );
}

function AgentChat({
  aborted,
  askProgress,
  assignee,
  chatDraft,
  chatting,
  cwd,
  delegate,
  folders,
  isSelfDelegation,
  messages,
  messagesLoading,
  decomposedFragments,
  fragmentLive,
  messageCitations,
  targets,
  targetsError,
  selectedUserId,
  selectedDeviceId,
  onSelectUser,
  onSelectDevice,
  onAssignee,
  onCwd,
  onDraftChange,
  onRunner,
  onStop,
  onRegenerate,
  onSubmit,
  onToggleDelegate,
  runner,
  pendingInput,
  pendingHitl,
  onHitlRespond,
  onFragmentDecided,
}: {
  aborted: boolean;
  askProgress: AskProgress;
  assignee: string;
  chatDraft: string;
  chatting: boolean;
  cwd: string;
  delegate: boolean;
  folders: AgentFolderList | null;
  isSelfDelegation: boolean;
  messages: AgentChatMessage[];
  messagesLoading: boolean;
  decomposedFragments: Record<string, SubAnswer[]>;
  fragmentLive: Record<string, AgentWorkItem>;
  messageCitations: Record<string, WikiLinkRef[]>;
  pendingInput: boolean;
  targets: AgentDelegateTargetList | null;
  targetsError: boolean;
  selectedUserId: string | null;
  selectedDeviceId: string | null;
  onSelectUser: (userId: string) => void;
  onSelectDevice: (deviceId: string | null) => void;
  onAssignee: (value: string) => void;
  onCwd: (value: string) => void;
  onDraftChange: (value: string) => void;
  onRunner: (value: AgentRunner) => void;
  // 중단 가능한 orchestrate 턴에서만 non-null — 제출 버튼이 중단 버튼으로 바뀐다.
  onStop: (() => void) | null;
  // 마지막 어시스턴트 버블의 재생성 버튼 핸들러(마지막 user 메시지로 재실행).
  onRegenerate: () => void;
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
  onToggleDelegate: () => void;
  runner: AgentRunner;
  pendingHitl: boolean;
  onHitlRespond: (
    question: AgentChatMessage,
    opts: { answer?: string; decision?: "approve" | "reject"; values?: Record<string, string> },
  ) => void;
  onFragmentDecided: (item: AgentWorkItem) => void;
}) {
  // 복사 버튼 노출/탭 타깃(모바일 항상 노출 + 44px)을 위한 compact 셸 여부.
  const compactShell = useCompactAgentWorkShell();

  // 재생성 버튼은 마지막 어시스턴트 응답 버블에만 노출한다(복사 버튼 옆).
  const lastAgentMessageId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i].role === "agent") return messages[i].message_id;
    }
    return null;
  }, [messages]);

  // A delegated agent_task is "running" when its dispatch ack ("[실행 시작] …")
  // is on the thread but no terminal result message carries the same work_id yet
  // (Slice C appends the result as a 2nd work_id message). Drives the live
  // "답변 생성 중" indicator so an async delegation isn't a silent wait.
  const runningWorkIds = useMemo(() => {
    const stat = new Map<string, { count: number; dispatched: boolean }>();
    for (const message of messages) {
      if (!message.work_id) continue;
      const current = stat.get(message.work_id) ?? { count: 0, dispatched: false };
      current.count += 1;
      if (message.role === "agent" && message.text.startsWith(DISPATCH_ACK_PREFIX)) {
        current.dispatched = true;
      }
      stat.set(message.work_id, current);
    }
    return [...stat.entries()]
      .filter(([, value]) => value.dispatched && value.count < 2)
      .map(([workId]) => workId);
  }, [messages]);

  // Streaming slice: while a delegated work_id is running, subscribe to its live
  // SSE output and accumulate the agent's stdout/stderr lines so the "생성 중"
  // bubble shows progress instead of a silent wait. Best-effort: when streaming is
  // off on the server the endpoint 404s and EventSource closes without retry (no
  // hammering); the terminal result still arrives via the Slice C poll writeback.
  // Per-work accumulated live text. Only entries whose work_id is still running are
  // ever rendered (the render maps over runningWorkIds), so stale entries are inert;
  // each entry is length-capped in the message callback to bound memory. The text is
  // mutated only from the async onmessage callback (never synchronously in the effect
  // body) to avoid cascading renders.
  const LIVE_CAP = 8000;
  const [liveOutput, setLiveOutput] = useState<Record<string, string>>({});
  const runningKey = runningWorkIds.slice().sort().join(",");
  useEffect(() => {
    if (runningWorkIds.length === 0) return;
    const sources: EventSource[] = [];
    for (const workId of runningWorkIds) {
      let es: EventSource;
      try {
        es = new EventSource(`${API_BASE}/agent-work/${workId}/stream`, {
          withCredentials: true,
        });
      } catch {
        continue;
      }
      es.onmessage = (event) => {
        try {
          const frame = JSON.parse(event.data);
          if (frame.type === "agent_output" && typeof frame.chunk === "string") {
            setLiveOutput((current) => ({
              ...current,
              [workId]: ((current[workId] ?? "") + frame.chunk).slice(-LIVE_CAP),
            }));
          } else if (frame.type === "turn_complete") {
            es.close();
          }
        } catch {
          // Ignore malformed frames; the poll writeback is the source of truth.
        }
      };
      // A non-2xx (streaming disabled) sets readyState=CLOSED and does not retry,
      // so no explicit handling is needed; transient drops auto-reconnect.
      sources.push(es);
    }
    return () => {
      for (const es of sources) es.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runningKey]);

  // @멘션 자동완성: 캐럿이 올라간 `@단어` 조각(mention)과 하이라이트 인덱스,
  // Esc로 닫은 조각의 시작 위치(dismissedAt — 같은 조각에서 재오픈 방지)를 든다.
  // 후보는 위임 picker와 같은 targets 소스에서 파생한다(신규 fetch 없음).
  const chatInputRef = useRef<HTMLTextAreaElement>(null);
  const [mention, setMention] = useState<MentionFragment | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const [mentionDismissedAt, setMentionDismissedAt] = useState<number | null>(null);
  const mentionCandidates = useMemo(
    () => (mention ? filterMentionTargets(targets?.targets ?? [], mention.query) : []),
    [mention, targets],
  );
  const mentionOpen =
    mention !== null &&
    mentionCandidates.length > 0 &&
    mentionDismissedAt !== mention.start;

  // 입력/캐럿 이동 때마다 멘션 조각을 다시 계산한다. 조각(시작/쿼리)이 바뀌면
  // 하이라이트를 첫 항목으로 되돌리고, Esc dismiss는 같은 시작 위치의 조각에서만
  // 유지되며 다른 조각으로 옮겨가면 자동 해제된다.
  function syncMention(el: HTMLTextAreaElement) {
    const fragment = findMentionFragment(el.value, el.selectionStart ?? el.value.length);
    if (fragment?.start !== mention?.start || fragment?.query !== mention?.query) {
      setMentionIndex(0);
    }
    setMention(fragment);
    if (mentionDismissedAt !== null && fragment?.start !== mentionDismissedAt) {
      setMentionDismissedAt(null);
    }
  }

  // 선택: `@조각`을 canonical `@<email> ` 토큰으로 치환하고 캐럿을 토큰 뒤에 둔다.
  function applyMention(target: AgentDelegateTarget) {
    if (!mention || !target.email) return;
    const before = chatDraft.slice(0, mention.start);
    const after = chatDraft.slice(mention.end);
    const token = `@${target.email} `;
    onDraftChange(before + token + after);
    setMention(null);
    const caret = before.length + token.length;
    // controlled value 반영 후 캐럿을 옮긴다(렌더 커밋 다음 프레임).
    requestAnimationFrame(() => {
      const el = chatInputRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(caret, caret);
      }
    });
  }

  // Chat-app scroll: pin the transcript to the newest message + streamed output,
  // but don't yank the view if the user has scrolled up to read older messages.
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  };
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [messages, liveOutput, chatting, askProgress]);

  return (
    <Card className="flex min-h-[320px] flex-col p-3 sm:min-h-[420px] xl:h-full xl:min-h-0">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <MessageSquare size={16} strokeWidth={1.8} />
          <ModeBadge>CHAT</ModeBadge>
        </div>
        {chatting ? (
          <Chip tone="warn">
            <span className="inline-flex items-center gap-1">
              <Loader2 className="animate-spin" size={11} strokeWidth={2.2} />
              응답 중
            </span>
          </Chip>
        ) : (
          <Chip tone="neutral">ready</Chip>
        )}
      </div>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 space-y-3 overflow-y-auto overflow-x-hidden pr-1"
      >
        {messagesLoading ? (
          <div className="flex h-full min-h-[180px] items-center justify-center text-center text-[var(--text-body-sm)] sm:min-h-[220px]" style={{ color: "var(--color-text-muted)" }}>
            <Loader2 className="animate-spin" size={16} strokeWidth={1.8} />
          </div>
        ) : messages.length ? (
          messages.map((message) => (
            <MessageBubble
              key={message.message_id}
              message={message}
              chatting={chatting}
              compactShell={compactShell}
              fragments={decomposedFragments[message.message_id]}
              fragmentLive={fragmentLive}
              citations={messageCitations[message.message_id]}
              onHitlRespond={onHitlRespond}
              onFragmentDecided={onFragmentDecided}
              onRegenerate={
                message.message_id === lastAgentMessageId ? onRegenerate : null
              }
            />
          ))
        ) : (
          <div className="flex h-full min-h-[180px] items-center justify-center text-center text-[var(--text-body-sm)] sm:min-h-[220px]" style={{ color: "var(--color-text-muted)" }}>
            에이전트에게 바로 물어보세요.
          </div>
        )}
        {chatting && !askProgress.done ? (
          // Tier i 진행 버블. finalText가 오면 같은 버블이 최종 답변 본문으로
          // in-place morph하고(스피너 제거), 스레드 append와 같은 렌더에서 done으로
          // 사라져 실제 메시지 버블과 깜빡임 없이 교체된다.
          <div className="mr-auto max-w-[88%]">
            <div
              className="rounded-[var(--radius-card)] p-3 text-[var(--text-body-sm)] leading-[1.55]"
              style={{
                background: "var(--color-sidebar-active)",
                color:
                  !delegate && askProgress.finalText != null
                    ? "var(--color-text-body)"
                    : "var(--color-text-muted)",
              }}
            >
              {!delegate && askProgress.finalText != null ? (
                <MessageMarkdown text={askProgress.finalText} />
              ) : (
                <>
                  <div className="flex items-center gap-2">
                    <Loader2 className="animate-spin" size={15} strokeWidth={1.8} />
                    <span>{delegate ? "위임 중…" : progressLabel(askProgress)}</span>
                  </div>
                  {!delegate && askProgress.liveText ? (
                    <pre
                      className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-words font-[family-name:var(--font-mono)] text-[var(--text-micro)] leading-[1.5]"
                      style={{ color: "var(--color-text-body)" }}
                    >
                      {askProgress.liveText.length > 4000
                        ? askProgress.liveText.slice(-4000)
                        : askProgress.liveText}
                    </pre>
                  ) : null}
                </>
              )}
            </div>
          </div>
        ) : null}
        {!chatting && aborted ? (
          // 중단된 턴의 진행 버블 고정 상태 — 어시스턴트 메시지로 대체하지 않고
          // 다음 전송/세션 전환 때까지 남는다(스레드에는 저장되지 않음).
          <div className="mr-auto max-w-[88%]">
            <div
              className="rounded-[var(--radius-card)] p-3 text-[var(--text-body-sm)]"
              style={{ background: "var(--color-sidebar-active)", color: "var(--color-text-muted)" }}
            >
              <div className="flex items-center gap-2">
                <Square size={13} strokeWidth={2} />
                <span>생성이 중단되었습니다</span>
              </div>
            </div>
          </div>
        ) : null}
        {!chatting && runningWorkIds.length > 0 ? (
          <div className="mr-auto max-w-[88%]">
            <div
              className="rounded-[var(--radius-card)] p-3 text-[var(--text-body-sm)]"
              style={{ background: "var(--color-sidebar-active)", color: "var(--color-text-muted)" }}
            >
              <div className="flex items-center gap-2">
                <Loader2 className="animate-spin" size={15} strokeWidth={1.8} />
                <span>로컬 에이전트가 답변 생성 중…</span>
              </div>
              {runningWorkIds.map((workId) =>
                liveOutput[workId] ? (
                  <pre
                    key={workId}
                    className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-words font-[family-name:var(--font-mono)] text-[var(--text-micro)] leading-[1.5]"
                    style={{ color: "var(--color-text-body)" }}
                  >
                    {liveOutput[workId].length > 4000
                      ? liveOutput[workId].slice(-4000)
                      : liveOutput[workId]}
                  </pre>
                ) : null,
              )}
            </div>
          </div>
        ) : null}
      </div>
      <form className="mt-3 flex flex-col gap-2" onSubmit={onSubmit}>
        <div className="flex flex-wrap items-center gap-1.5">
          <DelegateToggle on={delegate} onToggle={onToggleDelegate} />
          {pendingInput || pendingHitl ? <Chip tone="warn">입력 대기</Chip> : null}
        </div>
        {delegate ? (
          <DelegateControls
            targets={targets}
            targetsError={targetsError}
            selectedUserId={selectedUserId}
            onSelectUser={onSelectUser}
            selectedDeviceId={selectedDeviceId}
            onSelectDevice={onSelectDevice}
            runner={runner}
            onRunner={onRunner}
            cwd={cwd}
            onCwd={onCwd}
            assignee={assignee}
            onAssignee={onAssignee}
            fallbackFolders={folders}
            fallbackIsSelf={isSelfDelegation}
          />
        ) : null}
        <div className="flex items-end gap-2">
          <label className="sr-only" htmlFor="agent-chat-input">
            에이전트 채팅
          </label>
          <div className="relative min-w-0 flex-1">
            {mentionOpen ? (
              <MentionAutocomplete
                candidates={mentionCandidates}
                highlightIndex={mentionIndex}
                onHighlight={setMentionIndex}
                onSelect={applyMention}
              />
            ) : null}
            <textarea
            aria-activedescendant={mentionOpen ? mentionOptionId(mentionIndex) : undefined}
            aria-controls={mentionOpen ? MENTION_LISTBOX_ID : undefined}
            className={`${inputClass} min-h-11 w-full resize-none`}
            id="agent-chat-input"
            onBlur={() => setMention(null)}
            onChange={(event) => {
              onDraftChange(event.target.value);
              syncMention(event.target);
            }}
            onKeyDown={(event) => {
              // 멘션 패널 내비게이션/선택 — Enter 전송 가드보다 먼저 본다.
              // IME 조합 중에는 기존 Enter 가드와 동일하게 어느 쪽도 개입하지 않는다.
              if (mentionOpen && !event.nativeEvent.isComposing) {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setMentionIndex((i) => (i + 1) % mentionCandidates.length);
                  return;
                }
                if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setMentionIndex(
                    (i) => (i - 1 + mentionCandidates.length) % mentionCandidates.length,
                  );
                  return;
                }
                if (event.key === "Enter" || event.key === "Tab") {
                  // 패널이 열려 있는 동안 Enter는 절대 폼 전송으로 내려가지 않는다.
                  event.preventDefault();
                  applyMention(
                    mentionCandidates[Math.min(mentionIndex, mentionCandidates.length - 1)],
                  );
                  return;
                }
                if (event.key === "Escape" && mention) {
                  event.preventDefault();
                  setMentionDismissedAt(mention.start);
                  return;
                }
              }
              // Enter = send, Shift+Enter = newline.
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            // 캐럿 이동(클릭/방향키)에도 멘션 조각을 갱신한다.
            onSelect={(event) => syncMention(event.currentTarget)}
            ref={chatInputRef}
            placeholder={
              pendingHitl
                ? "질문에 대한 답변을 입력"
                : pendingInput
                  ? "선택 번호나 답변을 입력"
                  : delegate
                    ? DELEGATE_PLACEHOLDER
                    : "메일 초안, 일정, 회사/개인 지식 질문 (@멘션 = 팀원 위임)"
            }
            rows={2}
            value={chatDraft}
            />
          </div>
          {chatting && onStop ? (
            // 생성 중엔 제출 버튼 자리가 중단 버튼이 된다(h-11 = 44px 탭 타깃).
            // 클라이언트 분리만 수행 — 서버측 취소는 시도하지 않는다.
            <ToolbarButton
              className="h-11 shrink-0"
              icon={<Square size={15} strokeWidth={1.8} />}
              onClick={onStop}
              type="button"
            >
              중단
            </ToolbarButton>
          ) : (
            <ToolbarButton
              className="h-11 shrink-0"
              disabled={chatting || !chatDraft.trim()}
              icon={
                chatting ? (
                  <Loader2 className="animate-spin" size={15} strokeWidth={1.8} />
                ) : (
                  <Send size={15} strokeWidth={1.8} />
                )
              }
              tone="primary"
              type="submit"
            >
              {chatting
                ? pendingInput || delegate
                  ? "위임 중"
                  : "생성 중"
                : pendingHitl || pendingInput
                  ? "답변"
                  : delegate
                    ? "위임"
                    : "보내기"}
            </ToolbarButton>
          )}
        </div>
      </form>
    </Card>
  );
}

// 채팅 메시지 버블 1개 — 어시스턴트 메시지는 마크다운(GFM) 렌더 + 인용 칩 +
// 복사 버튼, 유저 메시지는 평문 유지. 자동 스크롤/IME 가드 등 상위 로직은
// AgentChat에 그대로 두고 렌더 부분만 추출했다(빅뱅 리팩터 아님).
function MessageBubble({
  message,
  chatting,
  compactShell,
  fragments,
  fragmentLive,
  citations,
  onHitlRespond,
  onFragmentDecided,
  onRegenerate,
}: {
  message: AgentChatMessage;
  chatting: boolean;
  compactShell: boolean;
  fragments: SubAnswer[] | undefined;
  fragmentLive: Record<string, AgentWorkItem>;
  citations: WikiLinkRef[] | undefined;
  onHitlRespond: (
    question: AgentChatMessage,
    opts: { answer?: string; decision?: "approve" | "reject" },
  ) => void;
  onFragmentDecided: (item: AgentWorkItem) => void;
  // 마지막 어시스턴트 응답 버블에만 non-null — 복사 버튼 옆 재생성 버튼.
  onRegenerate: (() => void) | null;
}) {
  const isAgent = message.role === "agent";
  const warn = isAgent && isWarnResult(message.text);
  return (
    <div className={isAgent ? "group mr-auto max-w-[88%]" : "ml-auto max-w-[82%]"}>
      <div
        className="rounded-[var(--radius-card)] p-3 text-[var(--text-body-sm)] leading-[1.55]"
        style={{
          background: !isAgent
            ? "var(--color-text-strong)"
            : warn
              ? "var(--color-warn-bg)"
              : "var(--color-sidebar-active)",
          color: !isAgent
            ? "var(--color-card)"
            : warn
              ? "var(--color-warn-fg)"
              : "var(--color-text-body)",
        }}
      >
        {isAgent ? (
          // 어시스턴트 본문만 마크다운 — 저장 텍스트는 그대로, 렌더만 바뀐다.
          // 프래그먼트 딥링크 세그먼트 분리는 MessageMarkdown 내부에서 수행한다.
          <MessageMarkdown text={message.text} />
        ) : (
          <div className="whitespace-pre-wrap">{message.text}</div>
        )}
        {message.kind === "user_input_request" ? (
          <UserInputRequestControls
            message={message}
            chatting={chatting}
            onRespond={(opts) => onHitlRespond(message, opts)}
          />
        ) : null}
        {isAgent && citations?.length ? <MessageCitationChips links={citations} /> : null}
      </div>
      {isAgent ? (
        <div className="mt-0.5 flex items-center justify-start gap-1">
          <CopyMessageButton text={message.text} compact={compactShell} />
          {onRegenerate ? (
            <RegenerateMessageButton
              onClick={onRegenerate}
              disabled={chatting}
              compact={compactShell}
            />
          ) : null}
        </div>
      ) : null}
      {fragments?.length ? (
        <FragmentChipStrip
          fragments={fragments}
          live={fragmentLive}
          onDecided={onFragmentDecided}
        />
      ) : null}
    </div>
  );
}

/** 마지막 어시스턴트 버블의 재생성 버튼 — 복사 버튼과 같은 노출 규칙(데스크톱
 *  hover/focus 노출, 모바일 compact <760px 항상 노출 + 44px 탭 타깃). 클릭 시
 *  마지막 user 메시지 텍스트로 기존 send 경로를 재실행한다. */
function RegenerateMessageButton({
  onClick,
  disabled,
  compact,
}: {
  onClick: () => void;
  disabled: boolean;
  compact: boolean;
}) {
  return (
    <button
      aria-label="다시 생성"
      title="다시 생성"
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center gap-1 rounded-[var(--radius-toolbar)] px-1.5 text-[var(--text-meta)] transition-opacity disabled:opacity-40 ${
        compact
          ? "min-h-[44px] min-w-[44px] justify-center opacity-80"
          : "h-7 opacity-0 hover:opacity-100 focus-visible:opacity-100 group-hover:opacity-70"
      }`}
      style={{ color: "var(--color-text-muted)" }}
    >
      <RotateCcw size={14} strokeWidth={1.8} />
    </button>
  );
}

// S4-4b (D-FE2): one interactive chip per queued command fragment of a decomposed
// answer. Each chip links to its Agent Work item and shows a state-driven badge, the
// item title, an optional recipient, and reason codes. Wraps on phone (390×844) with
// 44px min tap targets — no horizontal page scroll.
// HITL: interactive controls under a user_input_request bubble. Choice → option
// buttons; approval → 승인/거부; text with `fields` → labeled fill-in-the-blank
// form card (values submitted as hitl-response `values`); text without fields →
// a hint to use the composer (agentic questions, older messages). Disabled once
// answered (payload.status). 44px tap targets, wraps on phones.
function UserInputRequestControls({
  message,
  chatting,
  onRespond,
}: {
  message: AgentChatMessage;
  chatting: boolean;
  onRespond: (opts: {
    answer?: string;
    decision?: "approve" | "reject";
    values?: Record<string, string>;
  }) => void;
}) {
  const uir = (message.payload ?? {}) as unknown as UserInputRequest;
  const answered = uir.status !== "waiting";
  const disabled = answered || chatting;
  const inputType = uir.input_type ?? "text";

  if (answered) {
    return (
      <div className="mt-2">
        <Chip tone="muted">답변 완료</Chip>
      </div>
    );
  }

  if (inputType === "choice" && (uir.options?.length ?? 0) > 0) {
    return (
      <div className="mt-2 flex flex-wrap gap-1.5">
        {uir.options.map((opt) => (
          <button
            key={opt.value}
            type="button"
            disabled={disabled}
            onClick={() => onRespond({ answer: opt.value })}
            className="inline-flex min-h-[44px] items-center rounded-[var(--radius-card)] border px-3 py-1 text-[var(--text-body-sm)] disabled:opacity-50"
            style={{ borderColor: "var(--color-divider)", background: "var(--color-card)", color: "var(--color-text-body)" }}
          >
            {opt.label ?? opt.value}
          </button>
        ))}
      </div>
    );
  }

  if (inputType === "approval") {
    return (
      <div className="mt-2 flex flex-wrap gap-1.5">
        <button
          type="button"
          disabled={disabled}
          onClick={() => onRespond({ decision: "approve" })}
          className="inline-flex min-h-[44px] items-center rounded-[var(--radius-card)] px-4 py-1 text-[var(--text-body-sm)] font-semibold disabled:opacity-50"
          style={{ background: "var(--color-text-strong)", color: "var(--color-card)" }}
        >
          승인
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => onRespond({ decision: "reject" })}
          className="inline-flex min-h-[44px] items-center rounded-[var(--radius-card)] border px-4 py-1 text-[var(--text-body-sm)] disabled:opacity-50"
          style={{ borderColor: "var(--color-divider)", background: "var(--color-card)", color: "var(--color-text-body)" }}
        >
          거부
        </button>
      </div>
    );
  }

  // free-text with fields: labeled fill-in-the-blank form card.
  const fields = uir.fields ?? [];
  if (fields.length > 0) {
    return (
      <HitlFieldForm
        fields={fields}
        disabled={disabled}
        onSubmit={(values) => onRespond({ values })}
      />
    );
  }

  // free-text without fields: answered via the composer below.
  return (
    <div className="mt-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
      아래 입력창에 답변을 입력하세요.
    </div>
  );
}

// Fill-in-the-blank form for a text HITL question that carries `fields`
// (e.g. email_send recipient). Local state per field; Enter submits (IME
// composition guarded like the composer); required fields (default true) must
// be non-blank before submit. Values are trimmed and sent as hitl `values`.
function HitlFieldForm({
  fields,
  disabled,
  onSubmit,
}: {
  fields: UserInputField[];
  disabled: boolean;
  onSubmit: (values: Record<string, string>) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const missingRequired = fields.some(
    (field) => (field.required ?? true) && !(values[field.name] ?? "").trim(),
  );
  const submitDisabled = disabled || missingRequired;

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitDisabled) return;
    const trimmed: Record<string, string> = {};
    for (const field of fields) {
      trimmed[field.name] = (values[field.name] ?? "").trim();
    }
    onSubmit(trimmed);
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="mt-2 flex flex-col gap-2 rounded-[var(--radius-card)] border p-3"
      style={{ borderColor: "var(--color-divider)", background: "var(--color-card)" }}
    >
      {fields.map((field) => (
        <label key={field.name} className="flex flex-col gap-1">
          <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            {field.label}
          </span>
          <input
            type="text"
            value={values[field.name] ?? ""}
            disabled={disabled}
            placeholder={field.placeholder ?? ""}
            onChange={(event) =>
              setValues((current) => ({ ...current, [field.name]: event.target.value }))
            }
            onKeyDown={(event) => {
              // 한글 IME 조합 중 Enter는 제출로 취급하지 않는다 (composer와 동일).
              if (event.key === "Enter" && event.nativeEvent.isComposing) {
                event.preventDefault();
              }
            }}
            className={`${inputClass} min-h-[44px]`}
            style={inputStyle}
          />
        </label>
      ))}
      <div>
        <button
          type="submit"
          disabled={submitDisabled}
          className="inline-flex min-h-[44px] items-center rounded-[var(--radius-card)] px-4 py-1 text-[var(--text-body-sm)] font-semibold disabled:opacity-50"
          style={{ background: "var(--color-text-strong)", color: "var(--color-card)" }}
        >
          제출
        </button>
      </div>
    </form>
  );
}

function FragmentChipStrip({
  fragments,
  live,
  onDecided,
}: {
  fragments: SubAnswer[];
  live: Record<string, AgentWorkItem>;
  onDecided?: (item: AgentWorkItem) => void;
}) {
  return (
    <div className="mt-1.5 flex flex-col gap-1.5">
      {fragments.map((part) =>
        part.agent_work_id ? (
          <FragmentChip
            key={part.agent_work_id}
            part={part}
            liveItem={live[part.agent_work_id]}
            onDecided={onDecided}
          />
        ) : null,
      )}
    </div>
  );
}

// States where a chat-side reviewer decision is meaningful: an email/document draft
// awaiting review, or a command-split action held for approval (request_more_data +
// command_split_hold). Everything else deep-links to /checklist only.
function fragmentIsReviewable(state: string, reasonCodes: string[]): boolean {
  if (state === "draft_for_review") return true;
  return state === "request_more_data" && reasonCodes.includes("command_split_hold");
}

function FragmentChip({
  part,
  liveItem,
  onDecided,
}: {
  part: SubAnswer;
  liveItem: AgentWorkItem | undefined;
  onDecided?: (item: AgentWorkItem) => void;
}) {
  const workId = part.agent_work_id!;
  // BADGE SoR = `state`. Prefer the live batch-polled state; fall back to the
  // queue-time `part.state` before the first poll lands. policy_outcome is NEVER
  // the badge source (a held item is auto_execute by outcome but request_more_data
  // by state — the badge must show the execution truth).
  const state = liveItem?.state ?? part.state ?? "pending";
  const label = DELEGATE_STATE_LABEL[state] ?? FRAGMENT_STATE_LABEL[state] ?? state;
  const tone: ChipTone = DELEGATE_STATE_TONE[state] ?? FRAGMENT_STATE_TONE[state] ?? "neutral";
  const title = (liveItem?.title ?? part.title ?? "").trim() || "명령";
  // Recipient: prefer the queue-time part.recipient (already extracted server-side,
  // S4-4a); fall back to reading the live item payload so a refresh keeps it.
  const recipient = (part.recipient ?? recipientFromPayload(liveItem?.payload) ?? "").trim();
  const reasonCodes = (liveItem?.reason_codes ?? part.reason_codes ?? []).filter(Boolean);
  const reviewable = onDecided != null && fragmentIsReviewable(state, reasonCodes);
  return (
    <div className="flex max-w-full flex-col gap-1">
      <Link
        // Deep-link to the review queue with the item preselected. /checklist reads
        // ?work_id and auto-selects the matching row (its draft body + decision controls).
        // The old `/agent-work#<id>` only set a hash on the chat page (no review surface),
        // so the chip changed the URL but nothing opened.
        href={`/checklist?work_id=${workId}`}
        className="inline-flex min-h-[44px] max-w-full items-center gap-1.5 rounded-[var(--radius-card)] border px-2 py-1 no-underline"
        style={{ borderColor: "var(--color-divider)", background: "var(--color-card)" }}
        title={reasonCodes.length ? reasonCodes.join(", ") : undefined}
      >
        <Chip tone={tone}>{label}</Chip>
        <span
          className="min-w-0 flex-1 truncate text-[var(--text-body-sm)]"
          style={{ color: "var(--color-text-body)" }}
        >
          {title}
        </span>
        {recipient ? (
          <span
            className="shrink-0 truncate text-[var(--text-meta)]"
            style={{ color: "var(--color-text-muted)", maxWidth: "10rem" }}
          >
            {recipient}
          </span>
        ) : null}
        {reasonCodes.length ? <Chip tone="muted">{reasonCodes.join(", ")}</Chip> : null}
      </Link>
      {reviewable ? <InlineDecisionButtons workId={workId} onDecided={onDecided!} /> : null}
    </div>
  );
}

// In-chat approve/dismiss for a reviewable command chip — a convenience alternative
// to opening /checklist. Posts the existing decision endpoint (same policy/audit
// path); the returned item is merged into fragmentLive so the badge converges.
function InlineDecisionButtons({
  workId,
  onDecided,
}: {
  workId: string;
  onDecided: (item: AgentWorkItem) => void;
}) {
  const [busy, setBusy] = useState<"approve" | "dismiss" | null>(null);
  const decide = async (decision: "approve" | "dismiss") => {
    if (busy) return;
    setBusy(decision);
    try {
      const res = await api.decideAgentWork(workId, { decision });
      onDecided(res.item);
    } catch {
      // Leave the buttons enabled for a retry; /checklist remains the fallback.
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="flex flex-wrap gap-1.5">
      <button
        type="button"
        disabled={busy != null}
        onClick={() => decide("approve")}
        className="inline-flex min-h-[44px] items-center rounded-[var(--radius-card)] px-3 py-1 text-[var(--text-body-sm)] font-semibold disabled:opacity-50"
        style={{ background: "var(--color-text-strong)", color: "var(--color-card)" }}
      >
        {busy === "approve" ? "승인 중…" : "승인"}
      </button>
      <button
        type="button"
        disabled={busy != null}
        onClick={() => decide("dismiss")}
        className="inline-flex min-h-[44px] items-center rounded-[var(--radius-card)] border px-3 py-1 text-[var(--text-body-sm)] disabled:opacity-50"
        style={{ borderColor: "var(--color-divider)", background: "var(--color-card)", color: "var(--color-text-body)" }}
      >
        {busy === "dismiss" ? "보류 중…" : "보류"}
      </button>
    </div>
  );
}

// Mirror orthus/agentwork/service.py::extract_command_recipient — recipient may sit
// at payload.recipient_hint (assistant_command) or payload.email_draft.recipient_hint
// (reply draft). Returns null when neither is a non-blank string.
function recipientFromPayload(payload: Record<string, unknown> | undefined): string | null {
  if (!payload) return null;
  const direct = payload.recipient_hint;
  if (typeof direct === "string" && direct.trim()) return direct.trim();
  const draft = payload.email_draft;
  if (draft && typeof draft === "object") {
    const nested = (draft as Record<string, unknown>).recipient_hint;
    if (typeof nested === "string" && nested.trim()) return nested.trim();
  }
  // Mirror backend extract_command_recipient's third shape (P7.1 reply_context.to_addr)
  // so a live-poll refresh of a reply-draft fragment shows the same recipient the
  // server-computed part.recipient carried at queue time (no FE/backend drift).
  const reply = payload.reply_context;
  if (reply && typeof reply === "object") {
    const to = (reply as Record<string, unknown>).to_addr;
    if (typeof to === "string" && to.trim()) return to.trim();
  }
  return null;
}

function agentAskScope(scope: "personal" | "company" | null): AskScope | undefined {
  return scope ?? undefined;
}

// A target's default node device_id: first online node, else the first node.
// null when the target has no nodes or the chosen node is deviceless (legacy).
function defaultNodeDeviceId(target: AgentDelegateTarget): string | null {
  const node = target.nodes.find((n) => n.status === "online") ?? target.nodes[0] ?? null;
  return node ? node.device_id : null;
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// GFM 표 셀 이스케이프 — 파이프는 열 구분을 깨고 개행은 행을 깬다.
function escapeTableCell(value: string): string {
  return value.replace(/\|/g, "\\|").replace(/\r?\n/g, " ");
}

// Render a structured (NL→SQL) result as a GFM table for the chat markdown
// renderer (the old " | " join text lost column alignment once markdown landed).
function formatStructured(result: AssistantResult): string {
  const cols = result.columns ?? [];
  const rows = result.rows ?? [];
  const total = result.row_count ?? rows.length;
  if (rows.length === 0) return "결과 없음 (0행)";
  // Single scalar (e.g. a COUNT) → just "column: value".
  if (rows.length === 1 && cols.length === 1) {
    return `${cols[0]}: ${formatCell(rows[0][0])}`;
  }
  const CAP = 20;
  const shown = rows.slice(0, CAP);
  // GFM 표는 헤더 행이 필수 — 컬럼명이 없으면 자리 이름으로 채운다.
  const width = cols.length || (shown[0]?.length ?? 0);
  const headerCells = cols.length
    ? cols.map((c) => escapeTableCell(String(c)))
    : Array.from({ length: width }, (_, i) => `col${i + 1}`);
  const header = `| ${headerCells.join(" | ")} |\n| ${headerCells.map(() => "---").join(" | ")} |`;
  const body = shown
    .map((row) => `| ${row.map((cell) => escapeTableCell(formatCell(cell))).join(" | ")} |`)
    .join("\n");
  const footer =
    total > shown.length
      ? `\n\n… 외 ${total - shown.length}행 (총 ${total}행)`
      : `\n\n(총 ${total}행)`;
  return `${header}\n${body}${footer}`;
}

// 라이브 턴 인용 수집: RoutedAnswer의 wiki_links(없으면 sources에서 유도)를
// slug/scope 기준 dedupe해 칩 데이터로 만든다. decomposed면 하위 파트의 wiki
// 답변까지 훑는다. 저장은 하지 않는다(라이브 턴 한정 — MessageCitationChips 참조).
function collectWikiLinks(answer: RoutedAnswer): WikiLinkRef[] {
  const links: WikiLinkRef[] = [];
  const push = (link: WikiLinkRef) => {
    if (!link.slug) return;
    if (!links.some((l) => l.slug === link.slug && l.scope === link.scope)) links.push(link);
  };
  const fromWiki = (wiki: WikiAnswer | null | undefined) => {
    if (!wiki) return;
    for (const link of wiki.wiki_links ?? []) push(link);
    if (!(wiki.wiki_links ?? []).length) {
      for (const s of wiki.sources ?? []) {
        push({ slug: s.page_slug, title: s.title, scope: s.source_scope ?? "company" });
      }
    }
  };
  fromWiki(answer.wiki);
  for (const part of answer.parts ?? []) fromWiki(part.routed?.wiki);
  return links.slice(0, 8); // 버블 하단이 칩으로 도배되지 않게 상한.
}

function formatAgentAnswer(answer: RoutedAnswer) {
  const warnings = answer.warnings.length ? `\n\n경고: ${answer.warnings.join(", ")}` : "";
  if (answer.mode === "decomposed") return `${formatDecomposed(answer)}${warnings}`;
  if (answer.wiki?.answer) return `${answer.wiki.answer}${warnings}`;
  if (answer.structured?.message) return `${answer.structured.message}${warnings}`;
  if (answer.structured) return `${formatStructured(answer.structured)}${warnings}`;
  if (answer.structured_parts.length) {
    const parts = answer.structured_parts
      .map((part) => `[${part.source_scope}]\n${formatStructured(part.result)}`)
      .join("\n\n");
    return `${parts}${warnings}`;
  }
  // mode='agent_work': a detected command was queued. Show the intake message
  // (queued/executed status) instead of a bare "agent_work 결과 없음".
  if (answer.agent_work?.message) return `${answer.agent_work.message}${warnings}`;
  // mode='decomposed': the synthesized multi-part answer body.
  if (answer.synthesized_body?.answer) return `${answer.synthesized_body.answer}${warnings}`;
  return `${answer.mode} 결과 없음${warnings}`;
}

// Sub-answers of a decomposed answer that queued an AgentWork item (command
// fragments). D-FE2 renders one chip per entry; D-FE1 lists each in the text so
// nothing is silently collapsed when several commands land in one input.
function queuedCommandParts(answer: RoutedAnswer): SubAnswer[] {
  return (answer.parts ?? []).filter((p) => Boolean(p.agent_work_id));
}

/** Flatten a decomposed (compound-question) result into chat text. The synthesized
 *  body combines the read sub-answers; action sub-parts (which synthesis ignores) and
 *  errored sub-parts are always listed so nothing is silently dropped. Every command
 *  fragment (case2: multiple commands in one input) is surfaced with its title, live
 *  state label, and a deeplink text to the queued item. */
function formatDecomposed(answer: RoutedAnswer): string {
  const head = answer.synthesized_body?.answer?.trim();
  const lines: string[] = [];
  if (head) lines.push(head);
  (answer.parts ?? []).forEach((part, i) => {
    const q = part.text ? `${i + 1}. ${part.text}` : `${i + 1}.`;
    if (part.agent_work_id) {
      // Badge SoR is `state` (NOT policy_outcome — a held item is request_more_data).
      const stateLabel = part.state
        ? (DELEGATE_STATE_LABEL[part.state] ?? part.state)
        : "검토 대기";
      const title = part.title?.trim() ? ` · ${part.title.trim()}` : "";
      const recipient = part.recipient?.trim() ? ` · 수신 ${part.recipient.trim()}` : "";
      lines.push(
        `${q}\n→ [${stateLabel}] Agent Work에 등록됨${title}${recipient}\n   ${agentWorkDeeplinkText(part.agent_work_id!)}`,
      );
    } else if (part.error) {
      lines.push(`${q}\n→ (처리 실패: ${part.error})`);
    } else if (!head) {
      // No synthesis to lean on — surface each sub-answer body verbatim.
      lines.push(`${q}\n${part.routed ? formatAgentAnswer(part.routed) : "(결과 없음)"}`);
    }
  });
  return lines.join("\n\n") || "decomposed 결과 없음";
}

// Deeplink TEXT (not a live href — chat messages persist as plain text) pointing at
// the queued fragment's review row. The interactive link lives in the chip (D-FE2);
// this keeps the persisted transcript self-describing. Points at /checklist?work_id
// (the review queue auto-selects it), matching the chip href.
function agentWorkDeeplinkText(workId: string): string {
  return `열기: /checklist?work_id=${workId}`;
}

// C7 칩 재수화: 조각 메타를 서버에 따로 저장하지 않는다 — decomposed 답변 텍스트에
// 이미 영속된 위 딥링크(agentWorkDeeplinkText)가 work_id의 SoR이다. 히스토리 로드
// 시 agent 메시지에서 work_id를 파싱해 최소 SubAnswer로 복원하면, 기존 배지 배치
// 폴링(?ids=)이 첫 tick에 라이브 state/title/recipient/reason을 채운다(FragmentChip은
// liveItem 우선). terminal state면 그 1회로 폴링이 멈춘다. 단일-명령(agent_work 모드)
// ack 메시지는 딥링크 텍스트를 넣지 않으므로 여기서도 칩이 생기지 않는다(라이브
// 렌더와 동일 모양). 스키마/서버 변경 없음. 구형 딥링크 `/agent-work#<id>`(PR #607
// 이전에 영속된 메시지)도 함께 매칭한다. 정규식 SoR은 마크다운 렌더의 세그먼트
// 분리와 공유하는 components/agent/message-markdown.tsx::FRAGMENT_DEEPLINK_RE다.

function rehydratedFragments(messages: AgentChatMessage[]): Record<string, SubAnswer[]> {
  const map: Record<string, SubAnswer[]> = {};
  for (const message of messages) {
    if (message.role !== "agent" || !message.text) continue;
    const seen = new Set<string>();
    const parts: SubAnswer[] = [];
    for (const match of message.text.matchAll(FRAGMENT_DEEPLINK_RE)) {
      const workId = match[1];
      if (seen.has(workId)) continue;
      seen.add(workId);
      parts.push({
        sub_question_id: `rehydrated-${workId}`,
        routed: null,
        grounded: false,
        agent_work_id: workId,
        error: null,
      });
    }
    if (parts.length) map[message.message_id] = parts;
  }
  return map;
}

function formatDate(value: string) {
  return new Date(value).toLocaleString("ko-KR");
}
