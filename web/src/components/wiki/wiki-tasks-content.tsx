"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  api,
  ApiError,
  type AuthMe,
  type WikiTask,
  type WikiTaskResolveBody,
  type WikiTaskResolveDecision,
} from "@/lib/api";
import {
  Banner,
  Card,
  Chip,
  PageHeader,
  Skeleton,
  Toolbar,
  ToolbarButton,
  inputClass,
  inputStyle,
} from "@/components/ui";

type TaskFilter = "open" | "resolved" | "all";

const FILTERS: TaskFilter[] = ["open", "resolved", "all"];
const FILTER_LABEL: Record<TaskFilter, string> = {
  open: "처리할 항목",
  resolved: "처리됨",
  all: "전체",
};
const COMPACT_WIKI_TASKS_WIDTH = 760;

// 사용자에게 보이는 친화적 제목/설명. 내부 용어(claim, incoming, wiki 등)는
// 노출하지 않는다.
const KIND_FRIENDLY: Record<
  WikiTask["kind"],
  { title: string; intro: string }
> = {
  conflict: {
    title: "같은 내용이 서로 달라요",
    intro:
      "이미 저장돼 있던 내용과 이번에 새로 확인된 내용이 서로 달라요. 어떤 내용을 남길지 골라 주세요.",
  },
  entity_conflict: {
    title: "같은 항목이 서로 달라요",
    intro:
      "같은 항목에 대해 저장된 내용과 새로 확인된 내용이 달라요. 어떤 내용을 남길지 골라 주세요.",
  },
  open_question: {
    title: "확인이 필요한 내용이에요",
    intro: "아래 질문에 답을 적어 주면 내용에 반영돼요.",
  },
  stale_audit: {
    title: "오래된 내용 점검",
    intro: "오래돼서 다시 확인하면 좋을 내용이에요. 확인했으면 완료해 주세요.",
  },
  dedup: {
    title: "겹치는 내용 정리",
    intro: "비슷한 내용이 중복돼 있어요. 확인했으면 완료해 주세요.",
  },
  provenance_fix: {
    title: "출처 정리",
    intro: "내용의 출처를 정리하면 좋아요. 확인했으면 완료해 주세요.",
  },
};

const DECISION_LABEL: Record<WikiTaskResolveDecision, string> = {
  keep_existing: "기존 내용 유지",
  use_incoming: "새 내용으로 변경",
  merge: "직접 정리",
  answered: "답변함",
  cleanup: "정리 완료",
  dismissed: "닫음",
};

// conflict description은 자동 생성된 영문 기술 문구
// (`... existing: '...'; incoming: '...'. Not overwritten.`)다. 사용자에게는
// 원문 대신 "저장된 내용 / 새 내용"으로 풀어서 보여주려고 본문만 뽑아낸다.
function parseConflict(
  description: string,
  incomingClaim?: string | null,
): { existing: string | null; incoming: string | null } {
  const existing =
    description.match(/existing:\s*(['"])([\s\S]*?)\1\s*;\s*incoming:/)?.[2] ??
    null;
  const incoming =
    incomingClaim ??
    description.match(
      /incoming:\s*(['"])([\s\S]*?)\1\s*\.?\s*(?:Not overwritten|$)/,
    )?.[2] ??
    null;
  return { existing, incoming };
}

const CLEANUP_KINDS: ReadonlyArray<WikiTask["kind"]> = [
  "stale_audit",
  "dedup",
  "provenance_fix",
  "entity_conflict",
];

function isCleanupKind(kind: WikiTask["kind"]): boolean {
  return CLEANUP_KINDS.includes(kind);
}

function isContentMutatingRole(role: string | null | undefined): boolean {
  return role === "owner" || role === "admin";
}

export function WikiTasksContent({ showHeader = true }: { showHeader?: boolean }) {
  const compact = useCompactWikiTasks();
  const [filter, setFilter] = useState<TaskFilter>("open");
  const [tasks, setTasks] = useState<WikiTask[]>([]);
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [compactDetailOpen, setCompactDetailOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busySlug, setBusySlug] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [me, setMe] = useState<AuthMe | null>(null);
  const [meLoaded, setMeLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .getAuthMe()
      .then((value) => {
        if (cancelled) return;
        setMe(value);
      })
      .catch(() => {
        // Conservative fallback: leave me=null; UI gates use !me as "unknown role".
        if (cancelled) return;
        setMe(null);
      })
      .finally(() => {
        if (cancelled) return;
        setMeLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function refreshTasks() {
    setLoading(true);
    setError(null);
    try {
      const resolved =
        filter === "open" ? false : filter === "resolved" ? true : undefined;
      const rows = await api.listWikiTasks({ resolved });
      setTasks(rows);
      setSelectedSlug((current) => current ?? rows[0]?.slug ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "위키 태스크를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;

    async function loadInitial() {
      setLoading(true);
      setError(null);
      try {
        const resolved =
          filter === "open" ? false : filter === "resolved" ? true : undefined;
        const rows = await api.listWikiTasks({ resolved });
        if (cancelled) return;
        setTasks(rows);
        setSelectedSlug((current) => current ?? rows[0]?.slug ?? null);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : "위키 태스크를 불러오지 못했습니다.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void loadInitial();

    return () => {
      cancelled = true;
    };
  }, [filter]);

  const selected = useMemo(
    () => tasks.find((task) => task.slug === selectedSlug) ?? tasks[0] ?? null,
    [selectedSlug, tasks],
  );

  function applyUpdated(updated: WikiTask) {
    setTasks((prev) => {
      // If the resolved-filter no longer matches, drop it from the list.
      if (filter === "open" && updated.resolved) {
        return prev.filter((row) => row.slug !== updated.slug);
      }
      if (filter === "resolved" && !updated.resolved) {
        return prev.filter((row) => row.slug !== updated.slug);
      }
      const exists = prev.some((row) => row.slug === updated.slug);
      return exists
        ? prev.map((row) => (row.slug === updated.slug ? updated : row))
        : [updated, ...prev];
    });
    setSelectedSlug(updated.slug);
  }

  async function setResolved(task: WikiTask, resolved: boolean) {
    setBusySlug(task.slug);
    setError(null);
    try {
      const updated = await api.setWikiTaskResolved(task.slug, resolved);
      applyUpdated(updated);
    } catch (e) {
      setError(formatError(e, "태스크 상태 변경 실패"));
    } finally {
      setBusySlug(null);
    }
  }

  async function resolveTask(task: WikiTask, body: WikiTaskResolveBody) {
    setBusySlug(task.slug);
    setError(null);
    try {
      const updated = await api.resolveWikiTask(task.slug, body);
      applyUpdated(updated);
    } catch (e) {
      setError(formatError(e, "태스크 처리 실패"));
    } finally {
      setBusySlug(null);
    }
  }

  const openCount = tasks.filter((task) => !task.resolved).length;
  const subtitle = `전체 ${tasks.length}개${openCount > 0 ? ` · 처리할 항목 ${openCount}개` : ""}`;
  const controls = (
    <TaskControls
      compact={compact}
      filter={filter}
      onFilterChange={(next) => {
        setFilter(next);
        setCompactDetailOpen(false);
      }}
      onRefresh={() => void refreshTasks()}
    />
  );
  const showList = !compact || !compactDetailOpen;
  const showDetail = !compact || compactDetailOpen;
  const contentClass = compact
    ? showHeader
      ? "min-h-0 flex-1 overflow-y-auto p-3 pb-[calc(var(--mobile-nav-height)+16px)]"
      : "min-h-[420px]"
    : showHeader
      ? "grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-y-auto p-3 sm:p-5 lg:grid-cols-[360px_minmax(0,1fr)] lg:overflow-hidden"
      : "grid min-h-[520px] grid-cols-1 gap-4 lg:grid-cols-[360px_minmax(0,1fr)]";
  const listSectionClass = compact
    ? showList
      ? "min-h-0"
      : "hidden"
    : "min-h-0 overflow-y-auto";
  const detailSectionClass = compact
    ? showDetail
      ? "min-h-0"
      : "hidden"
    : "min-h-0 overflow-y-auto";

  return (
    <div className="flex h-full min-h-0 flex-col">
      {showHeader ? (
        <PageHeader title="위키 태스크" subtitle={subtitle} right={controls} />
      ) : (
        <div className={`mb-3 flex flex-wrap items-center justify-between gap-3 px-1 ${compact ? "flex-col items-stretch" : ""}`}>
          <div className="min-w-0">
            <h2
              className="text-[var(--text-body)] font-bold"
              style={{ color: "var(--color-text-strong)" }}
            >
              위키 태스크
            </h2>
            <p
              className="mt-0.5 text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {subtitle}
            </p>
          </div>
          {controls}
        </div>
      )}

      <div className={contentClass}>
        <section className={listSectionClass}>
          {error ? (
            <div className="mb-3">
              <Banner tone="fail" title="실패">
                <span className="break-all">{error}</span>
              </Banner>
            </div>
          ) : null}

          {loading ? (
            <div className="space-y-2">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-[78px] w-full" />
              ))}
            </div>
          ) : tasks.length === 0 ? (
            <Card
              className="p-3 text-[var(--text-body-sm)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              항목이 없어요
            </Card>
          ) : (
            <div className="space-y-2">
              {tasks.map((task) => (
                <TaskListItem
                  key={task.slug}
                  task={task}
                  selected={selected?.slug === task.slug}
                  onSelect={() => {
                    setSelectedSlug(task.slug);
                    setCompactDetailOpen(true);
                  }}
                />
              ))}
            </div>
          )}
        </section>

        <section className={detailSectionClass}>
          {selected ? (
            <TaskDetail
              compact={compact}
              task={selected}
              busy={busySlug === selected.slug}
              role={me?.role ?? null}
              meLoaded={meLoaded}
              onBack={() => setCompactDetailOpen(false)}
              onResolve={(body) => void resolveTask(selected, body)}
              onReopen={() => void setResolved(selected, false)}
            />
          ) : (
            <Card
              className="flex h-full min-h-[260px] items-center justify-center p-6 text-[var(--text-body-sm)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              왼쪽에서 항목을 선택하세요
            </Card>
          )}
        </section>
      </div>
    </div>
  );
}

function formatError(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    const base = e.message || fallback;
    return `${base} (HTTP ${e.status})`;
  }
  if (e instanceof Error) return e.message;
  return fallback;
}

function useCompactWikiTasks() {
  const [compact, setCompact] = useState(false);

  useEffect(() => {
    const sync = () => {
      setCompact(window.innerWidth < COMPACT_WIKI_TASKS_WIDTH);
    };
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  return compact;
}

function TaskControls({
  compact,
  filter,
  onFilterChange,
  onRefresh,
}: {
  compact: boolean;
  filter: TaskFilter;
  onFilterChange: (filter: TaskFilter) => void;
  onRefresh: () => void;
}) {
  const buttons = (
    <>
      {FILTERS.map((item) => (
        <ToolbarButton
          className={compact ? "h-11 min-h-11 flex-1 justify-center px-3" : undefined}
          key={item}
          tone={filter === item ? "primary" : "default"}
          onClick={() => onFilterChange(item)}
        >
          {FILTER_LABEL[item]}
        </ToolbarButton>
      ))}
      <ToolbarButton
        className={compact ? "h-11 min-h-11 flex-1 justify-center px-3" : undefined}
        onClick={onRefresh}
      >
        새로고침
      </ToolbarButton>
    </>
  );

  if (compact) {
    return <div className="flex w-full flex-wrap items-center gap-2">{buttons}</div>;
  }

  return <Toolbar>{buttons}</Toolbar>;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="mb-2 text-[var(--text-meta)] font-semibold"
      style={{ color: "var(--color-text-muted)" }}
    >
      {children}
    </div>
  );
}

// open_question 리뷰어가 답을 적기 전에 무엇이 이미 알려져 있는지 보여주는
// 출처 컨텍스트 섹션. source_excerpt는 distilled 요약, related는 source/wiki
// 슬러그 chip 목록. 둘 다 비어 있으면 아무것도 렌더하지 않는다.
function OpenQuestionSourceSection({ task }: { task: WikiTask }) {
  const excerpt = task.source_excerpt?.trim() ?? "";
  const related = task.related ?? [];
  if (!excerpt && related.length === 0) return null;
  return (
    <Card className="p-4">
      <SectionLabel>출처</SectionLabel>
      {excerpt ? (
        <div className="mb-3">
          <div
            className="mb-1 text-[var(--text-meta)] font-medium"
            style={{ color: "var(--color-text-muted)" }}
          >
            출처 내용
          </div>
          <div
            className="rounded-[6px] p-3 text-[var(--text-body-sm)] leading-[1.6] whitespace-pre-wrap break-words [overflow-wrap:anywhere]"
            style={{
              background: "var(--color-sidebar-active)",
              color: "var(--color-text-body)",
            }}
          >
            {excerpt}
          </div>
        </div>
      ) : null}
      {related.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {related.map((slug) => {
            const isSource = slug.startsWith("src-");
            const text = isSource ? `출처: ${slug}` : slug;
            const chipClass =
              "inline-flex min-h-[28px] max-w-full min-w-0 items-center rounded-[4px] px-2 py-1 text-[var(--text-meta)] font-medium break-all [overflow-wrap:anywhere]";
            if (isSource) {
              return (
                <span
                  key={slug}
                  className={chipClass}
                  style={{
                    background: "var(--color-card)",
                    color: "var(--color-text-body)",
                    border: "1px solid var(--color-divider)",
                  }}
                >
                  {text}
                </span>
              );
            }
            return (
              <Link
                key={slug}
                href={`/wiki/${slug}`}
                className={chipClass}
                style={{
                  background: "var(--color-sidebar-active)",
                  color: "var(--color-text-body)",
                }}
              >
                {text}
              </Link>
            );
          })}
        </div>
      ) : null}
    </Card>
  );
}

function CompareCard({
  empty,
  label,
  text,
  tone,
}: {
  empty: string;
  label: string;
  text: string | null;
  tone: "existing" | "incoming";
}) {
  return (
    <Card className="p-4">
      <div className="mb-2 flex items-center gap-1.5">
        <span
          className="inline-block h-2 w-2 rounded-full"
          style={{
            background:
              tone === "incoming"
                ? "var(--color-warn-fg)"
                : "var(--color-text-muted)",
          }}
        />
        <span
          className="text-[var(--text-meta)] font-semibold"
          style={{ color: "var(--color-text-muted)" }}
        >
          {label}
        </span>
      </div>
      {text ? (
        <p
          className="whitespace-pre-wrap break-words text-[var(--text-body)] leading-[1.6] [overflow-wrap:anywhere]"
          style={{ color: "var(--color-text-body)" }}
        >
          {text}
        </p>
      ) : (
        <p
          className="text-[var(--text-body-sm)]"
          style={{ color: "var(--color-text-faint)" }}
        >
          {empty}
        </p>
      )}
    </Card>
  );
}

function TaskListItem({
  task,
  selected,
  onSelect,
}: {
  task: WikiTask;
  selected: boolean;
  onSelect: () => void;
}) {
  const friendly = KIND_FRIENDLY[task.kind];
  const preview =
    task.kind === "conflict" || task.kind === "entity_conflict"
      ? parseConflict(task.description, task.incoming_claim).existing ??
        task.incoming_claim ??
        task.description
      : task.description;
  return (
    <button type="button" onClick={onSelect} className="block w-full text-left">
      <Card
        interactive
        className="p-3"
        style={{
          outline: selected ? "2px solid var(--color-text-strong)" : "none",
          outlineOffset: "-2px",
        }}
      >
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <span
            className="min-w-0 break-words text-[var(--text-body-sm)] font-semibold [overflow-wrap:anywhere]"
            style={{ color: "var(--color-text-strong)" }}
          >
            {friendly.title}
          </span>
          <TaskStatus task={task} />
        </div>
        <div
          className="mt-1 line-clamp-2 break-words text-[var(--text-meta)] leading-snug [overflow-wrap:anywhere]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {preview}
        </div>
      </Card>
    </button>
  );
}

function TaskDetail({
  busy,
  compact,
  meLoaded,
  onBack,
  onReopen,
  onResolve,
  role,
  task,
}: {
  busy: boolean;
  compact: boolean;
  meLoaded: boolean;
  onBack: () => void;
  onReopen: () => void;
  onResolve: (body: WikiTaskResolveBody) => void;
  role: string | null;
  task: WikiTask;
}) {
  const actionButtonClass = compact
    ? "h-11 min-h-11 w-full justify-center px-3"
    : undefined;

  const friendly = KIND_FRIENDLY[task.kind];
  const isConflict = task.kind === "conflict" || task.kind === "entity_conflict";
  const conflict = isConflict
    ? parseConflict(task.description, task.incoming_claim)
    : { existing: null, incoming: null };

  return (
    <div className="space-y-3">
      {compact ? (
        <ToolbarButton
          className="h-11 min-h-11 w-full justify-center px-3"
          onClick={onBack}
        >
          목록
        </ToolbarButton>
      ) : null}
      <Card className="p-4">
        <div className={`flex items-start justify-between gap-3 ${compact ? "flex-col" : ""}`}>
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <TaskStatus task={task} />
            </div>
            <h2
              className="break-words text-[18px] font-extrabold leading-tight [overflow-wrap:anywhere]"
              style={{ color: "var(--color-text-strong)" }}
            >
              {friendly.title}
            </h2>
            <p
              className="mt-1 text-[var(--text-meta)]"
              style={{ color: "var(--color-text-muted)" }}
            >
              {new Date(task.created_at).toLocaleString("ko-KR")}
            </p>
          </div>
          {task.resolved ? (
            compact ? (
              <div className="w-full">
                <ToolbarButton
                  className={actionButtonClass}
                  disabled={busy}
                  onClick={onReopen}
                >
                  다시 열기
                </ToolbarButton>
              </div>
            ) : (
              <Toolbar>
                <ToolbarButton disabled={busy} onClick={onReopen}>
                  다시 열기
                </ToolbarButton>
              </Toolbar>
            )
          ) : null}
        </div>
        <p
          className="mt-3 text-[var(--text-body-sm)] leading-[1.6]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {friendly.intro}
        </p>
      </Card>

      {isConflict ? (
        <div className={`grid gap-3 ${compact ? "" : "sm:grid-cols-2"}`}>
          <CompareCard
            label="지금 저장된 내용"
            tone="existing"
            text={conflict.existing}
            empty="저장된 내용을 불러오지 못했어요."
          />
          <CompareCard
            label="새로 확인된 내용"
            tone="incoming"
            text={conflict.incoming}
            empty="새로 확인된 내용이 없어요."
          />
        </div>
      ) : (
        <>
          {task.kind === "open_question" ? (
            <OpenQuestionSourceSection task={task} />
          ) : null}
          <Card className="p-4">
            <SectionLabel>내용</SectionLabel>
            <p
              className="whitespace-pre-wrap break-words text-[var(--text-body)] leading-[1.6] [overflow-wrap:anywhere]"
              style={{ color: "var(--color-text-body)" }}
            >
              {task.description}
            </p>
          </Card>
        </>
      )}

      {task.resolved ? (
        <ResolutionSummary task={task} />
      ) : (
        <ResolveControls
          // 태스크 전환 시 폼 state(mergedText/answer/note/conflictChoice)를 remount로
          // 리셋한다. key가 없으면 A에 입력한 리졸브 텍스트가 다음 태스크 B로 새어
          // B claim에 write-back되는 오염·유실이 발생했다.
          key={task.slug}
          busy={busy}
          compact={compact}
          meLoaded={meLoaded}
          role={role}
          task={task}
          onResolve={onResolve}
        />
      )}
    </div>
  );
}

function ResolutionSummary({ task }: { task: WikiTask }) {
  const resolution = task.resolution;
  if (!resolution) return null;
  const decisionLabel =
    DECISION_LABEL[resolution.decision] ?? resolution.decision;
  return (
    <Card className="p-4">
      <div className="mb-2 flex items-center gap-2">
        <SectionLabel>처리 완료</SectionLabel>
        <Chip tone="pass">{decisionLabel}</Chip>
      </div>
      <dl className="space-y-1 text-[var(--text-body-sm)]" style={{ color: "var(--color-text-body)" }}>
        <div className="flex flex-wrap gap-2">
          <dt className="font-medium" style={{ color: "var(--color-text-muted)" }}>
            처리 시각
          </dt>
          <dd className="break-all">
            {new Date(resolution.resolved_at).toLocaleString("ko-KR")}
          </dd>
        </div>
        {resolution.note ? (
          <div>
            <dt className="font-medium" style={{ color: "var(--color-text-muted)" }}>
              메모
            </dt>
            <dd className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
              {resolution.note}
            </dd>
          </div>
        ) : null}
      </dl>
    </Card>
  );
}

type ConflictChoice = "keep_existing" | "use_incoming" | "merge";

function ResolveControls({
  busy,
  compact,
  meLoaded,
  onResolve,
  role,
  task,
}: {
  busy: boolean;
  compact: boolean;
  meLoaded: boolean;
  onResolve: (body: WikiTaskResolveBody) => void;
  role: string | null;
  task: WikiTask;
}) {
  const [note, setNote] = useState("");
  const [mergedText, setMergedText] = useState(task.incoming_claim ?? "");
  const [answer, setAnswer] = useState("");
  const [conflictChoice, setConflictChoice] = useState<ConflictChoice>(
    task.incoming_claim ? "use_incoming" : "keep_existing",
  );

  const buttonClass = compact
    ? "h-11 min-h-11 w-full justify-center px-3"
    : undefined;
  const textareaClass = `${inputClass} min-h-[120px] w-full whitespace-pre-wrap`;
  const noteInputClass = compact ? `${inputClass} h-11 min-h-11 w-full` : `${inputClass} w-full`;

  const canMutate = isContentMutatingRole(role);
  // If me is loaded but role is null/unknown, fall back conservatively per spec:
  // for conflict/open_question only dismiss is shown.
  const roleKnown = meLoaded;
  const allowMutating = roleKnown ? canMutate : false;

  const dismissNote = note.trim() ? { note: note.trim() } : {};

  function submitDismiss() {
    onResolve({ decision: "dismissed", ...dismissNote });
  }

  function submitCleanup() {
    onResolve({ decision: "cleanup", ...dismissNote });
  }

  function submitConflict() {
    const trimmedNote = note.trim();
    const base: WikiTaskResolveBody =
      conflictChoice === "merge"
        ? { decision: "merge", merged_text: mergedText }
        : { decision: conflictChoice };
    if (trimmedNote) base.note = trimmedNote;
    onResolve(base);
  }

  function submitAnswered() {
    const trimmedNote = note.trim();
    const body: WikiTaskResolveBody = {
      decision: "answered",
      answer,
    };
    if (trimmedNote) body.note = trimmedNote;
    onResolve(body);
  }

  // Per-kind body
  if (task.kind === "conflict") {
    const mergeEmpty = mergedText.trim().length === 0;
    const submitDisabled =
      busy ||
      !allowMutating ||
      (conflictChoice === "merge" && mergeEmpty);
    return (
      <Card className="p-4">
        <SectionLabel>어떻게 할까요?</SectionLabel>
        {!allowMutating ? (
          <div
            className="mb-3 text-[var(--text-body-sm)] leading-[1.6]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {roleKnown
              ? "내용 변경은 관리자만 할 수 있어요. 여기서는 ‘이 알림 닫기’만 가능해요."
              : "권한을 확인하는 중이에요. 잠시 후 변경할 수 있어요."}
          </div>
        ) : null}
        <fieldset className="mb-3 space-y-2" disabled={!allowMutating || busy}>
          <legend className="sr-only">처리 방법 선택</legend>
          <label className="flex items-start gap-2 text-[var(--text-body-sm)]">
            <input
              type="radio"
              className="mt-1 h-4 w-4"
              name={`conflict-${task.slug}`}
              checked={conflictChoice === "keep_existing"}
              onChange={() => setConflictChoice("keep_existing")}
            />
            <span>지금 저장된 내용 그대로 두기</span>
          </label>
          <label className="flex items-start gap-2 text-[var(--text-body-sm)]">
            <input
              type="radio"
              className="mt-1 h-4 w-4"
              name={`conflict-${task.slug}`}
              checked={conflictChoice === "use_incoming"}
              disabled={!task.incoming_claim}
              onChange={() => setConflictChoice("use_incoming")}
            />
            <span>
              새로 확인된 내용으로 바꾸기
              {!task.incoming_claim ? (
                <span
                  className="ml-1 text-[var(--text-meta)]"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  (새 내용이 없어요)
                </span>
              ) : null}
            </span>
          </label>
          <label className="flex items-start gap-2 text-[var(--text-body-sm)]">
            <input
              type="radio"
              className="mt-1 h-4 w-4"
              name={`conflict-${task.slug}`}
              checked={conflictChoice === "merge"}
              onChange={() => setConflictChoice("merge")}
            />
            <span>두 내용을 합쳐서 직접 쓰기</span>
          </label>
        </fieldset>

        {conflictChoice === "merge" ? (
          <div className="mb-3">
            <label
              className="mb-1 block text-[var(--text-meta)] font-medium"
              style={{ color: "var(--color-text-muted)" }}
              htmlFor={`merge-${task.slug}`}
            >
              남길 내용
            </label>
            <textarea
              id={`merge-${task.slug}`}
              className={textareaClass}
              style={inputStyle}
              value={mergedText}
              onChange={(e) => setMergedText(e.target.value)}
              disabled={!allowMutating || busy}
              placeholder="여기에 최종 내용을 적어 주세요"
            />
          </div>
        ) : null}

        <NoteField
          compact={compact}
          inputClassName={noteInputClass}
          note={note}
          slug={task.slug}
          onChange={setNote}
        />

        <ResolveButtonRow compact={compact}>
          <ToolbarButton
            className={buttonClass}
            tone="primary"
            disabled={submitDisabled}
            onClick={submitConflict}
          >
            이대로 저장
          </ToolbarButton>
          <ToolbarButton
            className={buttonClass}
            disabled={busy}
            onClick={submitDismiss}
          >
            이 알림 닫기
          </ToolbarButton>
        </ResolveButtonRow>
      </Card>
    );
  }

  if (task.kind === "open_question") {
    const answerEmpty = answer.trim().length === 0;
    const submitDisabled = busy || !allowMutating || answerEmpty;
    return (
      <Card className="p-4">
        <SectionLabel>답변</SectionLabel>
        {!allowMutating ? (
          <div
            className="mb-3 text-[var(--text-body-sm)] leading-[1.6]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {roleKnown
              ? "답변 등록은 관리자만 할 수 있어요. 여기서는 ‘이 알림 닫기’만 가능해요."
              : "권한을 확인하는 중이에요. 잠시 후 답변할 수 있어요."}
          </div>
        ) : null}
        <div className="mb-3">
          <textarea
            id={`answer-${task.slug}`}
            className={textareaClass}
            style={inputStyle}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            disabled={!allowMutating || busy}
            placeholder="이 질문에 대한 답을 적어 주세요"
          />
        </div>

        <NoteField
          compact={compact}
          inputClassName={noteInputClass}
          note={note}
          slug={task.slug}
          onChange={setNote}
        />

        <ResolveButtonRow compact={compact}>
          <ToolbarButton
            className={buttonClass}
            tone="primary"
            disabled={submitDisabled}
            onClick={submitAnswered}
          >
            답변 저장
          </ToolbarButton>
          <ToolbarButton
            className={buttonClass}
            disabled={busy}
            onClick={submitDismiss}
          >
            이 알림 닫기
          </ToolbarButton>
        </ResolveButtonRow>
      </Card>
    );
  }

  if (isCleanupKind(task.kind)) {
    return (
      <Card className="p-4">
        <SectionLabel>처리</SectionLabel>
        <NoteField
          compact={compact}
          inputClassName={noteInputClass}
          note={note}
          slug={task.slug}
          onChange={setNote}
        />
        <ResolveButtonRow compact={compact}>
          <ToolbarButton
            className={buttonClass}
            tone="primary"
            disabled={busy}
            onClick={submitCleanup}
          >
            정리 완료
          </ToolbarButton>
          <ToolbarButton
            className={buttonClass}
            disabled={busy}
            onClick={submitDismiss}
          >
            이 알림 닫기
          </ToolbarButton>
        </ResolveButtonRow>
      </Card>
    );
  }

  // Fallback (any future kind): dismiss only.
  return (
    <Card className="p-4">
      <SectionLabel>처리</SectionLabel>
      <NoteField
        compact={compact}
        inputClassName={noteInputClass}
        note={note}
        slug={task.slug}
        onChange={setNote}
      />
      <ResolveButtonRow compact={compact}>
        <ToolbarButton
          className={buttonClass}
          disabled={busy}
          onClick={submitDismiss}
        >
          이 알림 닫기
        </ToolbarButton>
      </ResolveButtonRow>
    </Card>
  );
}

function NoteField({
  compact,
  inputClassName,
  note,
  onChange,
  slug,
}: {
  compact: boolean;
  inputClassName: string;
  note: string;
  onChange: (value: string) => void;
  slug: string;
}) {
  return (
    <div className="mb-3">
      <label
        className="mb-1 block text-[var(--text-meta)] font-medium"
        style={{ color: "var(--color-text-muted)" }}
        htmlFor={`note-${slug}`}
      >
        note (선택)
      </label>
      <input
        id={`note-${slug}`}
        type="text"
        className={inputClassName}
        style={inputStyle}
        value={note}
        onChange={(e) => onChange(e.target.value)}
        placeholder={compact ? "메모" : "리뷰 메모 (선택)"}
      />
    </div>
  );
}

function ResolveButtonRow({
  children,
  compact,
}: {
  children: React.ReactNode;
  compact: boolean;
}) {
  if (compact) {
    return <div className="flex flex-col gap-2">{children}</div>;
  }
  return <Toolbar>{children}</Toolbar>;
}

function TaskStatus({ task }: { task: WikiTask }) {
  return (
    <Chip tone={task.resolved ? "pass" : "warn"}>
      {task.resolved ? "resolved" : "open"}
    </Chip>
  );
}
