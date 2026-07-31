"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  type AgentWorkItem,
  type AgentWorkWikiLinkResponse,
  type DataGap,
  type DataGapPageResponse,
  type OwnerFootprint,
  type WikiPage,
  type WikiPageGraphResponse,
  type WikiTask,
  type WikiTaskPageResponse,
} from "@/lib/api";
import { readCachedResource, useCachedResource } from "@/lib/use-cached-resource";
import {
  Banner,
  Card,
  Chip,
  ModeBadge,
  Skeleton,
} from "@/components/ui";
import { ErrorBoundary } from "@/components/error-boundary";
import { useStoredPreference } from "@/lib/use-stored-preference";
import {
  buildGraphGroups,
  cappedGraphView,
  ENTITY_GLOSS,
  ENTITY_SCOPE_COPY,
  entityTeaserText,
  GRAPH_COPY,
  isOwnPersonalNode,
  PERSONAL_NODE_LABEL,
  wikiSlugPath,
} from "@/components/wiki/graph-shared";
import { GraphChipGroups } from "@/components/wiki/graph-chip-list";

// 그래프 뷰는 viewport 측정 기반이라 SSR을 끈다(하이드레이션 mismatch 방지).
// E2 — wiki 페이지 그래프 뷰는 인터랙티브 탐색기(더블클릭 누적 확장 + force 레이아웃 +
// pan/zoom)를 쓴다. 고정 2-링 GraphRingPanel은 /ask path-view에서 계속 사용.
const GraphExplorerPanel = dynamic(
  () => import("@/components/wiki/graph-explorer-panel"),
  { ssr: false },
);

// 그래프/칩 뷰 선호 — slug 무관 단일 기억(기본 graph). client-only.
const GRAPH_VIEW_STORAGE_KEY = "orthus:wikiGraphView";
const GRAPH_VIEW_MODES = ["graph", "list"] as const;

type LoadState =
  | { slug: string; status: "loading"; page: null; error: null }
  | { slug: string; status: "ready"; page: WikiPage; error: null }
  | { slug: string; status: "not_found"; page: null; error: null }
  | { slug: string; status: "error"; page: null; error: string };

type WorkLinkState =
  | { slug: string; status: "loading"; count: 0; items: []; error: null }
  | { slug: string; status: "ready"; count: number; items: AgentWorkItem[]; error: null }
  | { slug: string; status: "error"; count: 0; items: []; error: string };

type DataGapLinkState =
  | { slug: string; status: "loading"; count: 0; items: []; error: null }
  | { slug: string; status: "ready"; count: number; items: DataGap[]; error: null }
  | { slug: string; status: "error"; count: 0; items: []; error: string };

type TaskLinkState =
  | { slug: string; status: "loading"; count: 0; items: []; error: null }
  | { slug: string; status: "ready"; count: number; items: WikiTask[]; error: null }
  | { slug: string; status: "error"; count: 0; items: []; error: string };

// depth는 별도 useState(source of truth). 여기엔 settled 결과 + 그 결과가 어느 depth로
// 받아진 것인지(`depth`)를 담는다 — busy는 state.depth !== graphDepth로 파생한다(effect 내
// 동기 setState 금지: react-hooks/set-state-in-effect).
type GraphLinkState =
  | { slug: string; depth: 1 | 2; status: "loading"; data: null; error: null }
  | { slug: string; depth: 1 | 2; status: "ready"; data: WikiPageGraphResponse; error: null }
  | { slug: string; depth: 1 | 2; status: "not_company"; data: null; error: null }
  | { slug: string; depth: 1 | 2; status: "error"; data: null; error: string };

const TASK_KIND_LABEL: Record<WikiTask["kind"], string> = {
  open_question: "open question",
  conflict: "conflict",
  stale_audit: "stale audit",
  dedup: "dedup",
  provenance_fix: "provenance",
  entity_conflict: "entity conflict",
};

const COMPACT_WIKI_PAGE_WIDTH = 760;

/* --- K5 related-graph panel (read-only KG, kg-k5-fe-plan.md §2/§3) --- */

export default function WikiDetailPage() {
  const params = useParams<{ slug?: string | string[] }>();
  const searchParams = useSearchParams();
  const slug = useMemo(() => normalizeSlug(params.slug), [params.slug]);
  const requestedScope = normalizeWikiScope(searchParams.get("scope"));
  const compactShell = useCompactWikiPageShell();
  const [graphDepth, setGraphDepth] = useState<1 | 2>(1);

  // 페이지당 5개 fetch를 slug+scope key의 SWR 캐시로 돌린다 — 방문했던 페이지로
  // 돌아오면 캐시가 동기 페인트되고(스켈레톤 없음) 백그라운드에서 조용히 갱신된다.
  // 기존 LoadState/…LinkState 모양은 렌더 코드를 위해 hook 결과에서 재구성한다.
  const scopeKey = requestedScope ?? "auto";
  const pageRes = useCachedResource<WikiPage>(`wiki:page:${scopeKey}:${slug}`, () =>
    api.getWikiPage(slug, { scope: requestedScope }),
  );
  const taskRes = useCachedResource<WikiTaskPageResponse>(
    `wiki:tasks:${scopeKey}:${slug}`,
    () => api.getWikiPageWikiTasks(slug, { scope: requestedScope }),
  );
  const gapRes = useCachedResource<DataGapPageResponse>(
    `wiki:data-gaps:${scopeKey}:${slug}`,
    () => api.getWikiPageDataGaps(slug, { scope: requestedScope }),
  );
  const workRes = useCachedResource<AgentWorkWikiLinkResponse>(
    `wiki:agent-work:${scopeKey}:${slug}`,
    () => api.getWikiPageAgentWork(slug, { scope: requestedScope }),
  );
  const graphRes = useCachedResource<WikiPageGraphResponse>(
    `wiki:graph:${slug}:d${graphDepth}`,
    () => api.getWikiPageGraph(slug, graphDepth),
  );

  const state: LoadState = pageRes.data
    ? { slug, status: "ready", page: pageRes.data, error: null }
    : pageRes.error
      ? pageRes.error instanceof ApiError && pageRes.error.status === 404
        ? { slug, status: "not_found", page: null, error: null }
        : {
            slug,
            status: "error",
            page: null,
            error:
              pageRes.error instanceof Error
                ? pageRes.error.message
                : "위키 페이지를 불러오지 못했습니다.",
          }
      : { slug, status: "loading", page: null, error: null };
  const workState: WorkLinkState = workRes.data
    ? { slug, status: "ready", count: workRes.data.count, items: workRes.data.items, error: null }
    : workRes.error
      ? {
          slug,
          status: "error",
          count: 0,
          items: [],
          error:
            workRes.error instanceof Error
              ? workRes.error.message
              : "관련 Agent Work를 불러오지 못했습니다.",
        }
      : { slug, status: "loading", count: 0, items: [], error: null };
  const gapState: DataGapLinkState = gapRes.data
    ? { slug, status: "ready", count: gapRes.data.count, items: gapRes.data.items, error: null }
    : gapRes.error
      ? {
          slug,
          status: "error",
          count: 0,
          items: [],
          error:
            gapRes.error instanceof Error
              ? gapRes.error.message
              : "관련 데이터 갭을 불러오지 못했습니다.",
        }
      : { slug, status: "loading", count: 0, items: [], error: null };
  const taskState: TaskLinkState = taskRes.data
    ? { slug, status: "ready", count: taskRes.data.count, items: taskRes.data.items, error: null }
    : taskRes.error
      ? {
          slug,
          status: "error",
          count: 0,
          items: [],
          error:
            taskRes.error instanceof Error
              ? taskRes.error.message
              : "관련 WikiTask를 불러오지 못했습니다.",
        }
      : { slug, status: "loading", count: 0, items: [], error: null };

  // depth 토글 중에는 반대 depth의 캐시(방금 보던 결과)를 유지해 busy(dim) 파생이
  // 살아 있게 한다 — 기존 state machine의 "이전 ready 칩 유지 + 갱신 중" 동작 보존.
  // slug가 바뀌면 key도 바뀌므로 이전 slug 결과가 새 slug에 비치지 않는다.
  const graphSettled: GraphLinkState | null = graphRes.data
    ? { slug, depth: graphDepth, status: "ready", data: graphRes.data, error: null }
    : graphRes.error
      ? graphRes.error instanceof ApiError && graphRes.error.status === 404
        ? { slug, depth: graphDepth, status: "not_company", data: null, error: null }
        : {
            slug,
            depth: graphDepth,
            status: "error",
            data: null,
            error: graphRes.error instanceof Error ? graphRes.error.message : GRAPH_COPY.error,
          }
      : null;
  const otherDepth: 1 | 2 = graphDepth === 1 ? 2 : 1;
  const prevDepthGraph = readCachedResource<WikiPageGraphResponse>(
    `wiki:graph:${slug}:d${otherDepth}`,
  );
  const graphState: GraphLinkState =
    graphSettled ??
    (prevDepthGraph
      ? { slug, depth: otherDepth, status: "ready", data: prevDepthGraph, error: null }
      : { slug, depth: graphDepth, status: "loading", data: null, error: null });

  const loading = state.status === "loading";
  const page = state.status === "ready" ? state.page : null;
  const askHref = `/ask?q=&context_wiki_slug=${encodeURIComponent(slug)}`;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <WikiPageHeader
        title={page?.title ?? slug}
        subtitle="컴파일된 위키 페이지를 읽고 같은 컨텍스트로 Assistant에 질문합니다."
        askHref={askHref}
        compact={compactShell}
      />

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[900px] px-3 py-3 sm:px-5 sm:py-5">
          {loading ? (
            <Card className="p-4">
              <Skeleton className="h-4 w-40" />
              <Skeleton className="mt-3 h-24 w-full" />
              <Skeleton className="mt-2 h-16 w-5/6" />
            </Card>
          ) : state.status === "not_found" ? (
            <Banner tone="fail" title="위키 페이지 없음">
              <span className="break-all">현재 node에서 볼 수 있는 위키 페이지가 아닙니다: {slug}</span>
            </Banner>
          ) : state.status === "error" ? (
            <Banner tone="fail" title="불러오기 실패">
              <span className="break-all">{state.error}</span>
            </Banner>
          ) : page ? (
            <div className="space-y-4">
              <div className="flex min-w-0 flex-wrap items-center gap-2 px-1">
                <ModeBadge>WIKI PAGE</ModeBadge>
                <BreakableChip mono>{page.slug}</BreakableChip>
              </div>

              <RelatedAgentWorkPanel compact={compactShell} slug={slug} state={workState} />
              <RelatedWikiTasksPanel compact={compactShell} slug={slug} state={taskState} />
              <RelatedDataGapsPanel compact={compactShell} slug={slug} state={gapState} />
              <RelatedGraphPanel
                compact={compactShell}
                depth={graphDepth}
                onDepthChange={setGraphDepth}
                slug={slug}
                state={graphState}
              />

              <section aria-label="정의">
                <h2 className="mb-1.5 px-1 text-[var(--text-body)] font-bold" style={{ color: "var(--color-text-strong)" }}>
                  정의
                </h2>
                <Card className="p-4">
                  <p className="whitespace-pre-wrap text-[var(--text-body)] leading-[1.6]" style={{ color: "var(--color-text-strong)" }}>
                    {page.definition || "정의가 아직 없습니다."}
                  </p>
                </Card>
              </section>

              <section aria-label="개요">
                <h2 className="mb-1.5 px-1 text-[var(--text-body)] font-bold" style={{ color: "var(--color-text-strong)" }}>
                  개요
                </h2>
                <Card className="p-4">
                  <p className="whitespace-pre-wrap text-[var(--text-body)] leading-[1.6]" style={{ color: "var(--color-text-body)" }}>
                    {page.overview || "개요가 아직 없습니다."}
                  </p>
                </Card>
              </section>

              <WikiList title="관계" items={page.relations} />
              <WikiList title="근거" items={page.evidence} mono />
              <WikiList title="소스" items={page.sources} mono />
              <WikiList title="백링크" items={page.backlinks} mono />
              <WikiList title="경쟁 해석" items={page.competing} />
              <WikiList title="열린 질문" items={page.open_questions} />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function useCompactWikiPageShell() {
  const [compact, setCompact] = useState(false);

  useEffect(() => {
    const sync = () => {
      setCompact(window.innerWidth < COMPACT_WIKI_PAGE_WIDTH);
    };
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  return compact;
}

function WikiPageHeader({
  askHref,
  compact,
  subtitle,
  title,
}: {
  askHref: string;
  compact: boolean;
  subtitle: ReactNode;
  title: ReactNode;
}) {
  return (
    <header
      className={`orthus-page-header flex flex-wrap items-center justify-between gap-3 px-5 py-3 ${compact ? "items-start" : ""}`}
      style={{
        background: "var(--color-app-canvas)",
        borderBottom: "1px solid var(--color-divider)",
      }}
    >
      <div className={`min-w-0 flex-1 ${compact ? "basis-full" : ""}`}>
        <h1
          className="orthus-page-title break-words text-[var(--text-day-heading)] font-extrabold [overflow-wrap:anywhere]"
          style={{ color: "var(--color-text-strong)", lineHeight: 1.1 }}
        >
          {title}
        </h1>
        <p
          className="orthus-page-subtitle mt-0.5 break-words text-[var(--text-meta)] [overflow-wrap:anywhere]"
          style={{ color: "var(--color-text-muted)" }}
        >
          {subtitle}
        </p>
      </div>
      <div className={`orthus-page-actions flex min-w-0 items-center gap-2 ${compact ? "w-full" : "shrink-0"}`}>
        <ToolbarLink href={askHref} className={compact ? "w-full justify-center" : undefined}>
          이 페이지에 대해 묻기
        </ToolbarLink>
      </div>
    </header>
  );
}

function RelatedWikiTasksPanel({
  compact,
  slug,
  state,
}: {
  compact: boolean;
  slug: string;
  state: TaskLinkState;
}) {
  const stale = state.slug !== slug;
  return (
    <section aria-label="관련 WikiTask">
      <div className={`mb-1.5 flex flex-wrap items-center justify-between gap-2 px-1 ${compact ? "flex-col items-stretch" : ""}`}>
        <h2 className="min-w-0 break-words text-[var(--text-body)] font-bold [overflow-wrap:anywhere]" style={{ color: "var(--color-text-strong)" }}>
          관련 WikiTask
        </h2>
        <ToolbarLink href="/wiki/tasks" className={compact ? "w-full justify-center" : undefined}>
          WikiTask 열기{state.status === "ready" && !stale ? ` (${state.count})` : ""}
        </ToolbarLink>
      </div>
      <Card className="p-4">
        {state.status === "loading" || stale ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-44" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : state.status === "error" ? (
          <p className="text-[var(--text-body-sm)] leading-[1.6]" style={{ color: "var(--color-text-muted)" }}>
            현재 권한 또는 node 범위에서 관련 WikiTask를 표시할 수 없습니다.
          </p>
        ) : state.items.length ? (
          <div className="space-y-2">
            {state.items.slice(0, 5).map((task) => (
              <Link
                className="block min-w-0 rounded-[6px] px-3 py-2 transition"
                href="/wiki/tasks"
                key={task.slug}
                style={{
                  background: "var(--color-sidebar-active)",
                  color: "var(--color-text-body)",
                }}
              >
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <Chip tone="warn">open</Chip>
                  <Chip tone="neutral">{TASK_KIND_LABEL[task.kind]}</Chip>
                  <Chip tone="muted">{formatDateTime(task.created_at)}</Chip>
                </div>
                <div className="mt-1 line-clamp-2 break-words text-[var(--text-body-sm)] font-semibold [overflow-wrap:anywhere]" style={{ color: "var(--color-text-strong)" }}>
                  {task.description || task.slug}
                </div>
                <div className="mt-1 line-clamp-2 break-all text-[var(--text-meta)] font-[family-name:var(--font-mono)]" style={{ color: "var(--color-text-muted)" }}>
                  {task.slug}
                </div>
              </Link>
            ))}
          </div>
        ) : (
          <p className="break-words text-[var(--text-body-sm)] leading-[1.6] [overflow-wrap:anywhere]" style={{ color: "var(--color-text-muted)" }}>
            이 node의 local wiki page로 resolve되는 open WikiTask가 없습니다. personal에서
            보는 federated central page는 cross-node WikiTask 집계를 지원하지 않습니다.
          </p>
        )}
      </Card>
    </section>
  );
}

function RelatedDataGapsPanel({
  compact,
  slug,
  state,
}: {
  compact: boolean;
  slug: string;
  state: DataGapLinkState;
}) {
  const stale = state.slug !== slug;
  return (
    <section aria-label="관련 데이터 갭">
      <div className={`mb-1.5 flex flex-wrap items-center justify-between gap-2 px-1 ${compact ? "flex-col items-stretch" : ""}`}>
        <h2 className="min-w-0 break-words text-[var(--text-body)] font-bold [overflow-wrap:anywhere]" style={{ color: "var(--color-text-strong)" }}>
          관련 데이터 갭
        </h2>
        <ToolbarLink href="/agent-work" className={compact ? "w-full justify-center" : undefined}>
          데이터 갭 열기{state.status === "ready" && !stale ? ` (${state.count})` : ""}
        </ToolbarLink>
      </div>
      <Card className="p-4">
        {state.status === "loading" || stale ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-44" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : state.status === "error" ? (
          <p className="text-[var(--text-body-sm)] leading-[1.6]" style={{ color: "var(--color-text-muted)" }}>
            현재 권한 또는 node 범위에서 관련 데이터 갭을 표시할 수 없습니다.
          </p>
        ) : state.items.length ? (
          <div className="space-y-2">
            {state.items.slice(0, 5).map((gap) => (
              <Link
                className="block min-w-0 rounded-[6px] px-3 py-2 transition"
                href="/agent-work"
                key={gap.gap_id}
                style={{
                  background: "var(--color-sidebar-active)",
                  color: "var(--color-text-body)",
                }}
              >
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <Chip tone="warn">{gap.reason}</Chip>
                  <Chip tone="muted">hit {gap.hit_count}</Chip>
                  <Chip tone="muted">{formatDateTime(gap.last_seen_at)}</Chip>
                </div>
                <div className="mt-1 line-clamp-2 break-words text-[var(--text-body-sm)] font-semibold [overflow-wrap:anywhere]" style={{ color: "var(--color-text-strong)" }}>
                  {gap.question}
                </div>
              </Link>
            ))}
          </div>
        ) : (
          <p className="break-words text-[var(--text-body-sm)] leading-[1.6] [overflow-wrap:anywhere]" style={{ color: "var(--color-text-muted)" }}>
            이 페이지에서 발생한 미해결 데이터 갭이 없습니다.
          </p>
        )}
      </Card>
    </section>
  );
}

function RelatedAgentWorkPanel({
  compact,
  slug,
  state,
}: {
  compact: boolean;
  slug: string;
  state: WorkLinkState;
}) {
  const agentWorkHref = `/agent-work?wiki_slug=${encodeURIComponent(slug)}`;
  const stale = state.slug !== slug;
  return (
    <section aria-label="관련 Agent Work">
      <div className={`mb-1.5 flex flex-wrap items-center justify-between gap-2 px-1 ${compact ? "flex-col items-stretch" : ""}`}>
        <h2 className="min-w-0 break-words text-[var(--text-body)] font-bold [overflow-wrap:anywhere]" style={{ color: "var(--color-text-strong)" }}>
          관련 Agent Work
        </h2>
        <ToolbarLink href={agentWorkHref} className={compact ? "w-full justify-center" : undefined}>
          Agent Work 열기{state.status === "ready" && !stale ? ` (${state.count})` : ""}
        </ToolbarLink>
      </div>
      <Card className="p-4">
        {state.status === "loading" || stale ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-44" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : state.status === "error" ? (
          <p className="text-[var(--text-body-sm)] leading-[1.6]" style={{ color: "var(--color-text-muted)" }}>
            현재 권한 또는 node 범위에서 관련 Agent Work를 표시할 수 없습니다.
          </p>
        ) : state.items.length ? (
          <div className="space-y-2">
            {state.items.slice(0, 5).map((item) => (
              <Link
                className="block min-w-0 rounded-[6px] px-3 py-2 transition"
                href={`/agent-work?wiki_slug=${encodeURIComponent(slug)}`}
                key={item.work_id}
                style={{
                  background: "var(--color-sidebar-active)",
                  color: "var(--color-text-body)",
                }}
              >
                <div className="flex min-w-0 flex-wrap items-center gap-1.5">
                  <Chip tone={item.policy_outcome === "auto_execute" ? "pass" : "neutral"}>
                    {item.policy_outcome ?? "pending"}
                  </Chip>
                  <Chip tone="muted">{item.state}</Chip>
                  <Chip tone="muted">{item.source_kind}</Chip>
                </div>
                <div className="mt-1 line-clamp-2 break-words text-[var(--text-body-sm)] font-semibold [overflow-wrap:anywhere]" style={{ color: "var(--color-text-strong)" }}>
                  {item.title}
                </div>
              </Link>
            ))}
          </div>
        ) : (
          <p className="break-words text-[var(--text-body-sm)] leading-[1.6] [overflow-wrap:anywhere]" style={{ color: "var(--color-text-muted)" }}>
            이 node의 local wiki page로 resolve되는 Agent Work가 없습니다. personal에서 보는
            federated central page는 cross-node Agent Work 집계를 지원하지 않습니다.
          </p>
        )}
      </Card>
    </section>
  );
}

// K7.3b — "내 개인 메모" 경계 섹션 (Option A: ring 위 크롬). owner-scope ON일 때만 렌더
// (flag-off 안전). 노출은 footprint 비례 적응형(decision #7): 연결된 개인 노트가 없으면
// 상시 빈 라벨 박스 대신 최소 한 줄 + first-run(state 4). personal↔company "연결"은 아래
// ring에서 엣지로 보인다(노드 시각 구분은 ring의 accent halo가 담당).
function PersonalNotesSection({
  active,
  footprint,
  hasOwnPersonalInGraph,
  slug,
}: {
  active: boolean;
  footprint: OwnerFootprint | null;
  hasOwnPersonalInGraph: boolean;
  slug: string;
}) {
  // owner-scope off / 비-owner(active=false) → 섹션 없음. footprint 로딩 전(null)도 미표시.
  if (!active || footprint === null) return null;
  const { node_count: nodeCount, edge_count: edgeCount } = footprint;
  const askHref = `/ask?q=&context_wiki_slug=${encodeURIComponent(slug)}`;

  // K7.5 (O1) — status!=ready면 count=0을 "메모 없음"으로 렌더하지 않는다(false-zero 차단).
  // 측정 보류(rebuild 중) / 측정 불가(KG 미가용)는 '확인 보류 중'으로 정직하게 표시한다.
  if (footprint.status !== "ready") {
    return (
      // role=status(=aria-live polite, atomic) — SR가 transient '확인 보류 중' 전환을 읽는다.
      // aria-busy는 일부러 안 건다: live-region 메시지 자체에 busy=true면 announce가 보류되다
      // ready 전환 시 언마운트돼 영영 안 읽힐 수 있다(메시지 텍스트가 busy 상태를 전달). (K7.5 a11y)
      <p
        className="mb-2 px-1 text-[var(--text-body-sm)] leading-[1.6] [overflow-wrap:anywhere]"
        role="status"
        style={{ color: "var(--color-text-muted)" }}
      >
        개인 메모 확인 보류 중{" "}
        {footprint.status === "rebuilding" ? "(그래프 다시 읽는 중)" : "(잠시 후 다시 시도)"}
      </p>
    );
  }

  // state 4 (no-personal-notes-anywhere) — 빈 라벨 박스 금지, §6 done-def 리터럴 gloss + first-run.
  // first-run 링크는 별도 inline-flex min-h-11로 분리해 P5 44px 탭타깃을 보장한다(인라인 한 줄이면
  // 줄바꿈 시 17px까지 줄어듦 — kg-k7.3-plan §6 44px controls).
  if (nodeCount === 0) {
    return (
      <div className="mb-2 px-1">
        <p
          className="text-[var(--text-body-sm)] leading-[1.6] [overflow-wrap:anywhere]"
          style={{ color: "var(--color-text-muted)" }}
        >
          개인 메모는 나만 볼 수 있으며 팀원에게는 절대 보이지 않습니다.
        </p>
        <Link
          className="mt-1 inline-flex min-h-11 items-center rounded-[6px] font-semibold underline outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
          href={askHref}
          style={{ color: "var(--color-accent)" }}
        >
          이 페이지에 대한 내 메모 시작하기
        </Link>
      </div>
    );
  }

  // state 1 (populated) / state 3 (has-notes-but-unlinked) — bounded 섹션.
  return (
    <div
      className="mb-2 rounded-[var(--radius-card)] p-3"
      style={{
        border: "1px solid var(--color-accent)",
        background: "var(--color-card)",
      }}
    >
      <div className="mb-1 flex flex-wrap items-center gap-2">
        <h3 className="font-bold" style={{ color: "var(--color-text-strong)" }}>
          {PERSONAL_NODE_LABEL}
        </h3>
        <Chip tone="accent">나만 볼 수 있음</Chip>
      </div>
      {/* New-hire용 무맥락 gloss — "personal" = 비공개(초안/비공식 아님). §6 done-def 리터럴. */}
      <p
        className="text-[var(--text-meta)] leading-[1.5]"
        style={{ color: "var(--color-text-muted)" }}
      >
        나만 볼 수 있는 메모입니다. 팀원이나 관리자에게는 절대 보이지 않습니다.
      </p>
      <p
        className="mt-1 text-[var(--text-meta)] leading-[1.5]"
        style={{ color: "var(--color-text-muted)" }}
      >
        내 개인 메모 {nodeCount}개,{" "}
        <span title="메모끼리 이어진 관계 수">연결 {edgeCount}개</span> · 나만 볼 수
        있습니다
      </p>
      {hasOwnPersonalInGraph ? (
        <p
          className="mt-1.5 text-[var(--text-body-sm)] leading-[1.6]"
          style={{ color: "var(--color-text-body)" }}
        >
          이 페이지에 연결된 내 개인 메모가 아래 그래프에 표시됩니다.
        </p>
      ) : (
        // state 3 — D2 honesty(엔티티 연결 미구현). 빈 섹션이 버그로 읽히지 않게 + 수요 신호.
        // 연결 링크는 별도 inline-flex min-h-11로 분리 — P5 44px 탭타깃(인라인이면 390에서 17px).
        <>
          <p
            className="mt-1.5 text-[var(--text-body-sm)] leading-[1.6] [overflow-wrap:anywhere]"
            style={{ color: "var(--color-text-body)" }}
          >
            이 주제를 언급한 개인 메모가 있지만 아직 이 페이지에 연결되지 않았습니다. 개인 메모의
            엔티티 기반 연결은 준비 중입니다.
          </p>
          <Link
            className="mt-1 inline-flex min-h-11 items-center rounded-[6px] font-semibold underline outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
            href={askHref}
            style={{ color: "var(--color-accent)" }}
          >
            내 메모를 이 페이지에 연결
          </Link>
        </>
      )}
      {/* K7.3 name-but-not-build 승격 앵커 — 기존 /promote export 라우트로 deep-link(새 write
          경로 0). note-별 promote 핸들러는 미구현이라 muted + "준비 중"(owner-initiated only). */}
      <Link
        className="mt-2 inline-flex min-h-11 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
        href="/promote"
        style={{ border: "1px solid var(--color-divider)", color: "var(--color-text-muted)" }}
      >
        회사로 승격 (준비 중)
      </Link>
    </div>
  );
}

function RelatedGraphPanel({
  compact,
  depth,
  onDepthChange,
  slug,
  state,
}: {
  compact: boolean;
  depth: 1 | 2;
  onDepthChange: (depth: 1 | 2) => void;
  slug: string;
  state: GraphLinkState;
}) {
  const router = useRouter();
  const [viewMode, setViewMode] = useStoredPreference(
    GRAPH_VIEW_STORAGE_KEY,
    GRAPH_VIEW_MODES,
    "graph",
  );
  const stale = state.slug !== slug;
  const ready = !stale && state.status === "ready" ? state.data : null;
  const supported = ready?.supported === true;
  // 표시 중 데이터가 현재 요청 depth와 다르면 refetch 진행 중(이전 칩/그래프 유지 + dim).
  const busy = !stale && state.status === "ready" && state.depth !== depth;

  // K9.2 (C-C/U2) — entity 다리 펼침은 slug-derived(reset effect 없이 navigation에 자동 접힘):
  // expandedFor===slug면 펼친 상태. 펼치면 include=entity로 별도 fetch해 entity 노드/엣지를 합류.
  // fail-open: 펼침 실패는 티저 유지(기본 패널 무변). 티저 자체는 기본 응답(ready.entity_teaser)에서.
  const [expandedFor, setExpandedFor] = useState<string | null>(null);
  const entityExpanded = expandedFor === slug;
  const teaser = ready?.entity_teaser ?? null;
  // 펼침 fetch도 slug+depth key SWR 캐시 — 접었다 다시 펼치면 즉시 페인트.
  // fail-open — 펼침 실패(error)는 티저 유지(관측 누락일 뿐, 기본 패널은 ready로 렌더).
  const entityRes = useCachedResource<WikiPageGraphResponse>(
    entityExpanded ? `wiki:graph-entity:${slug}:d${depth}` : null,
    () => api.getWikiPageGraph(slug, depth, { includeEntity: true }),
  );
  // 펼친 동안 현재 slug/depth의 entity-합류 데이터가 도착했으면 그것을 그래프 입력으로 쓴다
  // (도착 전엔 ready로 — 잠깐 base 그래프 후 entity 노드 합류).
  const entityReady = entityExpanded ? entityRes.data : null;
  const effectiveReady = entityReady ?? ready;

  // 렌더당 1회로 built+capped를 함께 계산 — supported/ready 가드를 한 곳에 모으고
  // capped는 built의 groups/currentNodeId를 재사용한다(재BFS 없음). 빈 그래프면 capped=null.
  const graph = useMemo(() => {
    if (!effectiveReady || effectiveReady.supported !== true) return null;
    const built = buildGraphGroups(effectiveReady);
    if (built.groups.length === 0) return { built, capped: null };
    return { built, capped: cappedGraphView(effectiveReady, built) };
  }, [effectiveReady]);
  const hasGraph = graph != null && graph.built.groups.length > 0;
  const showGraph =
    viewMode === "graph" && !!graph?.capped && graph.capped.nodes.length > 0;
  const dimStyle = useMemo(
    () => (busy ? { opacity: 0.6, transition: "opacity 120ms" } : undefined),
    [busy],
  );
  // onNodeClick prop identity 안정화 — RelatedGraphPanel 재렌더 때 GraphRingPanel 불필요 reconcile 방지.
  const navTo = useCallback(
    (s: string) => router.push(`/wiki/${wikiSlugPath(s)}`),
    [router],
  );
  const resetKey = useMemo(() => `${slug}:${depth}`, [slug, depth]);

  // K7.3b — owner-scope ON 신호(서버 파생). flag-off/비-owner는 false → personal 섹션 미표시.
  const ownerScopeActive = ready?.personal_entity_search_deferred === true;
  // 이 페이지 그래프에 caller 본인 personal 노드가 있나(state 1 판정). 두 플래그 AND는
  // isOwnPersonalNode가 보장. 없으면 footprint>0일 때 state 3(미연결)로 분기한다.
  const hasOwnPersonalInGraph = useMemo(
    () => (ready?.nodes ?? []).some(isOwnPersonalNode),
    [ready],
  );
  // footprint는 페이지 무관 per-user 집계 — owner-scope ON일 때만 가져온다(flag-off면 호출 0,
  // key=null). 실패는 fail-open(data null → 섹션 미표시). owner_id 미수신(카운트만).
  // in-memory 세션 캐시라 T4(세션 간 공유/CDN 캐시 금지)와 무관하고 로그아웃 시 비워진다.
  const footprint =
    useCachedResource<OwnerFootprint>(
      ownerScopeActive ? "wiki:kg-footprint" : null,
      () => api.getOwnerKgFootprint(),
    ).data ?? null;

  return (
    <section aria-busy={busy} aria-label="관련 지식 그래프">
      <PersonalNotesSection
        active={ownerScopeActive}
        footprint={footprint}
        hasOwnPersonalInGraph={hasOwnPersonalInGraph}
        slug={slug}
      />
      <div
        className={`mb-1.5 flex flex-wrap items-center justify-between gap-2 px-1 ${compact ? "flex-col items-stretch" : ""}`}
      >
        <h2
          className="min-w-0 break-words text-[var(--text-body)] font-bold [overflow-wrap:anywhere]"
          style={{ color: "var(--color-text-strong)" }}
        >
          관련 지식 그래프
        </h2>
        {supported ? (
          <div
            className={`flex flex-wrap items-center gap-2 ${compact ? "w-full" : ""}`}
          >
            {hasGraph ? (
              <div
                aria-label="보기 방식"
                className={`inline-flex overflow-hidden rounded-[var(--radius-toolbar)] ${compact ? "w-full" : ""}`}
                role="group"
                style={{
                  border: "1px solid var(--color-toolbar-border)",
                  boxShadow: "var(--shadow-toolbar)",
                }}
              >
                {(["graph", "list"] as const).map((mode) => {
                  const active = viewMode === mode;
                  return (
                    <button
                      aria-pressed={active}
                      className={`min-h-11 px-3 text-[var(--text-body-sm)] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--color-text-strong)] ${compact ? "flex-1" : ""}`}
                      key={mode}
                      onClick={() => setViewMode(mode)}
                      style={{
                        background: active
                          ? "var(--color-sidebar-active)"
                          : "var(--color-card)",
                        color: "var(--color-text-strong)",
                      }}
                      type="button"
                    >
                      {mode === "graph" ? "그래프" : "칩"}
                    </button>
                  );
                })}
              </div>
            ) : null}
            <button
              aria-pressed={depth === 2}
              className={`inline-flex min-h-11 items-center justify-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)] disabled:cursor-not-allowed disabled:opacity-50 ${compact ? "w-full" : ""}`}
              disabled={busy}
              onClick={() => onDepthChange(depth === 1 ? 2 : 1)}
              style={{
                background: "var(--color-card)",
                border: "1px solid var(--color-toolbar-border)",
                boxShadow: "var(--shadow-toolbar)",
                color: "var(--color-text-strong)",
              }}
              type="button"
            >
              {depth === 1 ? "2 hop 보기" : "1 hop 보기"}
              {busy ? " · 갱신 중" : ""}
            </button>
          </div>
        ) : null}
      </div>
      <Card className="p-4">
        {stale || state.status === "loading" ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-44" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : state.status === "not_company" ? (
          <p
            className="break-words text-[var(--text-body-sm)] leading-[1.6] [overflow-wrap:anywhere]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {GRAPH_COPY.notCompany}
          </p>
        ) : state.status === "error" ? (
          <p
            className="text-[var(--text-body-sm)] leading-[1.6]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {GRAPH_COPY.error}
          </p>
        ) : !supported ? (
          <p
            className="break-words text-[var(--text-body-sm)] leading-[1.6] [overflow-wrap:anywhere]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {ready?.reason === "kg_disabled"
              ? GRAPH_COPY.disabled
              : GRAPH_COPY.unavailable}
          </p>
        ) : graph && graph.built.groups.length ? (
          showGraph && graph.capped ? (
            // 그래프 렌더 실패는 fail-open: 칩 리스트로 자동 강등(top concern).
            <ErrorBoundary
              fallback={<GraphChipGroups built={graph.built} busy={busy} />}
              resetKey={resetKey}
              onError={(err) =>
                console.error("graph render failed; showing chip fallback", err)
              }
            >
              <div style={dimStyle}>
                <GraphExplorerPanel
                  // slug 변경 시에만 remount(누적 리셋). depth 토글/entity 합류는 시드
                  // 병합으로 누적에 더해진다 — E2 탐색기 계약.
                  key={slug}
                  anchorNodeId={graph.capped.currentNodeId}
                  compact={compact}
                  onNodeNavigate={navTo}
                  seedEdges={graph.capped.edges}
                  seedNodes={graph.capped.nodes}
                />
                {graph.built.truncated ? (
                  <p
                    className="mt-2 text-[var(--text-meta)]"
                    style={{ color: "var(--color-text-muted)" }}
                  >
                    {GRAPH_COPY.truncated}
                  </p>
                ) : null}
              </div>
            </ErrorBoundary>
          ) : (
            <GraphChipGroups built={graph.built} busy={busy} />
          )
        ) : (
          <p
            className="break-words text-[var(--text-body-sm)] leading-[1.6] [overflow-wrap:anywhere]"
            style={{ color: "var(--color-text-muted)" }}
          >
            {GRAPH_COPY.empty}
          </p>
        )}
        {/* K9.2 (C-C/U2/D-TZ B) — entity 다리 티저→펼침. presence-gated(서버가 다리 있을 때만
            entity_teaser). 펼치면 위 ring에 entity 노드/엣지가 합류하고, 여기엔 single-scope
            카피(C-F) + gloss(U4) + 접기를 둔다. 다리 없으면 미렌더(calm 빈 상태, U3). */}
        {supported && (teaser || entityExpanded) ? (
          <div
            className="mt-3 border-t pt-3"
            style={{ borderColor: "var(--color-toolbar-border)" }}
          >
            {entityExpanded ? (
              <div className="space-y-1.5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p
                    className="text-[var(--text-body-sm)] font-semibold"
                    style={{ color: "var(--color-text-strong)" }}
                  >
                    {ENTITY_SCOPE_COPY}
                  </p>
                  <button
                    className="min-h-11 rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
                    onClick={() => setExpandedFor(null)}
                    style={{
                      background: "var(--color-card)",
                      border: "1px solid var(--color-toolbar-border)",
                      boxShadow: "var(--shadow-toolbar)",
                      color: "var(--color-text-strong)",
                    }}
                    type="button"
                  >
                    {GRAPH_COPY.entityCollapse}
                  </button>
                </div>
                <p
                  className="text-[var(--text-meta)] leading-[1.6]"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  {ENTITY_GLOSS}
                </p>
              </div>
            ) : teaser ? (
              <button
                className="flex min-h-11 w-full flex-wrap items-center justify-between gap-2 rounded-[var(--radius-toolbar)] px-3 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
                onClick={() => setExpandedFor(slug)}
                style={{
                  background: "var(--color-card)",
                  border: "1px solid var(--color-toolbar-border)",
                  boxShadow: "var(--shadow-toolbar)",
                }}
                type="button"
              >
                <span
                  className="min-w-0 break-words text-[var(--text-body-sm)] [overflow-wrap:anywhere]"
                  style={{ color: "var(--color-text-strong)" }}
                >
                  {entityTeaserText(teaser)}
                </span>
                <span
                  className="shrink-0 text-[var(--text-meta)] font-semibold"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  {GRAPH_COPY.entityExpand} →
                </span>
              </button>
            ) : null}
          </div>
        ) : null}
      </Card>
    </section>
  );
}

function formatDateTime(value: string) {
  return new Date(value).toLocaleString("ko-KR");
}

function WikiList({
  items,
  mono = false,
  title,
}: {
  items: string[];
  mono?: boolean;
  title: string;
}) {
  if (!items.length) return null;
  return (
    <section aria-label={title}>
      <h2 className="mb-1.5 px-1 text-[var(--text-body)] font-bold" style={{ color: "var(--color-text-strong)" }}>
        {title}
      </h2>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item, index) => (
          <BreakableChip key={`${item}-${index}`} mono={mono}>
            {item}
          </BreakableChip>
        ))}
      </div>
    </section>
  );
}

function BreakableChip({
  children,
  mono = false,
}: {
  children: string;
  mono?: boolean;
}) {
  return (
    <span
      className="inline-flex max-w-full min-w-0 items-center gap-1 rounded-[4px] px-1.5 py-[1px] text-[var(--text-meta)] font-medium"
      style={{
        background: "var(--color-card)",
        color: "var(--color-text-body)",
        border: "1px solid var(--color-divider)",
      }}
    >
      <span
        className={`${mono ? "font-[family-name:var(--font-mono)] " : ""}min-w-0 max-w-full break-all`}
      >
        {children}
      </span>
    </span>
  );
}

function ToolbarLink({
  children,
  className,
  href,
}: {
  children: ReactNode;
  className?: string;
  href: string;
}) {
  return (
    <Link
      className={`inline-flex min-h-11 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold ${className ?? ""}`}
      href={href}
      style={{
        background: "var(--color-card)",
        border: "1px solid var(--color-toolbar-border)",
        boxShadow: "var(--shadow-toolbar)",
        color: "var(--color-text-strong)",
      }}
    >
      {children}
    </Link>
  );
}

function normalizeSlug(value: string | string[] | undefined): string {
  const raw = Array.isArray(value) ? value.join("/") : (value ?? "");
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

function normalizeWikiScope(raw: string | null): "company" | "personal" | null {
  if (raw === "company" || raw === "personal") return raw;
  return null;
}
