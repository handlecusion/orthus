"use client";

import { ChevronDown } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";
import { WikiTasksContent } from "@/components/wiki/wiki-tasks-content";
import { api, type AgentWorkInboxSummary, type AskScope, type AuthConfig, type WikiAnswer } from "@/lib/api";
import { useCachedResource } from "@/lib/use-cached-resource";
import {
  Banner,
  Card,
  Chip,
  ModeBadge,
  PageHeader,
  Skeleton,
  Toolbar,
  ToolbarButton,
  cx,
  inputClass,
} from "@/components/ui";

const EXAMPLES = ["휴가 정책이 뭐야?", "경비 처리 방법은?", "보안 수칙 알려줘"];
const COMPACT_WIKI_ASK_WIDTH = 760;
type WikiHomeTab = "ask" | "tasks";

type InboxState =
  | { status: "loading"; summary: null; error: null }
  | { status: "ready"; summary: AgentWorkInboxSummary; error: null }
  | { status: "error"; summary: null; error: string };

export default function WikiPage() {
  const searchParams = useSearchParams();
  const activeTab: WikiHomeTab = searchParams.get("tab") === "tasks" ? "tasks" : "ask";
  const compactAskControls = useCompactWikiAskControls();
  // auth config는 api.ts 모듈 캐시 + SWR 캐시로 재방문 시 즉시 페인트된다.
  const { data: authConfig } = useCachedResource<AuthConfig>(
    "auth:config",
    () => api.getAuthConfig(),
    { ttlMs: 300_000 },
  );
  const [question, setQuestion] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [source, setSource] = useState("");
  // P8.5: company-node owner-scope selector. Default 'all' so the merge view
  // matches the /ask page; if the flag is off the server clamps to company.
  const [askScope, setAskScope] = useState<AskScope>("all");
  const [filtersOpenOverride, setFiltersOpenOverride] = useState<boolean | null>(null);
  const [result, setResult] = useState<WikiAnswer | null>(null);
  // 인박스는 캐시가 있으면 즉시 그리고 백그라운드 갱신한다(스켈레톤은 최초 1회만).
  const inbox = useCachedResource<AgentWorkInboxSummary>(
    activeTab === "ask" ? "wiki:inbox-summary" : null,
    () => api.getAgentWorkInboxSummary(),
  );
  const inboxState: InboxState = inbox.data
    ? { status: "ready", summary: inbox.data, error: null }
    : inbox.error
      ? {
          status: "error",
          summary: null,
          error:
            inbox.error instanceof Error
              ? inbox.error.message
              : "Agent Work inbox를 불러오지 못했습니다.",
        }
      : { status: "loading", summary: null, error: null };
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const activeFilterCount = [since, until, source.trim()].filter(Boolean).length;
  const nodeKind = authConfig?.node_kind ?? null;
  const ownerScopeEnabled = authConfig?.owner_scope_enabled === true;
  const showCompanyScopeSelector = nodeKind === "company" && ownerScopeEnabled;
  const scopeChipLabel = nodeKind === "personal" ? "전체" : nodeKind === "company" ? "회사" : "범위";
  const scopeTitle =
    nodeKind === "personal"
      ? "personal node는 개인 local 지식과 central company 지식을 함께 검색합니다."
      : "company node는 회사 지식만 검색합니다.";
  const filtersOpen = filtersOpenOverride ?? !compactAskControls;

  async function ask(q?: string) {
    const query = (q ?? question).trim();
    if (!query) return;
    setQuestion(query);
    setLoading(true);
    setError(null);
    try {
      const res = await api.askWiki(query, {
        since: since || undefined,
        until: until || undefined,
        source: source.trim() || undefined,
        scope: showCompanyScopeSelector ? askScope : undefined,
      });
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "질의에 실패했습니다.");
      setResult(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        title="위키"
        subtitle="사내 문서를 근거로 질문에 답합니다. 모든 답변에는 출처가 붙습니다."
        right={
          <Toolbar>
            {showCompanyScopeSelector ? (
              <WikiScopeSelector value={askScope} onChange={setAskScope} />
            ) : (
              <ToolbarChip title={scopeTitle}>{scopeChipLabel}</ToolbarChip>
            )}
            {!compactAskControls ? (
              <ToolbarButton icon={<SearchIcon />} aria-label="문서 범위">
                {activeFilterCount ? `필터 ${activeFilterCount}` : "모든 문서"}
              </ToolbarButton>
            ) : null}
          </Toolbar>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[820px] px-3 py-3 sm:px-5 sm:py-5">
          <WikiHomeTabs activeTab={activeTab} />

          {activeTab === "tasks" ? (
            <WikiTasksContent showHeader={false} />
          ) : (
            <>
              <PendingKnowledgeWorkInbox state={inboxState} />

              <Card className="p-3">
                <label htmlFor="wiki-q" className="sr-only">
                  질문
                </label>
                <textarea
                  id="wiki-q"
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  onKeyDown={(e) => {
                    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void ask();
                  }}
                  rows={2}
                  placeholder="예) 연차 휴가는 며칠인가요?"
                  className={cx(
                    inputClass,
                    "resize-none border-transparent bg-transparent px-0 py-1.5",
                  )}
                  style={{ border: "1px solid transparent" }}
                />
                <div className={compactAskControls ? "mt-2 flex flex-col items-stretch gap-2" : "mt-2 flex flex-wrap items-center justify-between gap-2"}>
                  <div className="flex flex-wrap gap-1.5">
                    {EXAMPLES.map((ex) => (
                      <ToolbarButton
                        key={ex}
                        onClick={() => void ask(ex)}
                        className="h-7 px-2 text-[var(--text-meta)]"
                      >
                        {ex}
                      </ToolbarButton>
                    ))}
                  </div>
                  <ToolbarButton
                    tone="primary"
                    onClick={() => void ask()}
                    disabled={loading || !question.trim()}
                    className={compactAskControls ? "h-11 justify-center" : undefined}
                  >
                    {loading ? "찾는 중…" : "질문 (⌘↵)"}
                  </ToolbarButton>
                </div>
                <WikiAskFilterDisclosure
                  activeFilterCount={activeFilterCount}
                  compact={compactAskControls}
                  filtersOpen={filtersOpen}
                  onOpenChange={setFiltersOpenOverride}
                  onReset={() => {
                    setSince("");
                    setUntil("");
                    setSource("");
                  }}
                  setSince={setSince}
                  setSource={setSource}
                  setUntil={setUntil}
                  since={since}
                  source={source}
                  until={until}
                />
                {activeFilterCount ? (
                  <div className="mt-2 flex flex-wrap items-center gap-1">
                    {since ? <Chip tone="neutral">from {since}</Chip> : null}
                    {until ? <Chip tone="neutral">to {until}</Chip> : null}
                    {source.trim() ? (
                      <Chip tone="neutral">
                        <span className="max-w-full break-all font-[family-name:var(--font-mono)]">
                          {source.trim()}
                        </span>
                      </Chip>
                    ) : null}
                  </div>
                ) : null}
              </Card>

              {error ? (
                <div className="mt-4">
                  <Banner tone="fail" title="질의 실패">
                    {error}
                  </Banner>
                </div>
              ) : null}

              {loading ? (
                <Card className="mt-4 p-3">
                  <Skeleton className="h-3 w-24" />
                  <div className="mt-2.5 space-y-1.5">
                    <Skeleton className="h-3 w-full" />
                    <Skeleton className="h-3 w-5/6" />
                  </div>
                </Card>
              ) : result ? (
                <div className="mt-4 space-y-3">
                  {result.warnings.length ? (
                    <div className="space-y-2">
                      {result.warnings.map((warning) => (
                        <Banner key={warning} tone="warn" title="부분 결과">
                          {warning}
                        </Banner>
                      ))}
                    </div>
                  ) : null}
                  {/* Answer */}
                  <div className="flex items-center justify-between gap-2 px-1">
                    <ModeBadge>ANSWER</ModeBadge>
                    <span
                      className="truncate text-[var(--text-meta)]"
                      style={{ color: "var(--color-text-muted)" }}
                      title={result.question}
                    >
                      {result.question}
                    </span>
                  </div>

                  <Card className="p-4">
                    <p
                      className="whitespace-pre-wrap text-[var(--text-body)] leading-[1.55]"
                      style={{ color: "var(--color-text-strong)" }}
                    >
                      {result.answer}
                    </p>
                    <WikiCitationLinks answer={result} />
                  </Card>

                  {/* Sources */}
                  <section aria-label="근거 출처">
                    <div className="mb-1.5 flex items-baseline gap-2 px-1">
                      <h2
                        className="text-[var(--text-body)] font-bold"
                        style={{ color: "var(--color-text-strong)" }}
                      >
                        근거
                      </h2>
                      <span
                        className="text-[var(--text-meta)]"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        {result.sources.length}개 출처
                      </span>
                    </div>

                    {result.sources.length === 0 ? (
                      <Card
                        className="p-3 text-[var(--text-body-sm)]"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        관련 근거를 찾지 못했습니다.
                      </Card>
                    ) : (
                      <ol className="space-y-2">
                        {result.sources.map((s, i) => (
                          <li key={`${s.page_slug}-${i}`}>
                            <Card interactive className="p-3">
                              <div className="mb-1.5 flex items-center justify-between gap-2">
                                <span
                                  className="flex min-w-0 items-center gap-2 text-[var(--text-meta)]"
                                  style={{ color: "var(--color-text-muted)" }}
                                >
                                  <span
                                    className="flex h-4 w-4 shrink-0 items-center justify-center rounded-[3px] font-[family-name:var(--font-mono)] text-[10px]"
                                    style={{
                                      background: "var(--color-panel)",
                                      border: "1px solid var(--color-divider)",
                                      color: "var(--color-text-body)",
                                    }}
                                  >
                                    {i + 1}
                                  </span>
                                  <span
                                    className="truncate font-semibold"
                                    style={{ color: "var(--color-text-strong)" }}
                                  >
                                    {s.title}
                                  </span>
                                  <span className="shrink-0 font-[family-name:var(--font-mono)] tracking-[0.04em]">
                                    {s.kind}
                                  </span>
                                  <Chip tone={s.source_scope === "company" ? "pass" : "neutral"}>
                                    {scopeLabel(s.source_scope)}
                                  </Chip>
                                </span>
                                <ScoreBadge score={s.score} />
                              </div>
                              <p
                                className="whitespace-pre-wrap text-[var(--text-body-sm)] leading-[1.55]"
                                style={{ color: "var(--color-text-body)" }}
                              >
                                {s.excerpt}
                              </p>
                              {s.provenance.length > 0 ? (
                                <div className="mt-2 flex flex-wrap items-center gap-1">
                                  {s.provenance.map((p, pi) => (
                                    <Chip key={`${p}-${pi}`} tone="neutral">
                                      <span className="font-[family-name:var(--font-mono)]">
                                        {p}
                                      </span>
                                    </Chip>
                                  ))}
                                </div>
                              ) : null}
                            </Card>
                          </li>
                        ))}
                      </ol>
                    )}
                  </section>
                </div>
              ) : (
                <EmptyState />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function useCompactWikiAskControls() {
  const [compact, setCompact] = useState(true);

  useEffect(() => {
    if (typeof window === "undefined") return;
    function syncCompact() {
      setCompact(window.innerWidth < COMPACT_WIKI_ASK_WIDTH);
    }
    syncCompact();
    window.addEventListener("resize", syncCompact);
    return () => {
      window.removeEventListener("resize", syncCompact);
    };
  }, []);

  return compact;
}


/* P8.5 — Wiki scope selector mirrors the /ask page selector. */
function WikiScopeSelector({
  onChange,
  value,
}: {
  onChange: (next: AskScope) => void;
  value: AskScope;
}) {
  return (
    <div
      aria-label="위키 검색 스코프"
      className="inline-flex h-11 min-h-11 shrink-0 items-stretch overflow-hidden rounded-[var(--radius-toolbar)]"
      role="group"
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        boxShadow: "var(--shadow-toolbar)",
      }}
    >
      <WikiScopeOption
        active={value === "company"}
        label="회사"
        onClick={() => onChange("company")}
        title="회사 위키만 검색합니다."
      />
      <span
        aria-hidden
        className="my-1 w-px"
        style={{ background: "var(--color-toolbar-border)" }}
      />
      <WikiScopeOption
        active={value === "all"}
        label="회사 + 개인"
        onClick={() => onChange("all")}
        title="회사 위키와 내 개인 지식을 함께 검색합니다."
      />
    </div>
  );
}

function WikiScopeOption({
  active,
  label,
  onClick,
  title,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      aria-pressed={active}
      className="inline-flex min-h-11 shrink-0 items-center whitespace-nowrap px-3 text-[var(--text-body-sm)] transition-colors"
      onClick={onClick}
      style={{
        background: active ? "var(--color-sidebar-active)" : "transparent",
        color: active ? "var(--color-text-strong)" : "var(--color-text-body)",
        fontWeight: active ? 600 : 500,
      }}
      title={title}
      type="button"
    >
      {label}
    </button>
  );
}

function WikiAskFilterDisclosure({
  activeFilterCount,
  compact,
  filtersOpen,
  onOpenChange,
  onReset,
  setSince,
  setSource,
  setUntil,
  since,
  source,
  until,
}: {
  activeFilterCount: number;
  compact: boolean;
  filtersOpen: boolean;
  onOpenChange: (open: boolean) => void;
  onReset: () => void;
  setSince: (value: string) => void;
  setSource: (value: string) => void;
  setUntil: (value: string) => void;
  since: string;
  source: string;
  until: string;
}) {
  const controlHeight = compact ? "h-11" : "h-8";

  return (
    <details
      className="mt-3 border-t pt-3"
      onToggle={(event) => onOpenChange(event.currentTarget.open)}
      open={filtersOpen}
      style={{ borderColor: "var(--color-divider)" }}
    >
      <summary
        aria-expanded={filtersOpen}
        className={cx(
          "flex min-h-11 cursor-pointer list-none items-center justify-between gap-2 rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold [&::-webkit-details-marker]:hidden",
          !compact && "w-fit",
        )}
        style={{
          background: "var(--color-card)",
          border: "1px solid var(--color-toolbar-border)",
          boxShadow: "var(--shadow-toolbar)",
          color: "var(--color-text-strong)",
        }}
      >
        <span>{activeFilterCount ? `필터 ${activeFilterCount}` : "필터"}</span>
        <ChevronDown
          className={cx("transition-transform", filtersOpen && "rotate-180")}
          size={15}
          strokeWidth={1.8}
        />
      </summary>
      <div
        className="mt-2 grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(9rem,1.2fr)_auto]"
      >
        <label className="min-w-0">
          <span className="sr-only">시작일</span>
          <input
            type="date"
            value={since}
            onChange={(e) => setSince(e.target.value)}
            className={cx(inputClass, controlHeight, "min-w-0 py-1 text-[var(--text-body-sm)]")}
          />
        </label>
        <label className="min-w-0">
          <span className="sr-only">종료일</span>
          <input
            type="date"
            value={until}
            onChange={(e) => setUntil(e.target.value)}
            className={cx(inputClass, controlHeight, "min-w-0 py-1 text-[var(--text-body-sm)]")}
          />
        </label>
        <label className="min-w-0">
          <span className="sr-only">소스</span>
          <input
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="source: notion, github, gws_drive"
            className={cx(inputClass, controlHeight, "min-w-0 py-1 text-[var(--text-body-sm)]")}
          />
        </label>
        <ToolbarButton
          onClick={onReset}
          disabled={!activeFilterCount}
          className={cx("justify-center", compact && "h-11")}
        >
          초기화
        </ToolbarButton>
      </div>
    </details>
  );
}

function WikiHomeTabs({ activeTab }: { activeTab: WikiHomeTab }) {
  return (
    <nav aria-label="Wiki workspace views" className="mb-3 flex flex-wrap gap-2 px-1">
      <WikiHomeTabLink active={activeTab === "ask"} href="/wiki">
        Ask
      </WikiHomeTabLink>
      <WikiHomeTabLink active={activeTab === "tasks"} href="/wiki?tab=tasks">
        Tasks
      </WikiHomeTabLink>
    </nav>
  );
}

function WikiHomeTabLink({
  active,
  children,
  href,
}: {
  active: boolean;
  children: ReactNode;
  href: string;
}) {
  return (
    <Link
      aria-current={active ? "page" : undefined}
      className="inline-flex h-8 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold"
      href={href}
      style={{
        background: active ? "var(--color-sidebar-active)" : "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        boxShadow: "var(--shadow-toolbar)",
        color: "var(--color-text-strong)",
      }}
    >
      {children}
    </Link>
  );
}

function PendingKnowledgeWorkInbox({ state }: { state: InboxState }) {
  if (state.status === "loading") {
    return (
      <section aria-label="Pending knowledge work" className="mb-4">
        <div className="mb-2 flex items-center justify-between gap-2 px-1">
          <ModeBadge>PENDING WORK</ModeBadge>
          <Skeleton className="h-3 w-20" />
        </div>
        <div className="grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2">
          {[0, 1, 2, 3].map((item) => (
            <Card className="min-w-0 p-3" key={item}>
              <Skeleton className="h-4 w-28" />
              <Skeleton className="mt-3 h-10 w-full" />
            </Card>
          ))}
        </div>
      </section>
    );
  }
  if (state.status === "error") {
    return (
      <div className="mb-4">
        <Banner tone="warn" title="Agent Work inbox 숨김">
          {state.error}
        </Banner>
      </div>
    );
  }

  const { buckets } = state.summary;
  return (
    <section aria-label="Pending knowledge work" className="mb-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 px-1">
        <div className="flex items-center gap-2">
          <ModeBadge>PENDING WORK</ModeBadge>
          <span className="text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
            {formatDateTime(state.summary.generated_at)}
          </span>
        </div>
        <Chip tone="neutral">
          {state.summary.node_kind} · {state.summary.node_id}
        </Chip>
      </div>
      <div className="grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2">
        <InboxBucketCard
          count={buckets.wiki_tasks.count}
          rows={buckets.wiki_tasks.items.map((item) => ({
            href: item.href,
            key: item.slug,
            meta: `${item.kind} · ${formatDateTime(item.created_at)}`,
            title: item.title,
          }))}
          title="Wiki tasks"
        />
        <InboxBucketCard
          count={buckets.promote_staging.count}
          rows={buckets.promote_staging.items.map((item) => ({
            href: item.href,
            key: item.stage_id,
            meta: `${item.status}${item.created_at ? ` · ${formatDateTime(item.created_at)}` : ""}`,
            title: item.source_title,
          }))}
          title="Promote"
          unsupported={!buckets.promote_staging.supported}
          unsupportedText="회사 노드에서만 사용 가능"
        />
        <InboxBucketCard
          count={buckets.data_gaps.count}
          rows={buckets.data_gaps.items.map((item) => ({
            href: item.href,
            key: item.gap_id,
            meta: `hit ${item.hit_count} · ${formatDateTime(item.last_seen_at)}`,
            title: item.missing_topic,
          }))}
          title="Data gaps"
        />
        <InboxBucketCard
          count={buckets.agent_work.count}
          outcomeCounts={buckets.agent_work.by_outcome}
          rows={buckets.agent_work.items.map((item) => ({
            href: item.href,
            key: item.work_id,
            meta: `${item.policy_outcome ?? "pending"} · ${item.state}`,
            title: item.title,
          }))}
          title="Agent Work"
        />
      </div>
    </section>
  );
}

function InboxBucketCard({
  count,
  outcomeCounts,
  rows,
  title,
  unsupported = false,
  unsupportedText,
}: {
  count: number;
  outcomeCounts?: Record<string, number>;
  rows: Array<{ href: string; key: string; meta: string; title: string }>;
  title: string;
  unsupported?: boolean;
  unsupportedText?: string;
}) {
  const outcomeEntries = outcomeCounts
    ? Object.entries(outcomeCounts).filter(([, value]) => value > 0)
    : [];
  return (
    <Card className="min-w-0 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="font-bold" style={{ color: "var(--color-text-strong)" }}>
          {title}
        </div>
        <Chip tone={count ? "warn" : "muted"}>{count}</Chip>
      </div>
      {outcomeEntries.length ? (
        <div className="mb-2 flex flex-wrap gap-1">
          {outcomeEntries.map(([key, value]) => (
            <Chip key={key} tone="neutral">
              {key} {value}
            </Chip>
          ))}
        </div>
      ) : null}
      {unsupported ? (
        <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          {unsupportedText ?? "현재 node에서 지원하지 않습니다."}
        </p>
      ) : rows.length ? (
        <div className="space-y-1.5">
          {rows.map((row) => (
            <Link
              className="block min-w-0 overflow-hidden rounded-[6px] px-2 py-1.5 transition"
              href={row.href}
              key={row.key}
              style={{ background: "var(--color-sidebar-active)" }}
            >
              <div className="line-clamp-2 min-w-0 break-words text-[var(--text-body-sm)] font-semibold [overflow-wrap:anywhere]" style={{ color: "var(--color-text-strong)" }}>
                {row.title}
              </div>
              <div className="mt-0.5 line-clamp-2 min-w-0 break-words text-[var(--text-meta)] [overflow-wrap:anywhere]" style={{ color: "var(--color-text-muted)" }}>
                {row.meta}
              </div>
            </Link>
          ))}
        </div>
      ) : (
        <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
          처리할 항목 없음
        </p>
      )}
    </Card>
  );
}

function ToolbarChip({
  children,
  title,
}: {
  children: ReactNode;
  title: string;
}) {
  return (
    <span
      className="inline-flex h-8 items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)]"
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        boxShadow: "var(--shadow-toolbar)",
        color: "var(--color-text-strong)",
        fontWeight: 500,
      }}
      title={title}
    >
      {children}
    </span>
  );
}

function WikiCitationLinks({ answer }: { answer: WikiAnswer }) {
  if (!answer.wiki_links.length) return null;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t pt-3" style={{ borderColor: "var(--color-divider)" }}>
      {answer.wiki_links.map((link) => (
        <Link
          className="inline-flex min-h-8 max-w-full items-center gap-1.5 rounded-[var(--radius-toolbar)] px-2.5 text-[var(--text-body-sm)]"
          href={wikiPageHref(link.slug)}
          key={`${link.scope}-${link.slug}`}
          style={{
            background: "var(--color-panel)",
            border: "1px solid var(--color-divider)",
            color: "var(--color-text-strong)",
          }}
        >
          <span className="truncate">{link.title || link.slug}</span>
          <span className="shrink-0 font-[family-name:var(--font-mono)] text-[var(--text-meta)]">
            {link.scope === "company" ? "회사" : "개인"}
          </span>
        </Link>
      ))}
    </div>
  );
}

function wikiPageHref(slug: string): string {
  return `/wiki/${slug.split("/").map(encodeURIComponent).join("/")}`;
}

function ScoreBadge({ score }: { score: number }) {
  return (
    <span
      className="font-[family-name:var(--font-mono)] text-[var(--text-meta)] tracking-[0.04em]"
      style={{ color: "var(--color-text-muted)" }}
    >
      score {score.toFixed(3)}
    </span>
  );
}

function scopeLabel(scope: "company" | "personal") {
  return scope === "company" ? "회사" : "개인";
}

function formatDateTime(value: string) {
  return new Date(value).toLocaleString("ko-KR");
}

function EmptyState() {
  return (
    <div className="mt-10 flex flex-col items-center justify-center py-10 text-center">
      <div
        className="flex h-10 w-10 items-center justify-center rounded-[6px]"
        style={{
          background: "var(--color-card)",
          border: "1px solid var(--color-divider)",
          color: "var(--color-text-muted)",
        }}
      >
        <SearchIcon />
      </div>
      <p
        className="mt-2.5 text-[var(--text-body-sm)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        질문을 입력하면 근거와 함께 답변을 보여드립니다.
      </p>
    </div>
  );
}

function SearchIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.2-3.2" />
    </svg>
  );
}
