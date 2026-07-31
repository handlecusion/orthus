"use client";

import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type AuthConfig,
  type KgEntityGraphResponse,
  type KgGraphEdge,
  type KgGraphNode,
  type KgOverview,
  type OwnerFootprint,
  type WikiPageGraphResponse,
  type WikiSearchResponse,
} from "@/lib/api";
import { Banner, Card, ChannelHeader, Chip, Skeleton, StatTile, cx } from "@/components/ui";
import { ErrorBoundary } from "@/components/error-boundary";
import { useCachedResource } from "@/lib/use-cached-resource";
import {
  buildGraphGroups,
  cappedGraphView,
  GRAPH_COPY,
  NODE_LABEL,
  wikiSlugPath,
} from "@/components/wiki/graph-shared";
import { GraphChipGroups } from "@/components/wiki/graph-chip-list";

// 그래프 뷰는 viewport 측정 기반이라 SSR을 끈다(하이드레이션 mismatch 방지) — wiki 페이지와 동일.
const GraphExplorerPanel = dynamic(
  () => import("@/components/wiki/graph-explorer-panel"),
  { ssr: false },
);

// by_label 브레이크다운은 :Entity를 뺀다(개체는 전용 타일). 나머지를 NODE_LABEL 순으로 표시.
const BREAKDOWN_ORDER = ["WikiPage", "WikiClaim", "WikiSource", "Document", "StructuredFact"];

// 전용 화면 캔버스 — 탐색기 기본(480px 정사각)은 wiki 사이드 패널용이라 넓은 화면에서 작다.
// 그래프가 주인공이므로 가로형 viewBox로 폭을 채운다. 렌더 높이 = 렌더 폭 × (H/W)라
// 16:9보다 조금 높은 비율을 써서 세로도 충분히 확보한다(노드가 상하로 퍼지는 force 레이아웃).
const CANVAS_VIEW_W = 880;
const CANVAS_VIEW_H = 520;
const CANVAS_ASPECT = CANVAS_VIEW_H / CANVAS_VIEW_W;
const CANVAS_MIN_W = 560; // 480(기본)보다 확실히 넓게
const CANVAS_MAX_W = 1280;

type BuiltGraph = ReturnType<typeof buildGraphGroups>;
type CappedGraph = ReturnType<typeof cappedGraphView>;

// KgEntityGraphResponse / WikiPageGraphResponse를 탐색기 시드로 변환하는 공용 헬퍼.
// slug가 있으면(페이지 그래프) 그 페이지가 앵커, 없으면(개체 지도) 앵커 없는 자유 그래프.
//
// `cap`: 페이지 그래프는 wiki 페이지와 같은 관례로 초기 시드를 GRAPH_TOTAL_CAP(20)까지만 보여주고
// 더블클릭으로 넓힌다. 반면 **랜딩 개체 지도는 캡하지 않는다** — 진입 즉시 사내 지식 전경을
// 보여주는 게 목적이라 서버 상한(kg_overview_limit)이 이미 렌더 가능한 크기이고, 탐색기 자체
// 누적 캡(MAX_NODES=200)이 안전장치다. 여기서 20으로 접으면 지도가 텅 비어 보인다.
function buildFromGraph(
  data: {
    supported: boolean;
    nodes: KgGraphNode[];
    edges: KgGraphEdge[];
    truncated: boolean;
    slug?: string;
  } | null,
  opts?: { cap?: boolean },
): { built: BuiltGraph; seed: CappedGraph | null } | null {
  if (!data || data.supported !== true) return null;
  const shim: WikiPageGraphResponse = {
    slug: data.slug ?? "",
    supported: true,
    reason: null,
    truncated: data.truncated,
    nodes: data.nodes,
    edges: data.edges,
    entity_teaser: null,
    personal_entity_search_deferred: false,
  };
  const built = buildGraphGroups(shim);
  if (built.groups.length === 0) return { built, seed: null };
  const seed =
    opts?.cap === false
      ? { nodes: data.nodes, edges: data.edges, currentNodeId: built.currentNodeId }
      : cappedGraphView(shim, built);
  return { built, seed };
}

export default function KnowledgeGraphPage() {
  const router = useRouter();

  // compact-shell 임계는 앱 전역과 동일한 <760px(대시보드 레이아웃과 일치).
  const [compact, setCompact] = useState(false);
  // 캔버스 폭 = 가용 폭과 "가용 높이/종횡비" 중 작은 값 — 가로형이라 세로가 화면을 덜 잡아먹지만
  // 짧은 창에서 그래프가 폴드 아래로 밀리지 않게 높이도 함께 본다. 가용 폭은 좌측 nav +
  // 인스펙터 도킹(~300) + 여백을 뺀 값.
  const [canvasMax, setCanvasMax] = useState(CANVAS_MIN_W);
  useEffect(() => {
    const sync = () => {
      const narrow = window.innerWidth < 760;
      setCompact(narrow);
      const availW = window.innerWidth - (narrow ? 32 : 620);
      const availH = window.innerHeight - 300; // 헤더+통계+검색바+카드 크롬
      setCanvasMax(
        Math.max(CANVAS_MIN_W, Math.min(availW, availH / CANVAS_ASPECT, CANVAS_MAX_W)),
      );
    };
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  // company-scope KG 집계 헤더.
  const overview = useCachedResource<KgOverview>("kg:overview", () => api.getKgOverview());
  const ov = overview.data;

  // owner-scope가 켜진 경우에만 내 personal footprint를 함께 보여준다(wiki 페이지와 캐시 키 공유).
  const authConfig = useCachedResource<AuthConfig>("auth:config", () => api.getAuthConfig()).data;
  const ownerScope = authConfig?.owner_scope_enabled === true;
  const footprint =
    useCachedResource<OwnerFootprint>(
      ownerScope ? "wiki:kg-footprint" : null,
      () => api.getOwnerKgFootprint(),
    ).data ?? null;

  // 랜딩 개체 지도 — 진입 즉시 로드(검색 없이 바로 사내 지식 그래프 표시).
  const entityGraph = useCachedResource<KgEntityGraphResponse>(
    "kg:entity-graph",
    () => api.getKgEntityGraph(),
  );
  // 랜딩 지도는 캡 없이 전체를 렌더한다(서버 상한이 이미 렌더 가능 크기).
  const entityBuilt = useMemo(
    () => buildFromGraph(entityGraph.data, { cap: false }),
    [entityGraph.data],
  );

  // 검색→시드: 입력을 300ms 디바운스해 /wiki/search로 특정 페이지 그래프로 점프한다.
  const [queryInput, setQueryInput] = useState("");
  const [debounced, setDebounced] = useState("");
  const [showResults, setShowResults] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(queryInput.trim()), 300);
    return () => clearTimeout(t);
  }, [queryInput]);
  const searchRes = useCachedResource<WikiSearchResponse>(
    debounced.length >= 1 ? `kg:search:${debounced}` : null,
    () => api.searchWiki(debounced, { limit: 10 }),
  );

  // 검색 자가발견성 — 처음 온 사용자가 설명 없이 "여기서 검색한다"를 알게 하는 장치:
  // ① 상시 accent 테두리(포커스는 링 글로우로만 구분) ② `/` 단축키(+바 우측 힌트 배지)
  // ③ 추천 칩. 자동 포커스는 뺐다(owner 결정 — 진입하자마자 입력창이 켜지는 건 과함).
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const [searchFocused, setSearchFocused] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      e.preventDefault();
      searchInputRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // "바로 찾아보기" 추천 칩 — 이미 받은 개체 지도에서 언급량 상위 개체명을 재사용(추가 API 0).
  // 클릭이 곧 검색 실행이라 설명 없이 사용법을 보여준다. person은 제외 — K9 티저의 person
  // 억제(D-TZ B)와 같은 보수 기준(자동 노출 표면에 사람 이름을 승격하지 않는다).
  const topEntities = useMemo(() => {
    const data = entityGraph.data;
    if (!data || data.supported !== true) return [];
    const seen = new Set<string>();
    const names: string[] = [];
    const sorted = [...data.nodes].sort(
      (a, b) => (b.mention_count ?? 0) - (a.mention_count ?? 0),
    );
    for (const n of sorted) {
      const name = (n.title ?? "").trim();
      if (n.label !== "Entity" || !name || n.entity_kind === "person" || seen.has(name)) continue;
      seen.add(name);
      names.push(name);
      if (names.length >= 6) break;
    }
    return names;
  }, [entityGraph.data]);

  const searchChip = useCallback((name: string) => {
    setQueryInput(name);
    setShowResults(true);
    searchInputRef.current?.focus({ preventScroll: true });
  }, []);

  // 선택된 시드 slug — 있으면 개체 지도 대신 그 페이지 그래프를 렌더한다.
  const [seedSlug, setSeedSlug] = useState<string | null>(null);
  const seededGraphRes = useCachedResource<WikiPageGraphResponse>(
    seedSlug ? `kg:graph:${seedSlug}` : null,
    () => api.getWikiPageGraph(seedSlug as string, 1, { includeEntity: true }),
  );
  const seededBuilt = useMemo(
    () => buildFromGraph(seededGraphRes.data),
    [seededGraphRes.data],
  );

  const navTo = useCallback(
    (s: string) => router.push(`/wiki/${wikiSlugPath(s)}`),
    [router],
  );

  const selectSeed = useCallback((slug: string, title: string) => {
    setSeedSlug(slug);
    setQueryInput(title);
    setShowResults(false); // 선택 후 결과 목록을 닫는다(그래프를 덮지 않게).
  }, []);

  const backToMap = useCallback(() => {
    setSeedSlug(null);
    setQueryInput("");
    setShowResults(false);
  }, []);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <ChannelHeader
        title="지식그래프"
        glyph="#"
        subtitle="검색하면 그 페이지의 연결 그래프로 바로 이동하고, 개체를 더블클릭하면 이웃으로 펼쳐집니다."
      />
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">
        {/* 검색이 첫 액션 — 제목 바로 아래(통계·그래프보다 위). 진입 시 자동 포커스(데스크톱) +
            포커스 accent 링 + `/` 단축키 + 추천 칩으로 "여기서 검색한다"를 설명 없이 전달한다. */}
        <div>
          <div
            className="flex items-center gap-2.5 rounded-[var(--radius-card)] px-4"
            style={{
              minHeight: 56,
              background: "var(--color-card)",
              // 테두리는 포커스와 무관하게 항상 accent — 검색바가 페이지의 첫 액션임을 상시
              // 표시한다(owner 결정). 포커스는 링 글로우로만 구분.
              border: "1.5px solid var(--color-accent)",
              boxShadow: searchFocused
                ? "0 0 0 3px color-mix(in srgb, var(--color-accent) 20%, transparent), var(--shadow-card)"
                : "var(--shadow-card)",
              transition: "box-shadow 120ms ease",
            }}
          >
            <span style={{ color: "var(--color-accent)" }} aria-hidden>
              <SearchIcon />
            </span>
            <input
              ref={searchInputRef}
              id="kg-seed-search"
              type="search"
              value={queryInput}
              onChange={(e) => {
                setQueryInput(e.target.value);
                setShowResults(true);
              }}
              onFocus={() => {
                setSearchFocused(true);
                if (debounced.length >= 1) setShowResults(true);
              }}
              onBlur={() => setSearchFocused(false)}
              placeholder="인물·프로젝트·페이지를 검색해 그래프 탐색"
              aria-label="지식그래프 페이지 검색"
              className="min-w-0 flex-1 bg-transparent text-[var(--text-body)] outline-none"
              style={{ color: "var(--color-text-strong)" }}
              autoComplete="off"
            />
            {!compact ? (
              <kbd
                className="shrink-0 rounded-[6px] px-2 py-0.5 text-[var(--text-meta)]"
                style={{
                  border: "1px solid var(--color-divider)",
                  color: "var(--color-text-muted)",
                  background: "var(--color-app-canvas)",
                }}
                aria-hidden
              >
                /
              </kbd>
            ) : null}
          </div>
          {/* 추천 칩 — 클릭 = 검색 실행(사용법 시연). 시드 선택 중엔 숨겨 그래프에 집중. */}
          {topEntities.length > 0 && seedSlug == null ? (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span
                className="text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                바로 찾아보기
              </span>
              {topEntities.map((name) => (
                <button
                  key={name}
                  type="button"
                  onClick={() => searchChip(name)}
                  className="rounded-full px-3 text-[var(--text-body-sm)] transition-colors"
                  style={{
                    minHeight: compact ? 44 : 32,
                    border: "1px solid var(--color-divider)",
                    background: "var(--color-card)",
                    color: "var(--color-text-body)",
                  }}
                >
                  {name}
                </button>
              ))}
            </div>
          ) : null}
          {showResults && debounced.length >= 1 ? (
            <SearchResults res={searchRes} onSelect={selectSeed} />
          ) : null}
        </div>

        {/* 집계 헤더 */}
        <div className="mt-4">
          <KgOverviewSection loading={overview.loading} overview={ov} footprint={footprint} />
        </div>

        {/* 그래프 — 기본은 개체 지도, 검색으로 특정 페이지를 고르면 그 페이지 그래프 */}
        <div className="mt-4">
          {seedSlug == null ? (
            <GraphView
              title="개체 지도 · 인물·프로젝트·도구"
              built={entityBuilt}
              loading={entityGraph.loading}
              error={entityGraph.error}
              supportedFalse={entityGraph.data?.supported === false}
              reason={entityGraph.data?.reason ?? null}
              anchorNodeId={null}
              canvasMax={canvasMax}
              compact={compact}
              onNavigate={navTo}
              emptyCopy="아직 표시할 개체 연결이 없습니다. 위 검색으로 페이지를 찾아보세요."
            />
          ) : (
            <GraphView
              title={seedSlug}
              onBack={backToMap}
              built={seededBuilt}
              loading={seededGraphRes.loading}
              error={seededGraphRes.error}
              supportedFalse={seededGraphRes.data?.supported === false}
              reason={seededGraphRes.data?.reason ?? null}
              anchorNodeId={seededBuilt?.seed?.currentNodeId ?? null}
              canvasMax={canvasMax}
              compact={compact}
              onNavigate={navTo}
              emptyCopy={GRAPH_COPY.empty}
            />
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------ overview 헤더 ------------------------------ */

function KgOverviewSection({
  loading,
  overview,
  footprint,
}: {
  loading: boolean;
  overview: KgOverview | null;
  footprint: OwnerFootprint | null;
}) {
  if (loading && !overview) {
    return (
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} className="h-[68px]" />
        ))}
      </div>
    );
  }
  if (!overview || overview.status !== "ready") {
    const copy =
      overview?.status === "disabled"
        ? "이 노드에서는 지식그래프가 비활성화되어 있습니다."
        : overview?.status === "rebuilding"
          ? "지식그래프를 재구축하는 중입니다. 잠시 후 다시 확인하세요."
          : "지식그래프를 일시적으로 불러올 수 없습니다.";
    return (
      <Banner tone="warn" title="지식그래프 상태">
        {copy}
      </Banner>
    );
  }

  const breakdown = BREAKDOWN_ORDER.filter((label) => (overview.by_label[label] ?? 0) > 0).map(
    (label) => `${NODE_LABEL[label] ?? label} ${overview.by_label[label]}`,
  );

  return (
    <div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <StatTile label="노드" value={overview.node_count.toLocaleString()} />
        <StatTile label="연결" value={overview.edge_count.toLocaleString()} />
        <StatTile label="개체" value={overview.entity_count.toLocaleString()} />
        <StatTile
          label="미해결 모순"
          value={overview.unresolved_conflicts.toLocaleString()}
        />
      </div>
      {breakdown.length ? (
        <p className="mt-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          {breakdown.join(" · ")}
          {overview.resolved_conflicts > 0
            ? ` · 해결된 모순 ${overview.resolved_conflicts}`
            : ""}
        </p>
      ) : null}
      {footprint && footprint.status === "ready" && footprint.node_count > 0 ? (
        <p className="mt-1 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          내 개인 메모: 노드 {footprint.node_count} · 연결 {footprint.edge_count}
        </p>
      ) : null}
    </div>
  );
}

/* ------------------------------ 검색 결과 목록 ------------------------------ */

function SearchResults({
  res,
  onSelect,
}: {
  res: ReturnType<typeof useCachedResource<WikiSearchResponse>>;
  onSelect: (slug: string, title: string) => void;
}) {
  if (res.loading && !res.data) {
    return (
      <div className="mt-2 flex flex-col gap-1">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-[44px]" />
        ))}
      </div>
    );
  }
  if (res.error) {
    return (
      <p className="mt-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
        검색 중 오류가 발생했습니다.
      </p>
    );
  }
  const items = res.data?.items ?? [];
  if (items.length === 0) {
    return (
      <p className="mt-2 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
        일치하는 페이지가 없습니다.
      </p>
    );
  }
  return (
    <ul className="mt-2 flex flex-col gap-1" role="listbox" aria-label="검색 결과">
      {items.map((item) => (
        <li key={item.slug}>
          <button
            type="button"
            onClick={() => onSelect(item.slug, item.title)}
            className={cx(
              "flex w-full flex-col gap-0.5 rounded-[var(--radius-card)] px-3 py-2 text-left transition-colors",
            )}
            style={{
              minHeight: 44,
              background: "var(--color-card)",
              border: "1px solid var(--color-divider)",
            }}
          >
            <span className="flex flex-wrap items-center gap-1.5">
              <span
                className="truncate text-[var(--text-body-sm)] font-semibold"
                style={{ color: "var(--color-text-strong)" }}
              >
                {item.title || item.slug}
              </span>
              <Chip>{NODE_LABEL[item.kind] ?? item.kind}</Chip>
              {item.scope === "personal" ? <Chip>내 개인</Chip> : null}
            </span>
            {item.snippet ? (
              <span
                className="line-clamp-2 text-[var(--text-meta)]"
                style={{ color: "var(--color-text-muted)" }}
              >
                {item.snippet}
              </span>
            ) : null}
          </button>
        </li>
      ))}
    </ul>
  );
}

/* ------------------------------ 그래프 뷰 ------------------------------ */

function GraphView({
  title,
  onBack,
  built,
  loading,
  error,
  supportedFalse,
  reason,
  anchorNodeId,
  canvasMax,
  compact,
  onNavigate,
  emptyCopy,
}: {
  title: string;
  onBack?: () => void;
  built: { built: BuiltGraph; seed: CappedGraph | null } | null;
  loading: boolean;
  error: unknown;
  supportedFalse: boolean;
  reason: string | null;
  anchorNodeId: string | null;
  canvasMax: number;
  compact: boolean;
  onNavigate: (slug: string) => void;
  emptyCopy: string;
}) {
  const header = (
    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
      <span
        className="truncate text-[var(--text-body-sm)] font-semibold [overflow-wrap:anywhere]"
        style={{ color: "var(--color-text-strong)" }}
      >
        {title}
      </span>
      {onBack ? (
        <button
          type="button"
          onClick={onBack}
          className="rounded-[var(--radius-toolbar)] px-2.5 py-1 text-[var(--text-meta)]"
          style={{
            minHeight: 44,
            color: "var(--color-text-muted)",
            border: "1px solid var(--color-divider)",
          }}
        >
          ← 개체 지도로
        </button>
      ) : null}
    </div>
  );

  let body: React.ReactNode;
  if (error) {
    body = (
      <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
        페이지를 찾을 수 없습니다.
      </p>
    );
  } else if (supportedFalse) {
    body = (
      <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
        {reason === "kg_disabled" ? GRAPH_COPY.disabled : GRAPH_COPY.unavailable}
      </p>
    );
  } else if (loading && !built) {
    body = <Skeleton className="h-[360px]" />;
  } else if (built && built.built.groups.length && built.seed && built.seed.nodes.length) {
    // 그래프 렌더 실패는 fail-open: 칩 리스트로 자동 강등(wiki 페이지와 동일).
    body = (
      <ErrorBoundary
        fallback={<GraphChipGroups built={built.built} busy={false} />}
        resetKey={anchorNodeId ?? title}
        onError={(err) => console.error("graph render failed; showing chip fallback", err)}
      >
        <div>
          <GraphExplorerPanel
            key={anchorNodeId ?? title}
            anchorNodeId={anchorNodeId}
            compact={compact}
            maxWidthPx={canvasMax}
            // 가로형은 데스크톱 전용 — 폰은 폭이 좁아 landscape면 세로가 짜부라진다
            // (실측 322x190). compact는 탐색기 기본 정사각(360x360)을 그대로 쓴다.
            viewHeight={compact ? undefined : CANVAS_VIEW_H}
            viewWidth={compact ? undefined : CANVAS_VIEW_W}
            onNodeNavigate={onNavigate}
            seedEdges={built.seed.edges}
            seedNodes={built.seed.nodes}
          />
        </div>
      </ErrorBoundary>
    );
  } else if (built && built.built.groups.length) {
    body = <GraphChipGroups built={built.built} busy={false} />;
  } else {
    body = (
      <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
        {emptyCopy}
      </p>
    );
  }

  return (
    <Card className="p-4">
      {header}
      {body}
    </Card>
  );
}

function SearchIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.2-3.2" />
    </svg>
  );
}
