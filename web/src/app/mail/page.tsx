"use client";

import {
  ChevronLeft,
  Download,
  Inbox,
  Loader2,
  Mail,
  MailOpen,
  Paperclip,
  PenSquare,
  RefreshCw,
  Reply,
  Search,
  Send,
  Sparkles,
  Star,
  Trash2,
  Undo2,
  Users,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type AllowlistEntry,
  type CanonicalEmail,
  type EmailAttachmentRef,
  type MailBackendName,
  type MailInboxResponse,
  type PersonalMailDetail,
  type PersonalMailItem,
  type PersonalMailResponse,
  type TeamMember,
  type WikiMailGrounding,
} from "@/lib/api";
import Link from "next/link";
import {
  Banner,
  Card,
  Chip,
  PageHeader,
  Skeleton,
  Toolbar,
  ToolbarButton,
  cx,
  inputClass,
  inputStyle,
} from "@/components/ui";
import {
  MailRichEditor,
  type MailRichEditorHandle,
  htmlToPlainText,
} from "@/components/mail/rich-editor";
import { MailHtmlBody, proxyMailImages } from "@/components/mail/html-body";

const BACKEND_LABELS: Record<MailBackendName, string> = {
  nova: "Nova",
  acme: "Acme",
  gmail: "Gmail",
};

// 백엔드 슬롯 이름(`nova`/`acme`)은 코드가 어느 어댑터를 탔는지를 가리킬 뿐,
// 사용자가 아는 이름이 아니다. 같은 슬롯에 다른 도메인 메일함을 붙이면(P6.7 멀티 계정,
// 로컬 스텁) 화면에 엉뚱한 회사 이름이 찍힌다. 메일함 주소의 도메인이 실제 소속을
// 말해주므로 그쪽을 우선 쓰고, 주소가 없을 때만 슬롯 이름으로 떨어진다.
const DOMAIN_LABELS: Record<string, string> = {
  "nova.example": "Nova",
  "acme.example": "Acme",
};

function mailboxLabel(backend: MailBackendName, ownerAddr?: string | null): string {
  const domain = (ownerAddr ?? "").split("@")[1]?.toLowerCase() ?? "";
  if (!domain) return BACKEND_LABELS[backend];
  const known = DOMAIN_LABELS[domain];
  if (known) return known;
  const name = domain.split(".")[0] ?? "";
  if (!name) return BACKEND_LABELS[backend];
  return name.charAt(0).toUpperCase() + name.slice(1);
}

const COMPACT_WIDTH = 760;
const PERSONAL_MAILBOX_KEY = "personal";
const ALL_MAILBOX_KEY = "all";
const AUTO_REFRESH_MS = 15_000;

const DATE_FORMAT = new Intl.DateTimeFormat("ko-KR", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});

// 로컬 저장 초안(작성창/AI 초안): private mode 등에서 localStorage가 throw할 수
// 있어 항상 try/catch로 감싸고, 실패해도 UI는 메모리 상태로 계속 동작한다.
function readDraftStorage<T>(key: string): T | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function writeDraftStorage(key: string, value: unknown): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage 비활성/용량 초과 — 메모리 상태로만 계속 동작.
  }
}

function removeDraftStorage(key: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

type FolderKey = "inbox" | "aidraft" | "sent" | "starred" | "attachments" | "trash";

const FOLDERS: ReadonlyArray<{ key: FolderKey; label: string; icon: React.ComponentType<{ className?: string }> }> = [
  { key: "inbox", label: "받은편지함", icon: Inbox },
  { key: "aidraft", label: "AI 초안", icon: Sparkles },
  { key: "sent", label: "보낸편지함", icon: Send },
  { key: "starred", label: "별표", icon: Star },
  { key: "attachments", label: "문서함", icon: Paperclip },
  { key: "trash", label: "휴지통", icon: Trash2 },
];

type Mailbox = {
  key: string;
  label: string;
  kind: "all" | "company" | "personal";
  accountId?: string | null;
  backend?: MailBackendName;
  backendLabel?: string;
  addr?: string | null;
  unread?: number;
  ok?: boolean;
};

type ListVM = {
  key: string;
  subject: string;
  preview: string;
  date: string | null;
  unread: boolean;
  starred: boolean;
  replied: boolean;
  outbound: boolean;
  attachment: boolean;
  attachmentCount: number;
  scope: "company" | "personal";
  backendLabel: string;
};

type View = "list" | "detail" | "compose";

function useCompact() {
  const [compact, setCompact] = useState(false);
  useEffect(() => {
    function sync() {
      setCompact(window.innerWidth < COMPACT_WIDTH);
    }
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);
  return compact;
}

export default function MailPage() {
  const compact = useCompact();

  const [companyData, setCompanyData] = useState<MailInboxResponse | null>(null);
  const [personalData, setPersonalData] = useState<PersonalMailResponse | null>(null);
  const [personalAvailable, setPersonalAvailable] = useState(false);

  const [mailboxKey, setMailboxKey] = useState<string>(ALL_MAILBOX_KEY);
  const [folder, setFolder] = useState<FolderKey>("inbox");
  const [sort, setSort] = useState<"newest" | "oldest">("newest");

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [view, setView] = useState<View>("list");

  const [personalDetail, setPersonalDetail] = useState<PersonalMailDetail | null>(null);
  const [personalDetailLoading, setPersonalDetailLoading] = useState(false);
  const [personalDetailError, setPersonalDetailError] = useState(false);
  // Company-mail single detail (full body + attachments, fetched on open; the
  // backend auto-marks it read). null = fall back to the inbox row meanwhile.
  const [companyDetail, setCompanyDetail] = useState<CanonicalEmail | null>(null);
  // HTML-only 메일은 리스트 row에 본문이 없어서, 상세 fetch 상태를 보여주지 않으면
  // 로딩 중/실패가 "본문 없음"으로 잘못 보인다.
  const [companyDetailLoading, setCompanyDetailLoading] = useState(false);
  const [companyDetailError, setCompanyDetailError] = useState(false);
  const [detailRetryTick, setDetailRetryTick] = useState(0);
  // Conversation thread (address-pair history with the other party).
  const [conversation, setConversation] = useState<CanonicalEmail[] | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  // 작성창 prefill(답장 등). null = 빈 새 메일.
  const [composePrefill, setComposePrefill] = useState<ComposePrefill | null>(null);

  const [query, setQuery] = useState("");
  const [activeQuery, setActiveQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const autoRefreshingRef = useRef(false);
  const pullRefreshingRef = useRef(false);
  // "더 보기": 기본 300통, 요청 시 서버 상한(500)까지 한 단계 확장.
  const [fetchLimit, setFetchLimit] = useState(300);
  const fetchLimitRef = useRef(300);
  // Gmail 미지원 노드(회사 노드 등)에서 15초마다 헛 요청을 반복하지 않도록,
  // 한 번 실패하면 silent 리로드에서는 건너뛴다(수동 새로고침은 재시도).
  const personalAvailableRef = useRef<boolean | null>(null);
  // 메일 본문은 불변이라 상세는 세션 내 캐시해 재열람을 즉시 표시한다.
  // 대화 스레드는 새 메일로 자랄 수 있어 리스트 리로드 때마다 비운다.
  const detailCacheRef = useRef(new Map<string, CanonicalEmail>());
  const conversationCacheRef = useRef(new Map<string, CanonicalEmail[]>());

  const load = useCallback(async (search: string, options?: { silent?: boolean }) => {
    // Mount owns the first-load skeleton; load() is the reload path (search/refresh).
    const showRefreshing = !options?.silent;
    if (showRefreshing) setRefreshing(true);
    setError(null);
    const trimmed = search.trim() || undefined;
    const skipPersonal = Boolean(options?.silent) && personalAvailableRef.current === false;
    // Company inbox + best-effort personal Gmail are independently owner-scoped;
    // we fetch both and merge in the client (display-only, no scope change).
    const [companyRes, personalRes] = await Promise.allSettled([
      api.listMailInbox({ limit: fetchLimitRef.current, search: trimmed }),
      skipPersonal
        ? Promise.reject(new Error("skipped"))
        : api.listPersonalMail({ limit: 80, search: trimmed }),
    ]);

    if (companyRes.status === "fulfilled") {
      setCompanyData(companyRes.value);
      conversationCacheRef.current.clear();
    } else {
      setError(
        companyRes.reason instanceof Error
          ? companyRes.reason.message
          : "메일 인박스를 불러오지 못했습니다.",
      );
    }

    if (personalRes.status === "fulfilled") {
      setPersonalData(personalRes.value);
      setPersonalAvailable(true);
      personalAvailableRef.current = true;
    } else if (!skipPersonal) {
      // Personal Gmail gate not satisfied (e.g. flag off) — just hide it.
      setPersonalAvailable(false);
      personalAvailableRef.current = false;
    }

    setLoading(false);
    if (showRefreshing) setRefreshing(false);
  }, []);

  useEffect(() => {
    // silent: 첫 로드는 스켈레톤(loading)이 표시를 맡는다. timeout 0은 effect 본문의
    // 동기 setState(react-hooks/set-state-in-effect)를 피하기 위한 지연이다.
    const timer = window.setTimeout(() => void load("", { silent: true }), 0);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible" || view === "compose") return;
      if (autoRefreshingRef.current) return;
      autoRefreshingRef.current = true;
      void load(activeQuery, { silent: true }).finally(() => {
        autoRefreshingRef.current = false;
      });
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [activeQuery, load, view]);

  useEffect(() => {
    function refreshOnVisible() {
      if (document.visibilityState !== "visible" || view === "compose") return;
      void load(activeQuery, { silent: true });
    }
    document.addEventListener("visibilitychange", refreshOnVisible);
    window.addEventListener("focus", refreshOnVisible);
    return () => {
      document.removeEventListener("visibilitychange", refreshOnVisible);
      window.removeEventListener("focus", refreshOnVisible);
    };
  }, [activeQuery, load, view]);

  const refresh = useCallback(async () => {
    await load(activeQuery);
    if (!pullRefreshingRef.current) {
      pullRefreshingRef.current = true;
      void api
        .pullMail()
        .then((res) =>
          // 대부분의 pull은 새 메일이 없다 — 실제로 뭔가 들어왔을 때만 한 번 더 읽어,
          // 수동 새로고침이 상습적으로 인박스를 두 번 당기지 않게 한다.
          res.ingested > 0 ? load(activeQuery, { silent: true }) : undefined,
        )
        .catch(() => {
          // best-effort; direct inbox read above is the source of truth for the UI.
        })
        .finally(() => {
          pullRefreshingRef.current = false;
        });
    }
  }, [activeQuery, load]);

  // Mailbox list: 전체 + configured company backends + 개인 Gmail (if reachable).
  const mailboxes = useMemo<Mailbox[]>(() => {
    const list: Mailbox[] = [{ key: ALL_MAILBOX_KEY, label: "전체", kind: "all" }];
    for (const backend of companyData?.backends ?? []) {
      if (!backend.configured) continue;
      list.push({
        key: `c:${backend.account_id ?? `${backend.backend}:${backend.owner_addr ?? ""}`}`,
        label: backend.owner_addr || BACKEND_LABELS[backend.backend],
        kind: "company",
        accountId: backend.account_id,
        backend: backend.backend,
        backendLabel: mailboxLabel(backend.backend, backend.owner_addr),
        addr: backend.owner_addr,
        unread: backend.unread,
        ok: backend.ok,
      });
    }
    if (personalAvailable) {
      list.push({
        key: PERSONAL_MAILBOX_KEY,
        label: "개인 Gmail",
        kind: "personal",
        unread: 0,
      });
    }
    return list;
  }, [companyData, personalAvailable]);

  // `activeMailbox` falls back to 전체 when the stored key is stale (data reload),
  // so children always receive `activeMailbox.key` rather than the raw stored key.
  const activeMailbox = useMemo(
    () => mailboxes.find((m) => m.key === mailboxKey) ?? mailboxes[0],
    [mailboxes, mailboxKey],
  );

  const isPersonalMailbox = activeMailbox?.kind === "personal";

  // Per-folder counts for the active mailbox (company source only).
  const folderCounts = useMemo<Record<FolderKey, number>>(() => {
    const counts: Record<FolderKey, number> = {
      inbox: 0,
      aidraft: 0,
      sent: 0,
      starred: 0,
      attachments: 0,
      trash: 0,
    };
    if (isPersonalMailbox) {
      counts.inbox = personalData?.items.length ?? 0;
      return counts;
    }
    for (const item of companyData?.items ?? []) {
      if (!matchesMailbox(item, activeMailbox)) continue;
      for (const f of FOLDERS) {
        if (matchesFolder(item, f.key)) counts[f.key] += 1;
      }
    }
    return counts;
  }, [companyData, personalData, activeMailbox, isPersonalMailbox]);

  // Rows to render for the active mailbox + folder.
  const rows = useMemo<ListVM[]>(() => {
    let vms: ListVM[];
    if (isPersonalMailbox) {
      vms = (personalData?.items ?? []).map(personalToVM);
    } else {
      vms = (companyData?.items ?? [])
        .filter((item) => matchesMailbox(item, activeMailbox) && matchesFolder(item, folder))
        .map(companyToVM);
    }
    const sorted = [...vms].sort((a, b) => {
      const at = a.date ? new Date(a.date).getTime() : 0;
      const bt = b.date ? new Date(b.date).getTime() : 0;
      return sort === "newest" ? bt - at : at - bt;
    });
    return sorted;
  }, [companyData, personalData, activeMailbox, folder, sort, isPersonalMailbox]);

  const selectedCompanyItem = useMemo(() => {
    if (isPersonalMailbox) return null;
    return companyData?.items.find((item) => companyKey(item) === selectedKey) ?? null;
  }, [companyData, selectedKey, isPersonalMailbox]);

  // Personal detail fetch (degraded projection — subject/snippet/body only).
  // The loading flag is raised in openRow(), so this effect never calls
  // setState synchronously in its body.
  useEffect(() => {
    if (!isPersonalMailbox || !selectedKey || view !== "detail") return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await api.getPersonalMail(selectedKey);
        if (!cancelled) {
          setPersonalDetail(res);
          setPersonalDetailError(res === null);
        }
      } catch {
        if (!cancelled) {
          setPersonalDetail(null);
          setPersonalDetailError(true);
        }
      } finally {
        if (!cancelled) setPersonalDetailLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isPersonalMailbox, selectedKey, view]);

  // Company-mail detail fetch: full body + attachments from the backend, which
  // also auto-marks the message read. The inbox row shows immediately; this
  // upgrades it. gmail stays on the personal path (read-only).
  useEffect(() => {
    if (isPersonalMailbox || view !== "detail" || !selectedKey) return;
    const item = companyData?.items.find((it) => companyKey(it) === selectedKey);
    if (!item) return;
    // 재열람은 캐시로 즉시 표시(본문 불변). 서버 읽음 처리는 첫 열람에서 이미 됐다.
    const cacheKey = `${item.backend}:${item.external_id}:${item.account_id ?? ""}`;
    const cached = detailCacheRef.current.get(cacheKey);
    if (cached) {
      setCompanyDetail(cached);
      setCompanyDetailLoading(false);
      setCompanyDetailError(false);
      if (!item.read) applyCompanyFlags(item.backend, item.external_id, item.account_id, { read: true });
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const res = await api.getMailMessage(item.backend, item.external_id, item.account_id);
        if (cancelled) return;
        detailCacheRef.current.set(cacheKey, res);
        // 무한 세션에서의 메모리 상한 — 가장 단순한 캡(전체 비우기)으로 충분하다.
        if (detailCacheRef.current.size > 80) detailCacheRef.current.clear();
        setCompanyDetail(res);
        setCompanyDetailLoading(false);
        setCompanyDetailError(false);
        if (!item.read) applyCompanyFlags(item.backend, item.external_id, item.account_id, { read: true });
      } catch {
        // 리스트 row에 본문이 있으면 그걸로 계속 보여주되, 없으면 에러+재시도 표시.
        if (cancelled) return;
        setCompanyDetailLoading(false);
        setCompanyDetailError(true);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPersonalMailbox, selectedKey, view, detailRetryTick]);

  // Conversation thread fetch: the message history with the other party
  // (inbound → sender, outbound → first recipient). Address-pair, not RFC.
  useEffect(() => {
    if (isPersonalMailbox || view !== "detail" || !selectedKey) return;
    const item = companyData?.items.find((it) => companyKey(it) === selectedKey);
    if (!item) return;
    const contact = extractEmail(
      item.direction === "outbound" ? item.to_addr[0] ?? "" : item.from_addr,
    );
    const cacheKey = `${item.backend}:${item.account_id ?? ""}:${contact}`;
    const cached = contact ? conversationCacheRef.current.get(cacheKey) : undefined;
    if (cached) {
      setConversation(cached);
      return;
    }
    let cancelled = false;
    void (async () => {
      if (!contact) {
        if (!cancelled) setConversation([]);
        return;
      }
      try {
        const res = await api.getMailConversation(item.backend, contact, item.account_id);
        conversationCacheRef.current.set(cacheKey, res);
        if (!cancelled) setConversation(res);
      } catch {
        if (!cancelled) setConversation([]);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPersonalMailbox, selectedKey, view]);

  // Reflect a flag mutation in the list (+ unread badge) and the open detail.
  function applyCompanyFlags(
    backend: MailBackendName,
    externalId: string,
    accountId: string | null,
    flags: { read?: boolean; starred?: boolean; trashed?: boolean },
  ) {
    // 상세 캐시도 같은 플래그로 갱신 — 재열람이 stale read/star/trashed를 보여
    // 복원/읽음 버튼이 어긋나지 않게 한다.
    const cacheKey = `${backend}:${externalId}:${accountId ?? ""}`;
    const cachedDetail = detailCacheRef.current.get(cacheKey);
    if (cachedDetail) detailCacheRef.current.set(cacheKey, { ...cachedDetail, ...flags });
    setCompanyData((prev) => {
      if (!prev) return prev;
      let unreadDelta = 0;
      const items = prev.items.map((it) => {
        const sameMessage =
          it.backend === backend && it.external_id === externalId && it.account_id === accountId;
        if (!sameMessage) return it;
        if (flags.read != null && it.read !== flags.read) unreadDelta += flags.read ? 1 : -1;
        return { ...it, ...flags };
      });
      return { ...prev, items, unread: Math.max(0, prev.unread - unreadDelta) };
    });
    setCompanyDetail((prev) =>
      prev &&
      prev.backend === backend &&
      prev.external_id === externalId &&
      prev.account_id === accountId
        ? { ...prev, ...flags }
        : prev,
    );
    setConversation((prev) =>
      prev?.map((it) =>
        it.backend === backend && it.external_id === externalId && it.account_id === accountId
          ? { ...it, ...flags }
          : it,
      ) ?? prev,
    );
  }

  async function toggleStar(item: CanonicalEmail) {
    const next = !item.starred;
    applyCompanyFlags(item.backend, item.external_id, item.account_id, { starred: next });
    try {
      await api.patchMailFlags(item.backend, item.external_id, { starred: next }, item.account_id);
    } catch {
      applyCompanyFlags(item.backend, item.external_id, item.account_id, { starred: !next });
    }
  }

  async function toggleRead(item: CanonicalEmail) {
    const next = !item.read;
    applyCompanyFlags(item.backend, item.external_id, item.account_id, { read: next });
    try {
      await api.patchMailFlags(item.backend, item.external_id, { read: next }, item.account_id);
    } catch {
      applyCompanyFlags(item.backend, item.external_id, item.account_id, { read: !next });
    }
  }

  async function restoreSelected(item: CanonicalEmail) {
    setActionBusy(true);
    applyCompanyFlags(item.backend, item.external_id, item.account_id, { trashed: false });
    setSelectedKey(null);
    setView("list");
    try {
      await api.trashMailMessage(item.backend, item.external_id, true, item.account_id);
    } catch {
      applyCompanyFlags(item.backend, item.external_id, item.account_id, { trashed: true });
      setError("복원 실패");
    } finally {
      setActionBusy(false);
    }
  }

  async function trashSelected(item: CanonicalEmail) {
    setActionBusy(true);
    // 낙관적 로컬 반영: 300+80통 전체 리로드 대신 해당 row만 trashed로 옮긴다.
    applyCompanyFlags(item.backend, item.external_id, item.account_id, { trashed: true });
    setSelectedKey(null);
    setView("list");
    try {
      await api.trashMailMessage(item.backend, item.external_id, false, item.account_id);
    } catch {
      applyCompanyFlags(item.backend, item.external_id, item.account_id, { trashed: false });
      setError("휴지통 이동 실패");
    } finally {
      setActionBusy(false);
    }
  }

  // 스레드 아코디언용: 해당 메일의 전체 본문을 가져오고(서버가 읽음 처리)
  // 목록/스레드의 읽음 배지도 맞춘다. 상세 화면 전환은 하지 않는다.
  async function loadConversationMessage(item: CanonicalEmail): Promise<CanonicalEmail> {
    const res = await api.getMailMessage(item.backend, item.external_id, item.account_id);
    if (!item.read) applyCompanyFlags(item.backend, item.external_id, item.account_id, { read: true });
    return res;
  }

  function selectMailbox(key: string) {
    setMailboxKey(key);
    setFolder("inbox");
    setSelectedKey(null);
    setView("list");
  }

  function selectFolder(key: FolderKey) {
    setFolder(key);
    setSelectedKey(null);
    setView("list");
  }

  function openRow(key: string) {
    setSelectedKey(key);
    setView("detail");
    if (isPersonalMailbox) {
      setPersonalDetail(null);
      setPersonalDetailError(false);
      setPersonalDetailLoading(true);
    } else {
      setCompanyDetail(null);
      setConversation(null);
      setCompanyDetailLoading(true);
      setCompanyDetailError(false);
    }
  }

  function submitSearch(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = query.trim();
    setActiveQuery(next);
    void load(next);
  }

  const fromOptions = useMemo(() => deriveFromOptions(companyData), [companyData]);
  const composeAvailable = Boolean(companyData?.send_enabled) && fromOptions.length > 0;

  // 보내는 사람 고정: 답장은 그 메일을 받은 메일함으로, 새 메일은 지금 보고 있는
  // 메일함으로 고정한다(확인 표시만). 전체함/개인함에서의 새 메일만 선택 가능.
  const composeLockedFrom = useMemo(() => {
    const candidate = composePrefill?.replyToId
      ? composePrefill.fromAddr
      : activeMailbox?.kind === "company"
        ? activeMailbox.addr
        : undefined;
    if (!candidate) return undefined;
    return fromOptions.find((option) => option.address === candidate)?.address;
  }, [composePrefill, activeMailbox, fromOptions]);

  // Unread count follows the active mailbox (전체 = company aggregate, a single
  // mailbox = its own unread, 개인 Gmail = 0) so it matches the listed rows.
  const headerUnread =
    activeMailbox?.kind === "all" ? (companyData?.unread ?? 0) : (activeMailbox?.unread ?? 0);
  const subtitle = loading
    ? "불러오는 중"
    : `${rows.length}개 · 안 읽음 ${headerUnread} · 메일함 ${Math.max(mailboxes.length - 1, 0)}`;

  const headerControls = (
    <Toolbar className="w-full justify-end">
      {compact ? (
        // 모바일: 어느 메일함(2fe/biz/개인 Gmail…)을 보는지 새로고침 옆에서 바로 선택.
        <select
          aria-label="메일함 선택"
          className={cx(inputClass, "h-[44px] min-w-0 flex-1")}
          onChange={(event) => selectMailbox(event.target.value)}
          style={inputStyle}
          value={activeMailbox?.key ?? ALL_MAILBOX_KEY}
        >
          {mailboxes.map((m) => (
            <option key={m.key} value={m.key}>
              {m.label}
              {m.unread ? ` (${m.unread})` : ""}
            </option>
          ))}
        </select>
      ) : null}
      {compact && composeAvailable ? (
        <ToolbarButton
          className="min-h-[44px]"
          icon={<PenSquare size={15} />}
          onClick={() => {
            setComposePrefill(null);
            setView("compose");
            setSelectedKey(null);
          }}
          type="button"
        >
          쓰기
        </ToolbarButton>
      ) : null}
      <ToolbarButton
        disabled={refreshing}
        icon={<RefreshCw className={cx("h-4 w-4", refreshing && "animate-spin")} />}
        onClick={() => void refresh()}
        type="button"
      >
        새로고침
      </ToolbarButton>
    </Toolbar>
  );

  const sidebar = (
    <MailSidebar
      activeFolder={folder}
      activeMailboxKey={activeMailbox?.key ?? ALL_MAILBOX_KEY}
      composeAvailable={composeAvailable}
      composeActive={view === "compose"}
      folderCounts={folderCounts}
      isPersonalMailbox={isPersonalMailbox}
      mailboxes={mailboxes}
      onCompose={() => {
        setComposePrefill(null);
        setView("compose");
        setSelectedKey(null);
      }}
      onSelectFolder={selectFolder}
      onSelectMailbox={selectMailbox}
    />
  );

  // Detail only renders while its selected mail still exists; otherwise fall back
  // to the list (a refresh dropped the selected mail, or the personal mailbox
  // became unavailable while its detail was open). Pure render-time derivation —
  // no effect/setState, so it can never strand the user on an empty detail pane.
  const selectionPresent = isPersonalMailbox
    ? Boolean(selectedKey && personalData?.items.some((item) => item.doc_id === selectedKey))
    : Boolean(selectedCompanyItem);
  const showCompose = view === "compose";
  const showDetail = view === "detail" && selectionPresent;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader title="메일" subtitle={subtitle} right={headerControls} />

      <div className="min-h-0 flex-1 overflow-hidden px-3 py-3 sm:px-5">
        <div className="flex h-full min-h-0 gap-3">
          {!compact ? <div className="w-[232px] shrink-0 overflow-y-auto">{sidebar}</div> : null}

          <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-3">
            {compact ? (
              <CompactControls
                activeFolder={folder}
                folderCounts={folderCounts}
                isPersonalMailbox={isPersonalMailbox}
                onSelectFolder={selectFolder}
              />
            ) : null}

            {error ? <Banner tone="fail" title="메일 로드 실패">{error}</Banner> : null}

            {showCompose ? (
              <ComposeView
                key={composePrefill?.replyToId ?? "new"}
                fromOptions={fromOptions}
                lockedFrom={composeLockedFrom}
                prefill={composePrefill ?? undefined}
                onClose={() => setView("list")}
                onSent={() => void load(activeQuery, { silent: true })}
              />
            ) : folder === "aidraft" ? (
              <AiDraftBatchView
                items={companyData?.items ?? []}
                mailbox={activeMailbox}
                isPersonalMailbox={isPersonalMailbox}
                fromOptions={fromOptions}
                loading={loading}
                onRefresh={() => void refresh()}
              />
            ) : showDetail ? (
              <DetailPane
                actionBusy={actionBusy}
                companyItem={companyDetail ?? selectedCompanyItem}
                companyLoading={companyDetailLoading}
                companyError={companyDetailError}
                onRetryCompany={() => {
                  setCompanyDetailLoading(true);
                  setCompanyDetailError(false);
                  setDetailRetryTick((tick) => tick + 1);
                }}
                conversation={conversation}
                isPersonal={isPersonalMailbox}
                onBack={() => setView("list")}
                onLoadConversationMessage={loadConversationMessage}
                onReply={
                  composeAvailable
                    ? (item) => {
                        setComposePrefill(buildReplyPrefill(item, fromOptions));
                        setView("compose");
                      }
                    : undefined
                }
                onToggleRead={toggleRead}
                onToggleStar={toggleStar}
                onTrash={trashSelected}
                onRestore={restoreSelected}
                personalDetail={personalDetail}
                personalError={personalDetailError}
                personalLoading={personalDetailLoading}
              />
            ) : (
              <ListPane
                activeQuery={activeQuery}
                folder={folder}
                isPersonalMailbox={isPersonalMailbox}
                loading={loading}
                mailboxLabel={activeMailbox?.label ?? "전체"}
                onLoadMore={
                  // 검색 중엔 provider가 전체 메일함을 이미 뒤지므로 확장 불필요.
                  !activeQuery && !isPersonalMailbox && fetchLimit < 500 && (companyData?.items.length ?? 0) >= fetchLimit
                    ? () => {
                        setFetchLimit(500);
                        fetchLimitRef.current = 500;
                        void load(activeQuery);
                      }
                    : undefined
                }
                onOpen={openRow}
                onSearchChange={setQuery}
                onSubmitSearch={submitSearch}
                onToggleSort={setSort}
                query={query}
                refreshing={refreshing}
                rows={rows}
                selectedKey={selectedKey}
                sort={sort}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- sidebar */

function MailSidebar({
  activeFolder,
  activeMailboxKey,
  composeAvailable,
  composeActive,
  folderCounts,
  isPersonalMailbox,
  mailboxes,
  onCompose,
  onSelectFolder,
  onSelectMailbox,
}: {
  activeFolder: FolderKey;
  activeMailboxKey: string;
  composeAvailable: boolean;
  composeActive: boolean;
  folderCounts: Record<FolderKey, number>;
  isPersonalMailbox: boolean;
  mailboxes: Mailbox[];
  onCompose: () => void;
  onSelectFolder: (key: FolderKey) => void;
  onSelectMailbox: (key: string) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      {composeAvailable ? (
        <button
          className="flex min-h-[44px] items-center justify-center gap-2 rounded-[8px] px-3 text-[var(--text-body-sm)] font-bold transition-colors"
          onClick={onCompose}
          style={{
            background: composeActive ? "var(--color-progress)" : "var(--color-text-strong)",
            color: "var(--color-card)",
          }}
          type="button"
        >
          <PenSquare className="h-4 w-4" />
          메일 쓰기
        </button>
      ) : null}

      <nav className="grid gap-0.5">
        {FOLDERS.map((f) => {
          const Icon = f.icon;
          const active = f.key === activeFolder;
          const count = folderCounts[f.key];
          const disabled = isPersonalMailbox && f.key !== "inbox";
          return (
            <button
              aria-current={active ? "page" : undefined}
              className={cx(
                "flex min-h-[40px] items-center gap-2.5 rounded-[7px] px-3 text-left text-[var(--text-body-sm)] transition-colors",
                disabled && "opacity-40",
              )}
              disabled={disabled}
              key={f.key}
              onClick={() => onSelectFolder(f.key)}
              style={{
                background: active ? "var(--color-sidebar-active)" : "transparent",
                color: active ? "var(--color-text-strong)" : "var(--color-text-body)",
                fontWeight: active ? 700 : 500,
              }}
              type="button"
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span className="min-w-0 flex-1 truncate">{f.label}</span>
              {count > 0 ? (
                <span
                  className="shrink-0 rounded-full px-1.5 text-[10px] font-bold"
                  style={{ background: "var(--color-app-canvas)", color: "var(--color-text-muted)" }}
                >
                  {count}
                </span>
              ) : null}
            </button>
          );
        })}
      </nav>

      <div className="pt-1">
        <div
          className="mb-1 px-3 text-[10px] font-bold uppercase tracking-wide"
          style={{ color: "var(--color-text-muted)" }}
        >
          메일함
        </div>
        <div className="grid gap-0.5">
          {mailboxes.map((m) => {
            const active = m.key === activeMailboxKey;
            return (
              <button
                aria-current={active ? "page" : undefined}
                className="flex min-h-[40px] items-center gap-2 rounded-[7px] px-3 text-left text-[var(--text-body-sm)] transition-colors"
                key={m.key}
                onClick={() => onSelectMailbox(m.key)}
                style={{
                  background: active ? "var(--color-sidebar-active)" : "transparent",
                  color: active ? "var(--color-text-strong)" : "var(--color-text-body)",
                  fontWeight: active ? 700 : 500,
                }}
                type="button"
                title={m.label}
              >
                <Mail className="h-3.5 w-3.5 shrink-0" style={{ color: "var(--color-text-muted)" }} />
                <span className="min-w-0 flex-1 truncate">{m.label}</span>
                {/* 주소 도메인(@nova.example/@acme.example)이 곧 제공자라 별도 백엔드 라벨은 생략 */}
                {m.kind === "company" && m.ok === false ? (
                  <Chip tone="warn">오류</Chip>
                ) : m.unread ? (
                  <span
                    className="shrink-0 rounded-full px-1.5 text-[10px] font-bold"
                    style={{ background: "var(--color-attention)", color: "var(--color-card)" }}
                  >
                    {m.unread}
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function CompactControls({
  activeFolder,
  folderCounts,
  isPersonalMailbox,
  onSelectFolder,
}: {
  activeFolder: FolderKey;
  folderCounts: Record<FolderKey, number>;
  isPersonalMailbox: boolean;
  onSelectFolder: (key: FolderKey) => void;
}) {
  // 메일함 선택/쓰기는 페이지 헤더(새로고침 옆)로 올라갔다 — 여기는 폴더 칩만.
  return (
    <div className="grid gap-2">
      <div className="flex gap-1.5 overflow-x-auto pb-1">
        {FOLDERS.map((f) => {
          const active = f.key === activeFolder;
          const disabled = isPersonalMailbox && f.key !== "inbox";
          if (disabled) return null;
          const count = folderCounts[f.key];
          return (
            <button
              className="flex min-h-[36px] shrink-0 items-center gap-1.5 rounded-full px-3 text-[var(--text-meta)] font-semibold transition-colors"
              key={f.key}
              onClick={() => onSelectFolder(f.key)}
              style={{
                background: active ? "var(--color-sidebar-active)" : "var(--color-card)",
                border: "1px solid var(--color-divider)",
                color: active ? "var(--color-text-strong)" : "var(--color-text-body)",
              }}
              type="button"
            >
              {f.label}
              {count > 0 ? <span style={{ color: "var(--color-text-muted)" }}>{count}</span> : null}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------- list */

function ListPane({
  activeQuery,
  folder,
  isPersonalMailbox,
  loading,
  mailboxLabel,
  onLoadMore,
  onOpen,
  onSearchChange,
  onSubmitSearch,
  onToggleSort,
  query,
  refreshing,
  rows,
  selectedKey,
  sort,
}: {
  activeQuery: string;
  folder: FolderKey;
  isPersonalMailbox: boolean;
  loading: boolean;
  mailboxLabel: string;
  onLoadMore?: () => void;
  onOpen: (key: string) => void;
  onSearchChange: (value: string) => void;
  onSubmitSearch: (event: React.FormEvent<HTMLFormElement>) => void;
  onToggleSort: (value: "newest" | "oldest") => void;
  query: string;
  refreshing: boolean;
  rows: ListVM[];
  selectedKey: string | null;
  sort: "newest" | "oldest";
}) {
  const folderLabel = FOLDERS.find((f) => f.key === folder)?.label ?? "받은편지함";
  return (
    <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div
        className="flex shrink-0 flex-wrap items-center justify-between gap-2 px-3 py-2.5"
        style={{ borderBottom: "1px solid var(--color-divider)" }}
      >
        <div className="flex min-w-0 items-center gap-2">
          <span
            className="truncate text-[var(--text-body-sm)] font-bold"
            style={{ color: "var(--color-text-strong)" }}
          >
            {isPersonalMailbox ? "받은편지함" : folderLabel}
          </span>
          <Chip tone="muted">{mailboxLabel}</Chip>
          {activeQuery ? <Chip tone="neutral">{activeQuery}</Chip> : null}
        </div>
        <div className="flex items-center gap-2">
          <form className="relative" onSubmit={onSubmitSearch}>
            <label className="sr-only" htmlFor="mail-search">
              메일 검색
            </label>
            <Search
              aria-hidden
              className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2"
              style={{ color: "var(--color-text-muted)" }}
            />
            <input
              className={cx(inputClass, "h-9 w-[150px] pl-8 text-[var(--text-body-sm)] sm:w-[220px]")}
              id="mail-search"
              onChange={(event) => onSearchChange(event.target.value)}
              placeholder="전체 메일 검색"
              style={inputStyle}
              title="제목·보낸사람·본문을 메일함 전체에서 검색합니다"
              value={query}
            />
          </form>
          <select
            aria-label="정렬"
            className={cx(inputClass, "h-9 text-[var(--text-body-sm)]")}
            onChange={(event) => onToggleSort(event.target.value as "newest" | "oldest")}
            style={inputStyle}
            value={sort}
          >
            <option value="newest">최신순</option>
            <option value="oldest">오래된순</option>
          </select>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {loading && !refreshing && !rows.length ? (
          <LoadingRows />
        ) : rows.length ? (
          <div className="divide-y divide-[color:var(--color-divider)]">
            {rows.map((row) => (
              <MailRow
                active={row.key === selectedKey}
                key={row.key}
                onSelect={() => onOpen(row.key)}
                row={row}
              />
            ))}
            {onLoadMore ? (
              <div className="flex justify-center px-3 py-3">
                <ToolbarButton
                  className="min-h-[44px]"
                  disabled={refreshing}
                  onClick={onLoadMore}
                  type="button"
                >
                  이전 메일 더 보기
                </ToolbarButton>
              </div>
            ) : null}
          </div>
        ) : (
          <EmptyInbox hasQuery={Boolean(activeQuery)} />
        )}
      </div>
    </Card>
  );
}

function MailRow({
  active,
  onSelect,
  row,
}: {
  active: boolean;
  onSelect: () => void;
  row: ListVM;
}) {
  const unread = row.unread;
  const background = active
    ? "var(--color-sidebar-active)"
    : unread
      ? "color-mix(in srgb, var(--color-attention) 12%, var(--color-card))"
      : "color-mix(in srgb, var(--color-card) 46%, transparent)";
  return (
    <button
      aria-pressed={active || undefined}
      className={cx(
        "relative block w-full px-3 py-2.5 text-left transition-colors hover:bg-[color-mix(in_srgb,var(--color-sidebar-active)_66%,transparent)]",
        unread ? "pl-4" : "opacity-65 hover:opacity-100",
      )}
      data-read-state={unread ? "unread" : "read"}
      onClick={onSelect}
      style={{
        background,
        boxShadow: unread ? "inset 0 0 0 1px color-mix(in srgb, var(--color-attention) 18%, transparent)" : undefined,
      }}
      type="button"
    >
      {unread ? (
        <span
          aria-hidden
          className="absolute left-0 top-0 h-full w-[4px]"
          style={{ background: "var(--color-attention)" }}
        />
      ) : null}
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-1.5">
            {unread ? (
              <span
                aria-label="안 읽음"
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ background: "var(--color-attention)" }}
              />
            ) : null}
            {row.starred ? (
              <Star className="h-3 w-3 shrink-0" style={{ color: "var(--color-attention)", fill: "var(--color-attention)" }} />
            ) : null}
            <div
              className={cx(
                "truncate text-[var(--text-body-sm)]",
                unread ? "font-extrabold" : "font-normal",
              )}
              style={{ color: unread ? "var(--color-text-strong)" : "var(--color-text-faint)" }}
            >
              {row.subject}
            </div>
          </div>
          <div
            className={cx("mt-0.5 truncate text-[var(--text-meta)]", unread && "font-bold")}
            style={{ color: unread ? "var(--color-text-strong)" : "color-mix(in srgb, var(--color-text-faint) 76%, transparent)" }}
          >
            {row.preview}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div
            className={cx("font-[family-name:var(--font-mono)] text-[10px]", unread ? "font-extrabold" : "font-normal")}
            style={{ color: unread ? "var(--color-text-strong)" : "var(--color-text-faint)" }}
          >
            {formatDate(row.date)}
          </div>
          {row.attachment ? (
            <div
              className="mt-1 flex items-center justify-end gap-0.5 text-[10px]"
              style={{ color: unread ? "var(--color-text-body)" : "var(--color-text-faint)" }}
            >
              <Paperclip className="h-3.5 w-3.5" />
              {row.attachmentCount > 1 ? <span>{row.attachmentCount}</span> : null}
            </div>
          ) : null}
        </div>
      </div>
      <div className="mt-2 flex min-w-0 flex-wrap items-center gap-1.5">
        {row.outbound ? (
          <Chip tone="pass">보냄</Chip>
        ) : (
          <Chip tone={row.unread ? "warn" : "muted"}>{row.unread ? "안읽음" : "읽음"}</Chip>
        )}
        {row.replied && !row.outbound ? (
          <Chip tone="pass">
            <Reply className="h-3 w-3" />
            답변함
          </Chip>
        ) : null}
        <Chip tone={row.scope === "company" ? "neutral" : "pass"}>{row.scope === "company" ? "회사" : "개인"}</Chip>
        <Chip tone="muted">{row.backendLabel}</Chip>
      </div>
    </button>
  );
}

/* ----------------------------------------------------------------- detail */

function DetailPane({
  actionBusy,
  companyItem,
  companyLoading,
  companyError,
  onRetryCompany,
  conversation,
  isPersonal,
  onBack,
  onLoadConversationMessage,
  onReply,
  onToggleRead,
  onToggleStar,
  onTrash,
  onRestore,
  personalDetail,
  personalError,
  personalLoading,
}: {
  actionBusy: boolean;
  companyItem: CanonicalEmail | null;
  companyLoading: boolean;
  companyError: boolean;
  onRetryCompany: () => void;
  conversation: CanonicalEmail[] | null;
  isPersonal: boolean;
  onBack: () => void;
  onLoadConversationMessage: (item: CanonicalEmail) => Promise<CanonicalEmail>;
  onReply?: (item: CanonicalEmail) => void;
  onToggleRead: (item: CanonicalEmail) => void;
  onToggleStar: (item: CanonicalEmail) => void;
  onTrash: (item: CanonicalEmail) => void;
  onRestore: (item: CanonicalEmail) => void;
  personalDetail: PersonalMailDetail | null;
  personalError: boolean;
  personalLoading: boolean;
}) {
  // Company mail supports triage (star/read/trash); gmail stays read-only.
  const canAct = !isPersonal && companyItem != null;
  // 다른 메일로 전환하면 이전 메일에서 내려간 스크롤이 남아 새 메일의 중간(짧은
  // 메일이면 빈 공백)에서 시작한다 — 표시 메일이 바뀌면 맨 위로 리셋한다.
  const detailScrollRef = useRef<HTMLDivElement | null>(null);
  const shownKey = isPersonal
    ? personalDetail?.doc_id ?? null
    : companyItem
      ? companyKey(companyItem)
      : null;
  useEffect(() => {
    detailScrollRef.current?.scrollTo({ top: 0 });
  }, [shownKey]);
  return (
    <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div
        className="flex h-12 shrink-0 items-center gap-2 px-3"
        style={{ borderBottom: "1px solid var(--color-divider)" }}
      >
        <ToolbarButton className="min-h-[44px]" icon={<ChevronLeft size={15} />} onClick={onBack} type="button">
          목록
        </ToolbarButton>
        {canAct && companyItem ? (
          <div className="ml-auto flex items-center gap-1">
            {onReply ? (
              <ToolbarButton
                className="min-h-[44px]"
                icon={<Reply size={15} />}
                onClick={() => onReply(companyItem)}
                type="button"
              >
                답장
              </ToolbarButton>
            ) : null}
            <ToolbarButton
              aria-label={companyItem.starred ? "별표 해제" : "별표"}
              className="min-h-[44px]"
              icon={
                <Star
                  size={15}
                  fill={companyItem.starred ? "var(--color-warn-fg, #d9a400)" : "none"}
                  style={{ color: companyItem.starred ? "var(--color-warn-fg, #d9a400)" : "currentColor" }}
                />
              }
              onClick={() => onToggleStar(companyItem)}
              type="button"
            />
            <ToolbarButton
              className="min-h-[44px]"
              onClick={() => onToggleRead(companyItem)}
              type="button"
            >
              {companyItem.read ? "안 읽음으로" : "읽음으로"}
            </ToolbarButton>
            {companyItem.trashed ? (
              <ToolbarButton
                className="min-h-[44px]"
                disabled={actionBusy}
                icon={<Undo2 size={15} />}
                onClick={() => onRestore(companyItem)}
                type="button"
              >
                복원
              </ToolbarButton>
            ) : (
              <ToolbarButton
                aria-label="휴지통"
                className="min-h-[44px]"
                disabled={actionBusy}
                icon={<Trash2 size={15} />}
                onClick={() => onTrash(companyItem)}
                type="button"
              />
            )}
          </div>
        ) : null}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto" ref={detailScrollRef}>
        {isPersonal ? (
          <PersonalDetailBody detail={personalDetail} error={personalError} loading={personalLoading} />
        ) : (
          <>
            <CompanyDetailBody
              item={companyItem}
              loading={companyLoading}
              error={companyError}
              onRetry={onRetryCompany}
            />
            {companyItem ? (
              <ConversationPanel
                currentItem={companyItem}
                loading={conversation === null}
                messages={conversation}
                onLoadMessage={onLoadConversationMessage}
              />
            ) : null}
          </>
        )}
      </div>
    </Card>
  );
}

// Address-pair conversation thread shown under a company mail (slice 7).
function ConversationPanel({
  currentItem,
  loading,
  messages,
  onLoadMessage,
}: {
  currentItem: CanonicalEmail;
  loading: boolean;
  messages: CanonicalEmail[] | null;
  onLoadMessage: (item: CanonicalEmail) => Promise<CanonicalEmail>;
}) {
  // 스레드 목록은 항상 펼쳐 둔다(owner 피드백 2026-07-03) — 접힘은 행 단위만.
  // 아코디언: 행을 누르면 그 자리에서 본문이 펼쳐지고 다시 누르면 접힌다.
  // 본문 전체(html)는 펼칠 때 지연 로드해 캐시한다(재펼침 시 재요청 없음).
  const [expandedKeys, setExpandedKeys] = useState<ReadonlySet<string>>(new Set());
  const [loadedBodies, setLoadedBodies] = useState<ReadonlyMap<string, CanonicalEmail>>(new Map());
  const [failedKeys, setFailedKeys] = useState<ReadonlySet<string>>(new Set());

  function fetchBody(m: CanonicalEmail) {
    const key = companyKey(m);
    setFailedKeys((current) => {
      if (!current.has(key)) return current;
      const next = new Set(current);
      next.delete(key);
      return next;
    });
    onLoadMessage(m)
      .then((full) => {
        setLoadedBodies((current) => new Map(current).set(key, full));
      })
      .catch(() => {
        setFailedKeys((current) => new Set(current).add(key));
      });
  }

  function toggleMessage(m: CanonicalEmail) {
    const key = companyKey(m);
    const isOpen = expandedKeys.has(key);
    setExpandedKeys((current) => {
      const next = new Set(current);
      if (isOpen) next.delete(key);
      else next.add(key);
      return next;
    });
    // 현재 열린 메일은 부모 상세 본문을 재사용하므로 추가 로드가 필요 없다.
    const isCurrent = key === companyKey(currentItem);
    if (!isOpen && !loadedBodies.has(key) && !isCurrent) fetchBody(m);
  }
  const merged = new Map<string, CanonicalEmail>();
  merged.set(companyKey(currentItem), currentItem);
  for (const message of messages ?? []) {
    merged.set(companyKey(message), message);
  }
  const ordered = [...merged.values()].sort((a, b) => {
    const at = new Date(a.received_at ?? a.sent_at ?? 0).getTime();
    const bt = new Date(b.received_at ?? b.sent_at ?? 0).getTime();
    return at - bt;
  });
  return (
    <section className="px-4 pb-4 pt-5" style={{ borderTop: "1px solid var(--color-divider)" }}>
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3
          className="text-[var(--text-body-sm)] font-bold"
          style={{ color: "var(--color-text-strong)" }}
        >
          이 사람과 주고받은 메일
          <span className="ml-1 font-normal" style={{ color: "var(--color-text-muted)" }}>
            {ordered.length}
          </span>
        </h3>
        {loading ? (
          <span className="inline-flex items-center gap-1 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            불러오는 중
          </span>
        ) : null}
      </div>
      <ul className="flex flex-col gap-1.5">
        {ordered.map((m) => {
          const key = companyKey(m);
          const current = key === companyKey(currentItem);
          const body = mailBodyText(m).replace(/\s+/g, " ").slice(0, 90);
          const expanded = expandedKeys.has(key);
          // 현재 열린 메일은 부모 상세가 이미 전체 본문을 갖고 있어 그대로 쓴다.
          const full = loadedBodies.get(key) ?? (current ? currentItem : undefined);
          return (
            <li key={key}>
              <button
                aria-current={current ? "true" : undefined}
                aria-expanded={expanded}
                className="w-full rounded-[8px] px-3 py-2 text-left transition-colors hover:bg-[color-mix(in_srgb,var(--color-sidebar-active)_72%,transparent)]"
                onClick={() => toggleMessage(m)}
                style={{
                  background: current ? "var(--color-sidebar-active)" : "var(--color-app-canvas)",
                  border: current ? "1px solid var(--color-attention)" : "1px solid transparent",
                }}
                type="button"
              >
                <div className="flex flex-wrap items-center gap-1.5">
                  <Chip tone={m.direction === "outbound" ? "pass" : "neutral"}>
                    {m.direction === "outbound" ? "보냄" : "받음"}
                  </Chip>
                  {m.direction === "inbound" ? (
                    <Chip tone={m.read ? "muted" : "warn"}>{m.read ? "읽음" : "안읽음"}</Chip>
                  ) : null}
                  {m.replied && m.direction === "inbound" ? (
                    <Chip tone="pass">
                      <Reply className="h-3 w-3" />
                      답변함
                    </Chip>
                  ) : null}
                  {current ? <Chip tone="accent">현재 메일</Chip> : null}
                  <span className="ml-auto shrink-0 text-[var(--text-meta)]" style={{ color: "var(--color-text-faint)" }}>
                    {formatDate(m.received_at ?? m.sent_at)}
                  </span>
                </div>
                <div
                  className="mt-1 truncate text-[var(--text-body-sm)] font-semibold"
                  style={{ color: "var(--color-text-body)" }}
                >
                  {m.subject || "(제목 없음)"}
                </div>
                {body && !expanded ? (
                  <p
                    className="mt-1 truncate text-[var(--text-meta)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {body}
                  </p>
                ) : null}
              </button>
              {expanded ? (
                <ConversationMessageBody
                  full={full}
                  failed={failedKeys.has(key)}
                  onRetry={() => fetchBody(m)}
                />
              ) : null}
            </li>
          );
        })}
      </ul>
      {!loading && ordered.length <= 1 ? (
        <p className="mt-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          이 상대와 연결된 다른 메일이 아직 없습니다.
        </p>
      ) : null}
    </section>
  );
}

/** 스레드 행 아래에 아코디언으로 펼쳐지는 메일 본문(지연 로드). */
function ConversationMessageBody({
  full,
  failed,
  onRetry,
}: {
  full: CanonicalEmail | undefined;
  failed: boolean;
  onRetry: () => void;
}) {
  const html = (full?.body_html ?? "").trim();
  const text = full && !html ? mailBodyText(full) : "";
  return (
    <div
      className="mt-1 rounded-[8px] px-3 py-3"
      style={{ background: "var(--color-panel)", border: "1px solid var(--color-divider)" }}
    >
      {full ? (
        html ? (
          <MailHtmlBodyWithQuote
            key={companyKey(full)}
            html={html}
            backend={full.backend}
            accountId={full.account_id}
          />
        ) : text ? (
          <MailPlainBody key={companyKey(full)} text={text} />
        ) : (
          <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
            본문 없음
          </p>
        )
      ) : failed ? (
        <div className="grid justify-items-start gap-2">
          <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
            본문을 불러오지 못했습니다.
          </p>
          <ToolbarButton className="min-h-[36px]" icon={<RefreshCw size={15} />} onClick={onRetry} type="button">
            다시 시도
          </ToolbarButton>
        </div>
      ) : (
        <div className="grid gap-2">
          <Skeleton className="h-4 w-11/12" />
          <Skeleton className="h-4 w-4/5" />
          <Skeleton className="h-16 w-full" />
        </div>
      )}
    </div>
  );
}

// 답장 인용 헤더("2026-… <…> 작성:", "On … wrote:", 원문 구분선 등) — 인용부 바로
// 위에 붙어 있으면 함께 접는다.
const QUOTE_HEADER_RE =
  /(작성:\s*$|wrote:\s*$|-{2,}\s*original message\s*-{2,}|^-{4,}\s*$)/i;

/**
 * plain text 본문에서 답장 인용 꼬리("> " 줄 무더기)를 분리한다. Gmail처럼 기본은
 * 접어 두고 버튼으로 펼치기 위함. 본문 중간에 한두 줄만 인용된 경우(꼬리가 인용
 * 위주가 아닐 때)는 접지 않고 원문 그대로 둔다.
 */
function splitQuotedTail(text: string): { main: string; quoted: string | null } {
  const lines = text.split("\n");
  let start = -1;
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trimStart().startsWith(">")) {
      start = i;
      break;
    }
  }
  if (start < 0) return { main: text, quoted: null };
  const tail = lines.slice(start).filter((l) => l.trim());
  const quotedCount = tail.filter((l) => l.trimStart().startsWith(">")).length;
  if (quotedCount < 2 || quotedCount / tail.length < 0.8) {
    return { main: text, quoted: null };
  }
  let s = start;
  let probe = start - 1;
  while (probe >= 0 && !lines[probe].trim()) probe -= 1;
  if (probe >= 0 && QUOTE_HEADER_RE.test(lines[probe].trim())) s = probe;
  const main = lines.slice(0, s).join("\n").trimEnd();
  const quoted = lines.slice(s).join("\n").trim();
  return { main, quoted: quoted || null };
}

/**
 * HTML 본문에서 답장 인용 꼬리를 분리한다. 우리 작성기 마커
 * (`data-orthus-reply-quote`) 외에 Gmail(`gmail_quote`)·일반 클라이언트의
 * trailing `blockquote`도 잡는다. 뉴스레터처럼 본문 중간에 blockquote를 쓰는
 * 메일을 오접지 않도록 (1) 인용 위에 본문이 있고 (2) 인용이 실질적으로 본문
 * 끝까지 이어지는 경우에만 접는다.
 */
// 신뢰할 수 있는 인용 마커(우리 작성기·Gmail). 일반 blockquote는 마커가 없어
// "… 작성:"/"On … wrote:" 헤더가 바로 앞에 있을 때만 답장 인용으로 본다 —
// 뉴스레터가 본문 스타일로 쓰는 blockquote를 오접지 않기 위함.
const TRUSTED_QUOTE_SELECTOR =
  "[data-orthus-reply-quote], .gmail_quote, .gmail_quote_container";

function splitQuotedHtml(html: string): { main: string; quoted: string | null } {
  if (typeof document === "undefined") return { main: html, quoted: null };
  const root = document.createElement("div");
  root.innerHTML = html;
  const prevMeaningful = (n: Node): Node | null => {
    let p: Node | null = n.previousSibling;
    while (p && p.nodeType !== Node.ELEMENT_NODE && !(p.textContent ?? "").trim()) {
      p = p.previousSibling;
    }
    return p;
  };
  const nextMeaningful = (n: Node): Node | null => {
    let p: Node | null = n.nextSibling;
    while (p && p.nodeType !== Node.ELEMENT_NODE && !(p.textContent ?? "").trim()) {
      p = p.nextSibling;
    }
    return p;
  };
  // 인용은 래퍼 안 어느 깊이에나 올 수 있어(뒤에 서명 table 등이 붙기도 함)
  // 문서 순서로 첫 인용 노드를 찾는다.
  let quoteStart: Node | null = null;
  let quoteEnd: Element | null = null;
  for (const el of Array.from(
    root.querySelectorAll(`${TRUSTED_QUOTE_SELECTOR}, blockquote`),
  )) {
    const prev = prevMeaningful(el);
    const prevIsHeader =
      prev != null && QUOTE_HEADER_RE.test((prev.textContent ?? "").trim().slice(-80));
    if (el.matches(TRUSTED_QUOTE_SELECTOR)) {
      quoteStart = prevIsHeader && prev ? prev : el;
      quoteEnd = el;
      break;
    }
    if (prevIsHeader && prev) {
      quoteStart = prev;
      quoteEnd = el;
      break;
    }
  }
  if (!quoteStart || !quoteEnd) return { main: html, quoted: null };
  // 우리 마커 div 뒤에 오는 blockquote처럼, 인용 본체가 연달아 이어지면 함께 접는다.
  for (;;) {
    const next = nextMeaningful(quoteEnd);
    if (
      next?.nodeType === Node.ELEMENT_NODE &&
      (next as Element).matches(`${TRUSTED_QUOTE_SELECTOR}, blockquote`)
    ) {
      quoteEnd = next as Element;
      continue;
    }
    break;
  }
  const core: Node[] = [];
  for (let n: Node | null = quoteStart; n; n = n.nextSibling) {
    core.push(n);
    if (n === quoteEnd) break;
  }
  // 인용 뒤에 남은 형제들(서명 등): 실질 텍스트가 짧으면 함께 접고, 길면 인용만 접는다.
  const rest: Node[] = [];
  for (let n: Node | null = quoteEnd.nextSibling; n; n = n.nextSibling) rest.push(n);
  const restText = rest.map((n) => n.textContent ?? "").join("").trim();
  const collapse = restText.length <= 200 ? [...core, ...rest] : core;
  const quotedText = collapse.map((n) => n.textContent ?? "").join("").trim();
  if (quotedText.length < 120) return { main: html, quoted: null };
  const quotedDiv = document.createElement("div");
  collapse.forEach((n) => quotedDiv.appendChild(n));
  // 인용을 걷어낸 나머지가 비면(메일 전체가 인용) 접지 않고 그대로 둔다.
  if (!(root.textContent ?? "").trim()) return { main: html, quoted: null };
  return { main: root.innerHTML, quoted: quotedDiv.innerHTML };
}

function QuoteToggleButton({
  open,
  onClick,
}: {
  open: boolean;
  onClick: () => void;
}) {
  return (
    <button
      aria-expanded={open}
      className="mt-3 inline-flex items-center gap-1.5 rounded-full px-3 text-[var(--text-meta)] font-semibold transition-colors"
      onClick={onClick}
      style={{
        minHeight: 32,
        border: "1px solid var(--color-divider)",
        background: "var(--color-panel)",
        color: "var(--color-text-muted)",
      }}
      title={open ? "이전 대화 숨기기" : "이전 대화 보기"}
      type="button"
    >
      <span aria-hidden style={{ letterSpacing: 2 }}>
        ⋯
      </span>
      {open ? "이전 대화 숨기기" : "이전 대화 보기"}
    </button>
  );
}

const MAIL_PRE_CLASS =
  "whitespace-pre-wrap break-words font-[family-name:var(--font-sans)] text-[var(--text-body)]";

/** plain text 본문 — 답장 인용 꼬리는 Gmail처럼 접어 두고 버튼으로 펼친다. */
function MailPlainBody({ text }: { text: string }) {
  const { main, quoted } = useMemo(() => splitQuotedTail(text), [text]);
  const [showQuoted, setShowQuoted] = useState(false);
  return (
    <div>
      {main ? (
        <pre className={MAIL_PRE_CLASS} style={{ color: "var(--color-text-body)", lineHeight: 1.58 }}>
          {main}
        </pre>
      ) : null}
      {quoted ? (
        <>
          <QuoteToggleButton open={showQuoted} onClick={() => setShowQuoted((v) => !v)} />
          {showQuoted ? (
            <pre
              className={cx(MAIL_PRE_CLASS, "mt-3")}
              style={{
                color: "var(--color-text-muted)",
                lineHeight: 1.58,
                borderLeft: "3px solid var(--color-divider)",
                paddingLeft: 12,
              }}
            >
              {quoted}
            </pre>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

/** HTML 본문 — 우리 답장 작성기가 남긴 인용 블록이 있으면 접어 두고 펼친다. */
function MailHtmlBodyWithQuote({
  html,
  backend,
  accountId,
}: {
  html: string;
  backend?: MailBackendName;
  accountId?: string | null;
}) {
  // 원격 이미지는 서버 프록시 경유로 치환한 뒤 인용 분리를 돌린다(둘 다 메모).
  const proxied = useMemo(
    () => proxyMailImages(html, { backend, accountId }),
    [html, backend, accountId],
  );
  const { main, quoted } = useMemo(() => splitQuotedHtml(proxied), [proxied]);
  const [showQuoted, setShowQuoted] = useState(false);
  if (!quoted) return <MailHtmlBody html={proxied} />;
  return (
    <div>
      {main ? <MailHtmlBody html={main} /> : null}
      <QuoteToggleButton open={showQuoted} onClick={() => setShowQuoted((v) => !v)} />
      {showQuoted ? (
        <div className="mt-3">
          <MailHtmlBody html={quoted} />
        </div>
      ) : null}
    </div>
  );
}

function CompanyDetailBody({
  item,
  loading,
  error,
  onRetry,
}: {
  item: CanonicalEmail | null;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  if (!item) {
    return <DetailEmpty />;
  }
  const timestamp = item.received_at ?? item.sent_at;
  // 원본 충실도: html이 있으면 샌드박스 iframe으로 원본 서식 그대로, 없으면
  // plain text. 리스트 row(본문 없음) 상태에서는 로딩/에러를 정직하게 보여준다.
  const html = (item.body_html ?? "").trim();
  const body = html ? "" : mailBodyText(item);
  const realAttachments = item.attachments.filter((attachment) => !attachment.inline);
  return (
    <article className="flex flex-col">
      <header className="px-4 py-4" style={{ borderBottom: "1px solid var(--color-divider)" }}>
        <div className="flex flex-wrap items-center gap-1.5">
          <Chip tone={item.scope === "company" ? "neutral" : "pass"}>{item.scope === "company" ? "회사" : "개인"}</Chip>
          <Chip tone="muted">{mailboxLabel(item.backend, item.owner_addr)}</Chip>
          <Chip tone="muted">{item.direction === "inbound" ? "받음" : "보냄"}</Chip>
        </div>
        <h2
          className="mt-3 break-words text-[20px] font-extrabold"
          style={{ color: "var(--color-text-strong)", lineHeight: 1.16 }}
        >
          {item.subject || "(제목 없음)"}
        </h2>
        <div className="mt-3 grid gap-1.5 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-body)" }}>
          <MetaLine label="From" value={item.from_addr} />
          <MetaLine label="To" value={item.to_addr.join(", ")} />
          {item.cc_addr.length ? <MetaLine label="Cc" value={item.cc_addr.join(", ")} /> : null}
          <MetaLine label="Date" value={formatDate(timestamp)} />
        </div>
      </header>
      <div className="px-4 py-4">
        {html ? (
          <MailHtmlBodyWithQuote
            key={companyKey(item)}
            html={html}
            backend={item.backend}
            accountId={item.account_id}
          />
        ) : body ? (
          <MailPlainBody key={companyKey(item)} text={body} />
        ) : loading ? (
          <div className="grid gap-2">
            <Skeleton className="h-4 w-11/12" />
            <Skeleton className="h-4 w-4/5" />
            <Skeleton className="h-4 w-3/5" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : error ? (
          <div className="grid justify-items-start gap-2">
            <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
              본문을 불러오지 못했습니다.
            </p>
            <ToolbarButton className="min-h-[44px]" icon={<RefreshCw size={15} />} onClick={onRetry} type="button">
              다시 시도
            </ToolbarButton>
          </div>
        ) : (
          <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
            본문 없음
          </p>
        )}
      </div>
      {realAttachments.length > 0 ? (
        <footer className="px-4 py-3" style={{ borderTop: "1px solid var(--color-divider)" }}>
          <div className="mb-2 flex items-center gap-2">
            <Paperclip className="h-4 w-4" style={{ color: "var(--color-text-muted)" }} />
            <span className="text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
              첨부 {realAttachments.length}개
            </span>
          </div>
          <div className="grid gap-1.5">
            {realAttachments.map((attachment, index) => (
              <AttachmentRow
                accountId={item.account_id}
                attachment={attachment}
                backend={item.backend}
                key={attachment.att_id ?? attachment.ref ?? `${attachment.filename}:${index}`}
              />
            ))}
          </div>
        </footer>
      ) : null}
    </article>
  );
}

function AttachmentRow({
  accountId,
  attachment,
  backend,
}: {
  accountId: string | null;
  attachment: EmailAttachmentRef;
  backend: MailBackendName;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const disabled = !attachment.ref;

  async function download() {
    if (!attachment.ref) return;
    setBusy(true);
    setFailed(false);
    try {
      await api.downloadMailAttachment(backend, attachment.ref, attachment.filename, accountId);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      className="flex min-h-[44px] w-full items-center gap-2 rounded-[8px] px-3 py-2 text-left transition-colors disabled:opacity-50"
      disabled={disabled || busy}
      onClick={download}
      style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider)" }}
      title={disabled ? "이 첨부는 다운로드할 수 없습니다" : attachment.filename}
      type="button"
    >
      <Paperclip className="h-4 w-4 shrink-0" style={{ color: "var(--color-text-muted)" }} />
      <span
        className="min-w-0 flex-1 truncate text-[var(--text-body-sm)] font-semibold"
        style={{ color: "var(--color-text-strong)" }}
      >
        {attachment.filename}
      </span>
      {attachment.size > 0 ? (
        <span className="shrink-0 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          {formatBytes(attachment.size)}
        </span>
      ) : null}
      {busy ? (
        <Loader2 className="h-4 w-4 shrink-0 animate-spin" style={{ color: "var(--color-text-muted)" }} />
      ) : failed ? (
        <span className="shrink-0 text-[var(--text-meta)]" style={{ color: "var(--color-attention)" }}>
          실패
        </span>
      ) : (
        <Download className="h-4 w-4 shrink-0" style={{ color: "var(--color-text-muted)" }} />
      )}
    </button>
  );
}

function PersonalDetailBody({
  detail,
  error,
  loading,
}: {
  detail: PersonalMailDetail | null;
  error: boolean;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="p-4">
        <Skeleton className="h-7 w-2/3" />
        <Skeleton className="mt-3 h-4 w-1/2" />
        <Skeleton className="mt-8 h-32 w-full" />
      </div>
    );
  }
  if (error || !detail) {
    return (
      <div className="flex min-h-[280px] items-center justify-center p-6 text-center">
        <div>
          <MailOpen className="mx-auto h-8 w-8" style={{ color: "var(--color-text-muted)" }} />
          <div className="mt-3 text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
            메일을 불러오지 못했습니다
          </div>
        </div>
      </div>
    );
  }
  const body = detail.body?.trim() ?? "";
  return (
    <article className="flex h-full min-h-0 flex-col">
      <header className="shrink-0 px-4 py-4" style={{ borderBottom: "1px solid var(--color-divider)" }}>
        <div className="flex flex-wrap items-center gap-1.5">
          <Chip tone="pass">개인</Chip>
          <Chip tone="muted">개인 Gmail</Chip>
        </div>
        <h2
          className="mt-3 break-words text-[20px] font-extrabold"
          style={{ color: "var(--color-text-strong)", lineHeight: 1.16 }}
        >
          {detail.subject || detail.title || "(제목 없음)"}
        </h2>
        <div className="mt-3 grid gap-1.5 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-body)" }}>
          <MetaLine label="Date" value={formatDate(detail.sent_at)} />
        </div>
      </header>
      <div className="min-h-0 flex-1 px-4 py-4">
        {body ? (
          <MailPlainBody key={detail.doc_id} text={body} />
        ) : (
          <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
            본문 없음
          </p>
        )}
      </div>
    </article>
  );
}

function DetailEmpty() {
  return (
    <div className="flex min-h-[280px] items-center justify-center p-6 text-center">
      <div>
        <MailOpen className="mx-auto h-8 w-8" style={{ color: "var(--color-text-muted)" }} />
        <div className="mt-3 text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
          선택된 메일 없음
        </div>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- compose */

interface FromOption {
  address: string;
  backend: MailBackendName;
}

function deriveFromOptions(data: MailInboxResponse | null): FromOption[] {
  if (!data?.send_enabled) return [];
  const seen = new Set<string>();
  const options: FromOption[] = [];
  for (const backend of data.backends) {
    if (!backend.send_configured || !backend.owner_addr) continue;
    if (seen.has(backend.owner_addr)) continue;
    seen.add(backend.owner_addr);
    options.push({ address: backend.owner_addr, backend: backend.backend });
  }
  return options;
}

/** 작성창 필드 한 줄: 좌측 고정폭 라벨 + 우측 입력. 세로 공간을 아껴 본문을 키운다. */
function ComposeRow({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="grid gap-0.5">
      <div className="flex items-center gap-2">
        <span
          className="w-[74px] shrink-0 text-[var(--text-meta)] font-semibold"
          style={{ color: "var(--color-text-muted)" }}
        >
          {label}
        </span>
        <div className="flex min-h-[44px] min-w-0 flex-1 flex-wrap items-center gap-2">{children}</div>
      </div>
      {hint ? <div className="pl-[82px]">{hint}</div> : null}
    </div>
  );
}

/** 초안 아래 붙는 근거 표시.
 *
 *  이 초안이 받은 메일만 보고 쓴 것인지, 회사 위키를 읽고 쓴 것인지를 검토자가 바로
 *  알아야 한다. 모순 경고가 있으면 그게 더 중요하므로 위에 둔다 — 사외로 나가는 메일에
 *  사내 미확정 값이 확정처럼 적히는 것이 이 기능의 최대 위험이다.
 *  근거 없음(flag off / 관련 페이지 없음)일 때는 아무것도 그리지 않는다. */
/** 근거 칩에 쓸 사람이 읽는 라벨.
 *
 *  slug 유니크화(docs/wiki-unique-source-slug.md)로 갈라진 페이지는 `title`이 slug와
 *  똑같이 저장돼 있어서 그대로 그리면 `atlas-v2-release-date-28ceb6a2` 같은 내부
 *  식별자가 화면에 노출된다. 제목이 사실상 slug일 때만 위키가 원래 쓰는 규칙(하이픈
 *  분해 + 단어별 대문자)으로 되돌리고, 구분용 해시 꼬리는 뗀다. 링크 주소는 건드리지
 *  않는다 — 보이는 이름만 바꾼다. */
function groundingLabel(title: string, slug: string): string {
  const looksLikeSlug = title === slug || /^[a-z0-9-]+$/.test(title);
  if (!looksLikeSlug) return title;
  return title
    .replace(/-[0-9a-f]{8}$/, "")
    .split("-")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function DraftGroundingNote({ grounding }: { grounding: WikiMailGrounding | null }) {
  if (!grounding) return null;
  // 같은 주제의 위키 페이지는 slug 유니크화로 여러 벌 갈라져 있어(`sla`,
  // `sla-3401858f`, …) 상위 3건이 전부 같은 페이지의 사본일 수 있다. 그대로 그리면
  // 똑같은 이름의 칩이 세 개 붙어 근거가 세 곳인 것처럼 보인다. 보이는 이름 기준으로
  // 첫 번째만 남긴다 — 링크는 그 대표 slug로 간다.
  const seen = new Set<string>();
  const sources = grounding.sources.filter((s) => {
    const label = groundingLabel(s.title, s.page_slug);
    if (seen.has(label)) return false;
    seen.add(label);
    return true;
  });
  return (
    <div className="mt-1.5 flex flex-col gap-1">
      {grounding.conflict_warning ? (
        <p
          className="text-[var(--text-meta)]"
          style={{ color: "var(--color-warn-fg, var(--color-fail-fg))" }}
        >
          ⚠ 지식그래프: {grounding.conflict_warning} 초안은 날짜·수치를 단정하지 않았습니다.
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-1">
        <span className="text-[var(--text-meta)] text-[var(--color-text-muted)]">
          회사 위키 근거
        </span>
        {sources.map((s) => (
          <Link
            key={s.page_slug}
            href={`/wiki/${s.page_slug}`}
            className="rounded border px-1.5 py-0.5 text-[var(--text-meta)] hover:underline"
            style={{ borderColor: "var(--color-border)" }}
            title={s.excerpt || s.title}
          >
            {groundingLabel(s.title, s.page_slug)}
          </Link>
        ))}
      </div>
    </div>
  );
}

type AiDraftState = {
  draft: string;
  busy: boolean;
  checked: boolean;
  sent: boolean;
  error: string | null;
  // 이 초안이 어떤 회사 위키에 근거했는지. 세션 한정 상태라 localStorage에 저장하지
  // 않는다(본문/체크만 저장) — 근거는 초안을 다시 생성하면 함께 새로 온다.
  grounding: WikiMailGrounding | null;
};

const AI_DRAFT_DEFAULT: AiDraftState = {
  draft: "",
  busy: false,
  checked: false,
  sent: false,
  error: null,
  grounding: null,
};

// AI 초안 영속화: 폴더 전환(언마운트)에도 작성/체크 상태가 살아남도록 메일함
// 단위로 저장한다. busy/sent/error는 세션 한정 상태라 저장하지 않는다.
type AiDraftPersisted = { draft: string; checked: boolean };

const AI_DRAFT_STORAGE_PREFIX = "orthus:mail:aidraft:v1:";

function aiDraftStorageKey(mailboxKey: string): string {
  return `${AI_DRAFT_STORAGE_PREFIX}${mailboxKey || ALL_MAILBOX_KEY}`;
}

function EmptyAiDraft({ text }: { text: string }) {
  return (
    <div
      className="flex h-full min-h-[160px] items-center justify-center px-4 text-center text-[var(--text-body-sm)]"
      style={{ color: "var(--color-text-muted)" }}
    >
      {text}
    </div>
  );
}

// 위키 근거는 central 초안 경로(`/mail/draft`)에서만 온다. 로컬 에이전트 경로
// (collector daemon의 claude/hermes)는 회사 위키에 붙지 않으므로 undefined다.
type DraftResult = {
  subject: string;
  body: string;
  wiki_grounding?: WikiMailGrounding | null;
};
type DraftOutcome = {
  draft: DraftResult | null;
  mode: "local" | "central" | "legacy" | "unavailable";
  message: string | null;
  runner: string | null;
};

async function pollMailDraftResult(
  workId: string,
  tries = 80,
  intervalMs = 1500,
): Promise<DraftResult | null> {
  for (let i = 0; i < tries; i++) {
    let item;
    try {
      item = await api.getAgentWorkItem(workId);
    } catch {
      return null;
    }
    const md = (item.payload as Record<string, unknown> | undefined)?.["mail_draft"] as
      | { subject?: string; body?: string }
      | undefined;
    if (md && typeof md.body === "string") {
      return { subject: md.subject ?? "", body: md.body };
    }
    if (item.state !== "auto_execute") return null;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return null;
}

async function generateMailDraft(
  req: {
    mode: "compose" | "reply";
    instruction: string;
    to: string;
    subject: string;
    context: string;
    runner?: "claude" | "hermes";
  },
  onProgress?: (message: string) => void,
): Promise<DraftOutcome> {
  let dispatch: Awaited<ReturnType<typeof api.dispatchMailDraft>> | null = null;
  try {
    dispatch = await api.dispatchMailDraft({
      mode: req.mode,
      instruction: req.instruction,
      to: req.to,
      subject: req.subject,
      context: req.context,
      runner: req.runner,
    });
  } catch {
    dispatch = null;
  }
  if (dispatch?.mode === "unavailable") {
    return { draft: null, mode: "unavailable", message: dispatch.message, runner: null };
  }
  if (dispatch?.mode === "central" && dispatch.draft) {
    return { draft: dispatch.draft, mode: "central", message: null, runner: null };
  }
  if (dispatch?.mode === "local" && dispatch.work_id) {
    onProgress?.(`내 PC의 ${dispatch.runner ?? "에이전트"}로 초안 생성 중...`);
    const draft = await pollMailDraftResult(dispatch.work_id);
    return {
      draft,
      mode: "local",
      message: draft ? null : "로컬 초안 생성 실패. 다시 시도해 주세요.",
      runner: dispatch.runner,
    };
  }
  const legacy = await api.draftMail({
    mode: req.mode,
    instruction: req.instruction,
    to: req.to,
    subject: req.subject,
    context: req.context,
  });
  return { draft: legacy, mode: "legacy", message: null, runner: null };
}

function AiDraftBatchView({
  items,
  mailbox,
  isPersonalMailbox,
  fromOptions,
  loading,
  onRefresh,
}: {
  items: CanonicalEmail[];
  mailbox: Mailbox | undefined;
  isPersonalMailbox: boolean;
  fromOptions: FromOption[];
  loading: boolean;
  onRefresh: () => void;
}) {
  const candidates = useMemo(
    () =>
      items
        .filter(
          (item) =>
            matchesMailbox(item, mailbox) &&
            item.direction === "inbound" &&
            !item.trashed &&
            !item.replied,
        )
        .sort((a, b) => (b.received_at ?? "").localeCompare(a.received_at ?? ""))
        .slice(0, 50),
    [items, mailbox],
  );

  // 폴더 전환마다 이 뷰 전체가 언마운트되므로, 초안/체크 상태는 메일함 단위로
  // localStorage에 저장해 재진입 시 복원한다.
  const draftStorageKey = aiDraftStorageKey(mailbox?.key ?? ALL_MAILBOX_KEY);
  const [states, setStates] = useState<Record<string, AiDraftState>>(() => {
    const persisted = readDraftStorage<Record<string, AiDraftPersisted>>(draftStorageKey) ?? {};
    const initial: Record<string, AiDraftState> = {};
    for (const [key, value] of Object.entries(persisted)) {
      initial[key] = { ...AI_DRAFT_DEFAULT, draft: value.draft, checked: value.checked };
    }
    return initial;
  });
  const [banner, setBanner] = useState<{ tone: "pass" | "fail"; message: string } | null>(null);
  const [batchBusy, setBatchBusy] = useState(false);

  // 변경마다 저장 — 빈(초안 없고 미체크) 항목은 덜어내 저장소가 무한정 크지 않게 한다.
  useEffect(() => {
    const persisted: Record<string, AiDraftPersisted> = {};
    for (const [key, value] of Object.entries(states)) {
      if (!value.draft && !value.checked) continue;
      persisted[key] = { draft: value.draft, checked: value.checked };
    }
    writeDraftStorage(draftStorageKey, persisted);
  }, [states, draftStorageKey]);

  const canSend = fromOptions.length > 0;
  const stateFor = (key: string) => states[key] ?? AI_DRAFT_DEFAULT;
  function patch(key: string, next: Partial<AiDraftState>) {
    setStates((prev) => ({ ...prev, [key]: { ...(prev[key] ?? AI_DRAFT_DEFAULT), ...next } }));
  }
  function resolveFrom(item: CanonicalEmail): string {
    return (
      item.owner_addr ||
      fromOptions.find((option) => option.backend === item.backend)?.address ||
      fromOptions[0]?.address ||
      ""
    );
  }

  async function generateDraft(item: CanonicalEmail) {
    const key = companyKey(item);
    patch(key, { busy: true, error: null });
    try {
      const context = mailBodyText(item).slice(0, 5000);
      const outcome = await generateMailDraft({
        mode: "reply",
        instruction: "",
        to: item.from_addr,
        subject: reSubject(item.subject),
        context,
      });
      if (outcome.mode === "unavailable") {
        patch(key, { busy: false, error: outcome.message ?? "로컬 에이전트 미등록" });
        return;
      }
      const body = outcome.draft?.body ?? "";
      patch(key, {
        busy: false,
        draft: body,
        checked: Boolean(body),
        grounding: outcome.draft?.wiki_grounding ?? null,
        error: body ? null : (outcome.message ?? "빈 초안. 원문 맥락이 부족할 수 있습니다."),
      });
    } catch (error) {
      patch(key, { busy: false, error: error instanceof Error ? error.message : "초안 생성 실패" });
    }
  }

  async function sendOne(item: CanonicalEmail): Promise<boolean> {
    const key = companyKey(item);
    const body = (states[key]?.draft ?? "").trim();
    if (!body) {
      patch(key, { error: "초안이 비어 있습니다." });
      return false;
    }
    const from = resolveFrom(item);
    if (!from) {
      patch(key, { error: "발신 메일함을 찾을 수 없습니다." });
      return false;
    }
    patch(key, { busy: true, error: null });
    try {
      const result = await api.sendMail({
        from_addr: from,
        to: item.from_addr,
        subject: reSubject(item.subject),
        text: body,
        reply_to_id: item.external_id || undefined,
      });
      if (result.status === "sent") {
        // draft를 비워 저장소에서도 이 항목이 함께 정리되게 한다(발송 완료본을
        // 다음 진입에서 다시 보여줄 필요가 없다).
        patch(key, { busy: false, sent: true, checked: false, draft: "" });
        return true;
      }
      patch(key, { busy: false, error: result.error ?? "발송 실패" });
      return false;
    } catch (error) {
      patch(key, { busy: false, error: error instanceof Error ? error.message : "발송 실패" });
      return false;
    }
  }

  const visibleCandidates = candidates.filter((item) => !stateFor(companyKey(item)).sent);

  const checkedItems = visibleCandidates.filter((item) => {
    const state = stateFor(companyKey(item));
    return state.checked;
  });

  async function generateSelected() {
    const targets = checkedItems.filter((item) => !stateFor(companyKey(item)).draft.trim());
    if (!targets.length) {
      setBanner({ tone: "fail", message: "초안을 만들 항목을 먼저 체크하세요." });
      return;
    }
    setBanner(null);
    setBatchBusy(true);
    for (const item of targets) await generateDraft(item);
    setBatchBusy(false);
  }

  async function sendSelected() {
    if (!canSend) {
      setBanner({ tone: "fail", message: "발송이 비활성화되어 있습니다." });
      return;
    }
    const targets = checkedItems.filter((item) => stateFor(companyKey(item)).draft.trim());
    if (!targets.length) {
      setBanner({ tone: "fail", message: "보낼 초안이 없습니다." });
      return;
    }
    if (!window.confirm(`체크한 ${targets.length}건을 지금 발송할까요?`)) return;
    setBanner(null);
    setBatchBusy(true);
    let ok = 0;
    let fail = 0;
    for (const item of targets) {
      if (await sendOne(item)) ok += 1;
      else fail += 1;
    }
    setBatchBusy(false);
    setBanner({
      tone: fail ? "fail" : "pass",
      message: fail ? `${ok}건 발송, ${fail}건 실패` : `${ok}건 모두 발송했습니다.`,
    });
    if (ok) onRefresh();
  }

  const selectableKeys = visibleCandidates.map((item) => companyKey(item));
  const allChecked = selectableKeys.length > 0 && selectableKeys.every((key) => stateFor(key).checked);
  function toggleAll(check: boolean) {
    setStates((prev) => {
      const next = { ...prev };
      for (const key of selectableKeys) {
        next[key] = { ...(next[key] ?? AI_DRAFT_DEFAULT), checked: check };
      }
      return next;
    });
  }

  return (
    <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div
        className="flex shrink-0 flex-wrap items-center gap-2 px-4 py-2.5"
        style={{ borderBottom: "1px solid var(--color-divider)" }}
      >
        <Sparkles className="h-4 w-4" />
        <span
          className="text-[var(--text-body-sm)] font-semibold"
          style={{ color: "var(--color-text-strong)" }}
        >
          AI 초안 · 답장 대기 {visibleCandidates.length}건
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <label
            className="flex items-center gap-1.5 text-[var(--text-meta)]"
            style={{ color: "var(--color-text-muted)" }}
          >
            <input
              type="checkbox"
              className="h-4 w-4"
              checked={allChecked}
              onChange={(event) => toggleAll(event.target.checked)}
              disabled={!selectableKeys.length}
              aria-label="전체 선택"
            />
            전체 선택
          </label>
          <ToolbarButton onClick={() => void generateSelected()} disabled={batchBusy || !checkedItems.length}>
            {batchBusy ? "처리 중" : "선택 초안 생성"}
          </ToolbarButton>
          <ToolbarButton
            tone="primary"
            onClick={() => void sendSelected()}
            disabled={batchBusy || !checkedItems.length}
          >
            선택 {checkedItems.length}건 보내기
          </ToolbarButton>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {banner ? (
          <div className="mb-2">
            <Banner tone={banner.tone} title={banner.message} />
          </div>
        ) : null}
        {!canSend ? (
          <p className="mb-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            발송이 비활성화되어 있어 초안 작성까지만 가능합니다.
          </p>
        ) : null}

        {isPersonalMailbox ? (
          <EmptyAiDraft text="AI 초안은 회사 메일함에서만 사용할 수 있습니다." />
        ) : loading && !candidates.length ? (
          <div className="space-y-2">
            {[0, 1, 2].map((index) => (
              <Skeleton key={index} className="h-28 w-full" />
            ))}
          </div>
        ) : !visibleCandidates.length ? (
          <EmptyAiDraft text="답장이 필요한 메일이 없습니다." />
        ) : (
          <div className="space-y-2">
            {visibleCandidates.map((item) => {
              const key = companyKey(item);
              const draftState = stateFor(key);
              const original = mailBodyText(item).slice(0, 600);
              return (
                <div
                  key={key}
                  className="rounded-[8px] border p-2.5"
                  style={{
                    background: draftState.sent ? "var(--color-pass-bg, #f0fdf4)" : "var(--color-app-canvas)",
                    borderColor: "var(--color-divider-soft)",
                  }}
                >
                  <div className="flex items-start gap-2">
                    <input
                      type="checkbox"
                      className="mt-1 h-5 w-5 shrink-0"
                      checked={draftState.checked}
                      disabled={draftState.sent}
                      onChange={(event) => patch(key, { checked: event.target.checked })}
                      aria-label="이 메일 선택"
                    />
                    <div className="min-w-0 flex-1">
                      <div
                        className="flex flex-wrap items-center gap-1.5 text-[var(--text-meta)]"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        <span
                          className="truncate font-semibold"
                          style={{ color: "var(--color-text-strong)" }}
                          title={item.from_addr}
                        >
                          {item.from_addr}
                        </span>
                        <span>· {formatDate(item.received_at)}</span>
                        <Chip tone="muted">{mailboxLabel(item.backend, item.owner_addr)}</Chip>
                        {item.owner_addr ? <Chip tone="muted">{item.owner_addr}</Chip> : null}
                        {draftState.sent ? <Chip tone="pass">보냄</Chip> : null}
                      </div>
                      <div
                        className="mt-0.5 truncate text-[var(--text-body-sm)] font-semibold"
                        style={{ color: "var(--color-text-strong)" }}
                        title={item.subject}
                      >
                        {reSubject(item.subject)}
                      </div>
                      <details className="mt-1">
                        <summary
                          className="cursor-pointer text-[var(--text-meta)]"
                          style={{ color: "var(--color-text-muted)" }}
                        >
                          받은 원문
                        </summary>
                        <div
                          className="mt-1 max-h-32 overflow-y-auto whitespace-pre-wrap text-[var(--text-meta)]"
                          style={{ color: "var(--color-text-body)" }}
                        >
                          {original || "(본문 없음)"}
                        </div>
                      </details>
                      <textarea
                        className={cx(inputClass, "mt-1.5 min-h-[88px] w-full resize-y")}
                        style={inputStyle}
                        value={draftState.draft}
                        disabled={draftState.sent}
                        placeholder="'AI 초안'을 누르거나 직접 답장을 작성하세요."
                        onChange={(event) => patch(key, { draft: event.target.value })}
                      />
                      {draftState.error ? (
                        <p className="mt-1 text-[var(--text-meta)]" style={{ color: "var(--color-fail-fg)" }}>
                          {draftState.error}
                        </p>
                      ) : null}
                      <DraftGroundingNote grounding={draftState.grounding} />
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        <ToolbarButton
                          className="h-9"
                          icon={
                            draftState.busy ? (
                              <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                              <Sparkles className="h-4 w-4" />
                            )
                          }
                          onClick={() => void generateDraft(item)}
                          disabled={draftState.busy || draftState.sent}
                        >
                          {draftState.busy ? "생성 중" : draftState.draft ? "다시 생성" : "AI 초안"}
                        </ToolbarButton>
                        <ToolbarButton
                          className="h-9"
                          tone="primary"
                          onClick={() => void sendOne(item)}
                          disabled={draftState.busy || draftState.sent || !draftState.draft.trim() || !canSend}
                        >
                          보내기
                        </ToolbarButton>
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </Card>
  );
}

/** 답장(Re)/전달 등에서 작성창을 미리 채우는 값. */
type ComposePrefill = {
  fromAddr?: string;
  to?: string;
  cc?: string;
  subject?: string;
  html?: string;
  replyToId?: string;
  context?: string;
};

function escapeHtmlText(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** 받은 메일 → 답장 작성창 prefill(받는사람=발신자, 제목 Re:, 원문 인용, reply_to_id). */
function buildReplyPrefill(item: CanonicalEmail, fromOptions: FromOption[]): ComposePrefill {
  const sender = extractEmail(item.from_addr);
  const baseSubject = item.subject.replace(/^\s*(re:\s*)+/i, "").trim();
  // 답장은 그 메일을 받은 메일함(owner_addr)에서 보낸다. 같은 backend에 메일함이
  // 여러 개일 수 있어 backend 매칭은 owner_addr가 없을 때의 fallback이다.
  const fromAddr =
    fromOptions.find((option) => option.address === item.owner_addr)?.address ??
    fromOptions.find((option) => option.backend === item.backend)?.address ??
    fromOptions[0]?.address;
  const when = formatDate(item.sent_at ?? item.received_at);
  // 원문은 plain text 우선 인용(escape)해 작성창에 provider HTML을 직접 넣지 않는다.
  const plain = mailBodyText(item);
  const quotedBody = plain ? escapeHtmlText(plain).replace(/\n/g, "<br>") : "";
  const header = `${when} ${escapeHtmlText(item.from_addr)} 님이 작성:`;
  const html =
    `<br><br><div data-orthus-reply-quote="true" style="color:#6b7280;font-size:13px">${header}</div>` +
    `<blockquote style="margin:6px 0 0;padding-left:12px;border-left:3px solid #e5e7eb;color:#6b7280">${quotedBody}</blockquote>`;
  return {
    fromAddr,
    to: sender,
    subject: `Re: ${baseSubject}`,
    html,
    replyToId: item.external_id,
    context: plain.slice(0, 4000),
  };
}

type RecipientField = "to" | "cc" | "bcc";

function splitRecipientList(value: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of value.replace(/;/g, ",").split(/[,\s]+/)) {
    const email = extractEmail(raw).trim();
    if (!email || !email.includes("@")) continue;
    const key = email.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(email);
  }
  return out;
}

function appendRecipients(current: string, nextEmails: string[]): string {
  return splitRecipientList(`${current}, ${nextEmails.join(", ")}`).join(", ");
}

function teamMemberEmails(member: TeamMember): string[] {
  return splitRecipientList(member.email ?? "");
}

function allowlistAsTeamMember(entry: AllowlistEntry): TeamMember {
  const email = entry.email;
  return {
    member_id: `allowlist:${entry.allowlist_id}`,
    name: email.split("@")[0] || email,
    title: "로그인 계정",
    department: "Allowlist",
    email,
    phone: null,
    join_date: null,
    birthday: null,
    address: null,
    emergency_contact: null,
    bank_account: null,
    color: null,
    bio: null,
    sort_order: 0,
    active: entry.revoked_at == null,
    invited: true,
  };
}

// 작성창 초안 영속화: 메일함 전환/탭 닫기/새로고침으로 잃던 본문을 매 입력마다
// localStorage에 저장한다(별도 beforeunload 불필요 — 언마운트도 이걸로 커버).
type ComposeDraftData = {
  to: string;
  cc: string;
  bcc: string;
  showCc: boolean;
  subject: string;
  html: string;
};

const COMPOSE_DRAFT_PREFIX = "orthus:mail:compose:v1:";

function composeDraftKey(mailboxAddr: string, replyToId?: string): string {
  return `${COMPOSE_DRAFT_PREFIX}${mailboxAddr || "unknown"}:${replyToId ?? "new"}`;
}

function ComposeView({
  fromOptions,
  lockedFrom,
  prefill,
  onClose,
  onSent,
}: {
  fromOptions: FromOption[];
  /** 지정되면 보내는 사람을 이 주소로 고정하고 확인용으로만 표시한다. */
  lockedFrom?: string;
  prefill?: ComposePrefill;
  onClose: () => void;
  /** 발송 성공 직후 호출 — 부모가 보낸편지함을 즉시 갱신한다. */
  onSent?: () => void;
}) {
  // 이 작성창의 저장 키 — key={replyToId ?? "new"}로 컴포넌트가 통째로
  // 리마운트되므로 최초 렌더 시점 값(발신 메일함 + 답장 대상)으로 고정한다.
  // lazy useState 초기화로 한 번만 계산한다(렌더 중 ref 접근 금지 규칙 준수).
  const [draftKey] = useState(() =>
    composeDraftKey(
      lockedFrom || prefill?.fromAddr || fromOptions[0]?.address || "unknown",
      prefill?.replyToId,
    ),
  );
  const [savedDraft] = useState(() => readDraftStorage<ComposeDraftData>(draftKey));

  const [fromChoice, setFromChoice] = useState(prefill?.fromAddr || fromOptions[0]?.address || "");
  // 잠금이면 항상 lockedFrom을 쓴다(작성 중 사이드바 메일함 변경도 그대로 따라감).
  const from = lockedFrom || fromChoice;
  const fromLocked = Boolean(lockedFrom);
  const lockedBackend = fromLocked
    ? fromOptions.find((option) => option.address === from)?.backend
    : undefined;
  // 저장된 초안이 있으면 그걸 우선 복원하고, 없으면 기존 prefill(답장 등)을 쓴다.
  const [to, setTo] = useState(savedDraft?.to ?? prefill?.to ?? "");
  const [cc, setCc] = useState(savedDraft?.cc ?? prefill?.cc ?? "");
  const [bcc, setBcc] = useState(savedDraft?.bcc ?? "");
  const [showCc, setShowCc] = useState(savedDraft?.showCc ?? Boolean(prefill?.cc));
  const [subject, setSubject] = useState(savedDraft?.subject ?? prefill?.subject ?? "");
  const [html, setHtml] = useState(savedDraft?.html ?? prefill?.html ?? "");
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState<{ tone: "pass" | "fail"; message: string } | null>(null);
  const [teamOpen, setTeamOpen] = useState(false);
  const [teamQuery, setTeamQuery] = useState("");
  const [teamMembers, setTeamMembers] = useState<TeamMember[]>([]);
  const [teamLoading, setTeamLoading] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const editorRef = useRef<MailRichEditorHandle>(null);
  const replyToId = prefill?.replyToId;
  const isReply = Boolean(replyToId);

  // 입력이 바뀔 때마다 저장 — 메일함/폴더 전환(언마운트), 탭 닫기, 새로고침을
  // 별도 처리 없이 한 번에 커버한다.
  useEffect(() => {
    writeDraftStorage(draftKey, { to, cc, bcc, showCc, subject, html } satisfies ComposeDraftData);
  }, [draftKey, to, cc, bcc, showCc, subject, html]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setTeamLoading(true);
      try {
        const rows = await api.listTeamMembers();
        const activeMembers = rows.filter((member) => member.active && teamMemberEmails(member).length);
        if (activeMembers.length) {
          if (!cancelled) setTeamMembers(activeMembers);
          return;
        }
        const allowlist = await api.listAllowlist();
        if (!cancelled) {
          setTeamMembers(
            allowlist.map(allowlistAsTeamMember).filter((member) => member.active && teamMemberEmails(member).length),
          );
        }
      } catch {
        if (!cancelled) setTeamMembers([]);
      } finally {
        if (!cancelled) setTeamLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const filteredTeamMembers = useMemo(() => {
    const needle = teamQuery.trim().toLowerCase();
    if (!needle) return teamMembers;
    return teamMembers.filter((member) => {
      const blob = [
        member.name,
        member.title,
        member.department,
        member.email,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return blob.includes(needle);
    });
  }, [teamMembers, teamQuery]);

  function addRecipients(field: RecipientField, emails: string[]) {
    const clean = splitRecipientList(emails.join(", "));
    if (!clean.length) return;
    if (field === "to") setTo((prev) => appendRecipients(prev, clean));
    if (field === "cc") {
      setShowCc(true);
      setCc((prev) => appendRecipients(prev, clean));
    }
    if (field === "bcc") {
      setShowCc(true);
      setBcc((prev) => appendRecipients(prev, clean));
    }
  }

  // 취소/X처럼 명시적으로 닫을 때만 확인한다 — 메일함/폴더 전환은 초안이
  // localStorage에 이미 저장돼 있어 별도 확인 없이 그냥 돌아오면 복원된다.
  function hasDraftContent(): boolean {
    return Boolean(to.trim() || cc.trim() || bcc.trim() || subject.trim() || htmlToPlainText(html).trim());
  }

  function requestClose() {
    if (hasDraftContent() && !window.confirm("작성 중인 내용이 있습니다. 저장하지 않고 닫을까요?")) {
      return;
    }
    removeDraftStorage(draftKey);
    onClose();
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // 명함/서명은 orthus가 붙이지 않는다 — nova/acme 메일 시스템의
    // 명함이 발송 측에서 처리되므로 본문을 그대로 보낸다.
    const htmlForSend = html;
    const plain = htmlToPlainText(htmlForSend);
    if (!plain) {
      setNotice({ tone: "fail", message: "내용을 입력하세요" });
      editorRef.current?.focus();
      return;
    }
    setSending(true);
    setNotice(null);
    try {
      // 서식 포함 본문은 html로, plain text는 fallback으로 함께 보낸다.
      const result = await api.sendMail({
        from_addr: from,
        to: to.trim(),
        subject,
        text: plain,
        html: htmlForSend,
        cc: cc.trim() || undefined,
        bcc: bcc.trim() || undefined,
        reply_to_id: replyToId || undefined,
      });
      if (result.status === "sent") {
        setNotice({ tone: "pass", message: `${BACKEND_LABELS[result.backend]} 발송 완료` });
        setTo("");
        setCc("");
        setBcc("");
        setSubject("");
        setHtml("");
        removeDraftStorage(draftKey);
        // 보낸 메일이 다음 자동 새로고침(15초)까지 보낸편지함에 없던 공백 제거.
        onSent?.();
      } else {
        setNotice({ tone: "fail", message: result.error ?? "발송 실패" });
      }
    } catch (e) {
      setNotice({ tone: "fail", message: e instanceof Error ? e.message : "발송 실패" });
    } finally {
      setSending(false);
    }
  }

  return (
    <Card className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div
        className="flex h-12 shrink-0 items-center justify-between px-4"
        style={{ borderBottom: "1px solid var(--color-divider)" }}
      >
        <span className="text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
          {isReply ? "답장" : "새 메일"}
        </span>
        <button
          aria-label="닫기"
          className="flex min-h-[44px] min-w-[44px] items-center justify-center"
          onClick={requestClose}
          type="button"
        >
          <X className="h-4 w-4" style={{ color: "var(--color-text-muted)" }} />
        </button>
      </div>

      <form className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-4 py-4" onSubmit={submit}>
        {notice ? (
          <Banner tone={notice.tone} title={notice.tone === "pass" ? "완료" : "오류"}>
            {notice.message}
          </Banner>
        ) : null}
        {/* AI 작성 바는 제거(간편 작성 우선) — 답장일 때 원본 보기만 남긴다. */}
        {isReply && prefill?.context ? (
          <div>
            <button
              className="text-[var(--text-meta)] font-semibold"
              onClick={() => setContextOpen((open) => !open)}
              style={{ color: "var(--color-text-muted)" }}
              type="button"
            >
              {contextOpen ? "원본 메일 숨기기" : "원본 메일 보기"}
            </button>
            {contextOpen ? (
              <pre
                className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-words text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                {prefill.context}
              </pre>
            ) : null}
          </div>
        ) : null}
        <ComposeRow label="보내는 사람">
          {fromLocked ? (
            <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
              <span className="truncate text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
                {from}
              </span>
              {lockedBackend ? <Chip tone="muted">{mailboxLabel(lockedBackend, from)}</Chip> : null}
            </div>
          ) : (
            <select
              className={cx(inputClass, "min-h-[44px] min-w-0 flex-1")}
              onChange={(event) => setFromChoice(event.target.value)}
              style={inputStyle}
              value={from}
            >
              {fromOptions.map((option) => (
                <option key={option.address} value={option.address}>
                  {option.address} ({mailboxLabel(option.backend, option.address)})
                </option>
              ))}
            </select>
          )}
        </ComposeRow>
        <ComposeRow label="받는 사람">
          <input
            className={cx(inputClass, "min-h-[44px] min-w-0 flex-1")}
            onChange={(event) => setTo(event.target.value)}
            placeholder="recipient@example.com, teammate@acme.example"
            required
            style={inputStyle}
            type="text"
            value={to}
          />
          {/* 팀 주소록은 참조처럼 받는 사람 줄 오른쪽 텍스트 버튼으로 연다 —
              상시 한 줄을 차지하던 팀 메일 바 제거(owner 피드백 2026-07-05). */}
          <button
            type="button"
            onClick={() => setTeamOpen((open) => !open)}
            className="inline-flex min-h-[44px] shrink-0 items-center gap-1 text-[var(--text-meta)] font-semibold"
            style={{ color: "var(--color-progress,#2563eb)" }}
          >
            <Users className="h-3.5 w-3.5" />
            {teamOpen ? "팀메일 닫기" : "팀메일"}
          </button>
          <button
            type="button"
            onClick={() => setShowCc((v) => !v)}
            className="min-h-[44px] shrink-0 text-[var(--text-meta)] font-semibold"
            style={{ color: "var(--color-progress,#2563eb)" }}
          >
            {showCc ? "참조 숨기기" : "참조"}
          </button>
        </ComposeRow>
        {teamOpen ? (
          <div
            className="rounded-[8px]"
            style={{ background: "var(--color-app-canvas)", border: "1px solid var(--color-divider)" }}
          >
            <div className="grid gap-2 px-3 py-3">
                <input
                  className={cx(inputClass, "min-h-[40px]")}
                  onChange={(event) => setTeamQuery(event.target.value)}
                  placeholder="이름, 직함, 이메일 검색"
                  style={inputStyle}
                  value={teamQuery}
                />
                <div className="flex flex-wrap gap-1.5">
                  <ToolbarButton
                    className="h-8"
                    onClick={() => addRecipients("to", filteredTeamMembers.flatMap(teamMemberEmails))}
                    type="button"
                    disabled={!filteredTeamMembers.length}
                  >
                    전체 To
                  </ToolbarButton>
                  <ToolbarButton
                    className="h-8"
                    onClick={() => addRecipients("cc", filteredTeamMembers.flatMap(teamMemberEmails))}
                    type="button"
                    disabled={!filteredTeamMembers.length}
                  >
                    전체 CC
                  </ToolbarButton>
                </div>
                {teamLoading ? (
                  <div className="py-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
                    팀 주소록 불러오는 중
                  </div>
                ) : filteredTeamMembers.length ? (
                  <div className="max-h-48 overflow-y-auto">
                    {filteredTeamMembers.map((member) => {
                      const emails = teamMemberEmails(member);
                      return (
                        <div
                          key={member.member_id}
                          className="py-1.5"
                          style={{ borderTop: "1px solid var(--color-divider-soft)" }}
                        >
                          <div className="truncate text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
                            {member.name}
                            {member.title ? <span style={{ color: "var(--color-text-muted)" }}> · {member.title}</span> : null}
                          </div>
                          {/* 주소별 칩: 주소를 누르면 받는 사람, CC를 누르면 참조에 그 주소만 추가.
                              (한 사람이 nova/acme/개인 gmail 여러 주소를 가질 수 있어
                              사람 단위 일괄 추가 대신 주소 단위 선택으로 둔다.) */}
                          <div className="mt-1 flex flex-wrap gap-1.5">
                            {emails.map((email) => (
                              <span
                                key={email}
                                className="inline-flex max-w-full items-stretch overflow-hidden rounded-full"
                                style={{ border: "1px solid var(--color-divider)", background: "var(--color-card)" }}
                              >
                                <button
                                  className="min-h-[32px] min-w-0 truncate px-2.5 text-[var(--text-meta)] font-semibold"
                                  onClick={() => addRecipients("to", [email])}
                                  style={{ color: "var(--color-text-body)" }}
                                  title="받는 사람에 추가"
                                  type="button"
                                >
                                  {email}
                                </button>
                                <button
                                  className="min-h-[32px] shrink-0 px-2 text-[var(--text-meta)] font-semibold"
                                  onClick={() => addRecipients("cc", [email])}
                                  style={{ borderLeft: "1px solid var(--color-divider)", color: "var(--color-text-muted)" }}
                                  title="참조에 추가"
                                  type="button"
                                >
                                  CC
                                </button>
                              </span>
                            ))}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <div className="py-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
                    표시할 팀원 메일주소가 없습니다.
                  </div>
                )}
            </div>
          </div>
        ) : null}
        {showCc ? (
          <>
            <ComposeRow label="참조">
              <input
                className={cx(inputClass, "min-h-[44px] min-w-0 flex-1")}
                onChange={(event) => setCc(event.target.value)}
                placeholder="cc1@example.com, cc2@example.com"
                style={inputStyle}
                value={cc}
              />
            </ComposeRow>
            <ComposeRow label="숨은참조">
              <input
                className={cx(inputClass, "min-h-[44px] min-w-0 flex-1")}
                onChange={(event) => setBcc(event.target.value)}
                placeholder="bcc@example.com"
                style={inputStyle}
                value={bcc}
              />
            </ComposeRow>
          </>
        ) : null}
        <ComposeRow label="제목">
          <input
            className={cx(inputClass, "min-h-[44px] min-w-0 flex-1")}
            onChange={(event) => setSubject(event.target.value)}
            required
            style={inputStyle}
            value={subject}
          />
        </ComposeRow>
        <MailRichEditor
          ref={editorRef}
          html={html}
          onChange={setHtml}
          minHeight={320}
          className="flex-1"
        />
        {/* 메일 명함은 nova/acme 메일 시스템 쪽 명함을 쓴다 — orthus 자체
            명함 버튼/자동 포함은 제거(간편 작성). */}
        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 pt-1">
          <ToolbarButton className="min-h-[44px]" onClick={requestClose} type="button">
            취소
          </ToolbarButton>
          <ToolbarButton
            className="min-h-[44px]"
            disabled={sending || !from || !to.trim()}
            icon={<Send size={15} />}
            type="submit"
          >
            {sending ? "발송 중" : "보내기"}
          </ToolbarButton>
        </div>
      </form>
    </Card>
  );
}

/* ----------------------------------------------------------------- shared */

function LoadingRows() {
  return (
    <div className="grid gap-3 p-3">
      {Array.from({ length: 6 }).map((_, index) => (
        <div key={index} className="grid gap-2">
          <Skeleton className="h-4 w-4/5" />
          <Skeleton className="h-3 w-2/3" />
          <Skeleton className="h-5 w-1/3" />
        </div>
      ))}
    </div>
  );
}

function EmptyInbox({ hasQuery }: { hasQuery: boolean }) {
  return (
    <div className="flex min-h-[280px] items-center justify-center px-4 text-center">
      <div>
        <Inbox className="mx-auto h-8 w-8" style={{ color: "var(--color-text-muted)" }} />
        <div className="mt-3 text-[var(--text-body-sm)] font-semibold" style={{ color: "var(--color-text-strong)" }}>
          {hasQuery ? "검색 결과 없음" : "메일 없음"}
        </div>
      </div>
    </div>
  );
}

function MetaLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[54px_minmax(0,1fr)] gap-2">
      <span
        className="font-[family-name:var(--font-mono)] text-[10px] uppercase"
        style={{ color: "var(--color-text-muted)" }}
      >
        {label}
      </span>
      <span className="min-w-0 break-words">{value || "-"}</span>
    </div>
  );
}

/* --------------------------------------------------------------- helpers */

function companyKey(item: CanonicalEmail) {
  return `${item.account_id ?? item.backend}:${item.external_id}`;
}

// "Name <email>" → "email"; bare address passes through.
function extractEmail(addr: string): string {
  const match = addr.match(/<([^>]+)>/);
  return (match ? match[1] : addr).trim();
}

function reSubject(subject: string): string {
  const base = subject.replace(/^\s*(re:\s*)+/i, "").trim();
  return `Re: ${base || "(제목 없음)"}`;
}

function matchesMailbox(item: CanonicalEmail, mailbox: Mailbox | undefined): boolean {
  if (!mailbox || mailbox.kind === "all") return true;
  if (mailbox.kind !== "company") return false;
  if (mailbox.accountId) return item.account_id === mailbox.accountId;
  if (mailbox.backend && item.backend !== mailbox.backend) return false;
  if (mailbox.addr && item.owner_addr && item.owner_addr !== mailbox.addr) return false;
  return true;
}

function matchesFolder(item: CanonicalEmail, folder: FolderKey): boolean {
  switch (folder) {
    case "inbox":
      return !item.trashed && item.direction === "inbound";
    case "aidraft":
      return !item.trashed && item.direction === "inbound" && !item.replied;
    case "sent":
      return !item.trashed && item.direction === "outbound";
    case "starred":
      return item.starred && !item.trashed;
    case "attachments":
      // 문서함: 첨부(받은 문서)가 있는 메일을 한곳에 모아 본다.
      return !item.trashed && item.attachment_count > 0;
    case "trash":
      return item.trashed;
  }
}

function companyToVM(item: CanonicalEmail): ListVM {
  return {
    key: companyKey(item),
    subject: item.subject || "(제목 없음)",
    preview: item.direction === "outbound" ? `받는 사람: ${item.to_addr.join(", ")}` : item.from_addr,
    date: item.received_at ?? item.sent_at,
    unread: item.direction === "inbound" && !item.read,
    starred: item.starred,
    replied: item.replied,
    outbound: item.direction === "outbound",
    attachment: item.attachment_count > 0,
    attachmentCount: item.attachment_count,
    scope: item.scope,
    // 전체함에서 각 메일이 어느 메일함(2fe@… / biz@… 등) 메일인지 구분되도록
    // 백엔드 이름 대신 메일함 주소를 우선 표기한다.
    backendLabel: item.owner_addr || BACKEND_LABELS[item.backend],
  };
}

function personalToVM(item: PersonalMailItem): ListVM {
  return {
    key: item.doc_id,
    subject: item.subject || item.title || "(제목 없음)",
    preview: item.snippet || "-",
    date: item.sent_at,
    unread: false,
    starred: false,
    replied: false,
    outbound: false,
    attachment: false,
    attachmentCount: 0,
    scope: "personal",
    backendLabel: "개인 Gmail",
  };
}

function formatDate(value: string | null) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return DATE_FORMAT.format(date);
}

function formatBytes(bytes: number) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function mailBodyText(item: CanonicalEmail): string {
  const htmlBody = htmlToReadableText(item.body_html);
  if (htmlBody) return htmlBody;
  return normalizeMailText(item.body_text);
}

function htmlToReadableText(value: string): string {
  const decoded = decodeHtmlEntities(value);
  if (!decoded) return "";

  if (typeof document === "undefined") {
    return normalizeMailText(
      decoded
        .replace(/<br\s*\/?>/gi, "\n")
        .replace(/<\/(p|div|li|tr|table|blockquote|h[1-6])>/gi, "\n")
        .replace(/<[^>]+>/g, " "),
    );
  }

  const root = document.createElement("div");
  root.innerHTML = decoded;
  root.querySelectorAll("script,style,iframe,object,embed,link,meta,form,input,button").forEach((node) => node.remove());
  root.querySelectorAll("br").forEach((br) => br.replaceWith("\n"));
  root.querySelectorAll("p,div,li,tr,table,blockquote,h1,h2,h3,h4,h5,h6").forEach((node) => node.append("\n"));
  return normalizeMailText(root.textContent ?? "");
}

function normalizeMailText(value: string): string {
  const decoded = decodeHtmlEntities(value)
    .replace(/\u00a0/g, " ")
    .replace(/\r\n?/g, "\n")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n[ \t]+/g, "\n")
    .replace(/[ \t]{2,}/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  if (!looksLikeHtml(decoded)) return decoded;
  return htmlToReadableText(decoded);
}

function decodeHtmlEntities(value: string): string {
  let out = value;
  for (let i = 0; i < 3; i += 1) {
    const next = decodeHtmlEntitiesOnce(out);
    if (next === out) break;
    out = next;
  }
  return out;
}

function decodeHtmlEntitiesOnce(value: string): string {
  if (!value) return "";
  if (typeof document !== "undefined") {
    const textarea = document.createElement("textarea");
    textarea.innerHTML = value;
    return textarea.value;
  }
  return value
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCodePoint(Number.parseInt(code, 16)));
}

function looksLikeHtml(value: string): boolean {
  return /<\/?[a-z][\w:-]*(\s|>|\/>)/i.test(value);
}
