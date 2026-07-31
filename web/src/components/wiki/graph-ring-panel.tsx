"use client";

/**
 * K5.1 — read-only KG page-graph visualization (deterministic SVG hop-rings).
 *
 * Pure presentational: renders exactly the {nodes, edges} subset it is handed
 * (the caller caps via cappedGraphView), centered on `currentNodeId`, with
 * 1-hop neighbors on an inner ring and the rest on an outer ring. No physics,
 * no graph library, no API calls, no data reshaping — deterministic on every
 * render (kg-k5-fe-plan.md §3). Designed to be reused by K4b /ask path-view and
 * K7 owner-scope panels: it knows nothing about the wiki page route or its state.
 *
 * Layout scales via a fixed viewBox + width:100% inside an overflow-hidden box,
 * so it can never introduce page-level horizontal scroll (P5 mobile parity).
 */
import { useMemo, useState } from "react";
import type { KgGraphEdge, KgGraphNode } from "@/lib/api";
import { useReducedMotion } from "@/lib/use-reduced-motion";
import {
  CONFLICT_PLACEHOLDER_LABEL,
  entityReadout,
  graphChipText,
  graphHopDistances,
  isClickableWikiNode,
  isEntityNode,
  isOwnPersonalNode,
  nodeConflictSets,
  nodeFillColor,
  nodeKindLabel,
  nodeStrokeColor,
  orderRels,
  PERSONAL_NODE_LABEL,
  presentNodeLabels,
  REL_GROUP_ORDER,
  relColor,
  wikiNodeHref,
} from "@/components/wiki/graph-shared";
import { GraphLegend } from "@/components/wiki/graph-legend";

const VIEW_W = 360;
const VIEW_H = 340;
const CENTER_X = VIEW_W / 2;
const CENTER_Y = 158;
const RING_1 = 66;
const RING_2 = 132;
const HIT_RADIUS = 24; // 투명 탭 타깃 — viewBox 스케일 ~1에서 ~48px
const LABEL_MAX = 12;
// 라벨 anchor 전환 임계 — 중심에서 이만큼 좌/우면 텍스트를 중심쪽으로 뻗어 viewBox 밖 잘림 방지.
const LABEL_SIDE_THRESHOLD = 30;

function relRank(rel: string | null): number {
  if (rel == null) return REL_GROUP_ORDER.length;
  const i = REL_GROUP_ORDER.indexOf(rel as (typeof REL_GROUP_ORDER)[number]);
  return i < 0 ? REL_GROUP_ORDER.length : i;
}

function truncate(s: string): string {
  return s.length > LABEL_MAX ? `${s.slice(0, LABEL_MAX - 1)}…` : s;
}

type Placed = {
  node: KgGraphNode;
  x: number;
  y: number;
  ring: 0 | 1 | 2;
  sortRel: string | null; // 결정론 레이아웃 **정렬 키 전용**(중심 방향 rel). 색/stroke에 미사용.
  status?: string; // CONFLICTS_WITH status (있을 때만)
  inConflict: boolean; // 인접 CONFLICTS_WITH 존재(status 유무 무관) — stroke warn 판정, K8
};

type Layout = { placed: Placed[]; byId: Map<string, Placed> };

function buildLayout(
  nodes: KgGraphNode[],
  edges: KgGraphEdge[],
  currentNodeId: string | null,
): Layout {
  const byNodeId = new Map<string, KgGraphNode>();
  for (const n of nodes) if (!byNodeId.has(n.id)) byNodeId.set(n.id, n); // dedup-safe

  const dist = graphHopDistances(edges, currentNodeId);

  // 무방향 인접 + (a,b)별 rel 목록 (중심 방향 rel/색 결정용)
  const neighbors = new Map<string, Set<string>>();
  const relBetween = new Map<string, string[]>();
  const pairKey = (a: string, b: string) => (a < b ? `${a}|${b}` : `${b}|${a}`);
  const addNb = (a: string, b: string) => {
    let s = neighbors.get(a);
    if (!s) neighbors.set(a, (s = new Set()));
    s.add(b);
  };
  for (const e of edges) {
    if (!byNodeId.has(e.src) || !byNodeId.has(e.dst)) continue;
    if (e.src === e.dst) continue; // self-loop: 노드가 자기 이웃이 되면 ring/색 계산이 왜곡됨
    addNb(e.src, e.dst);
    addNb(e.dst, e.src);
    const pk = pairKey(e.src, e.dst);
    const list = relBetween.get(pk);
    if (list) list.push(e.rel);
    else relBetween.set(pk, [e.rel]);
  }
  // 충돌 상태 + 충돌 여부(공용 nodeConflictSets, explorer/칩과 동일 규칙). status는 UNRESOLVED-wins,
  // conflictIds는 status 유무 무관 인접 CONFLICTS_WITH 존재 — stroke warn 판정에 쓴다(status-less
  // 충돌도 warn 유지, K8 회귀 차단). **렌더되는 두 노드 사이 충돌만** 센다 — 이웃 계산(위 byNodeId
  // 게이트)과 동일하게 off-graph 끝점 엣지는 제외해, counterpart가 안 그려진 노드가 warn stroke로
  // 오표시되지 않게 한다(explorer는 drawEdges로 이미 필터, ring도 내부 불변식 일치).
  const presentEdges = edges.filter((e) => byNodeId.has(e.src) && byNodeId.has(e.dst));
  const { status: conflictStatus, conflictIds } = nodeConflictSets(presentEdges);

  const hopOf = (id: string) =>
    id === currentNodeId ? 0 : (dist.get(id) ?? Number.POSITIVE_INFINITY);

  // 중심 방향(더 가까운 hop) 이웃들 중 rel 우선순위가 가장 높은 rel = 색
  const colorRelOf = (id: string): string | null => {
    const myHop = hopOf(id);
    let best: string | null = null;
    for (const nb of neighbors.get(id) ?? []) {
      if (hopOf(nb) >= myHop) continue; // 중심 방향만
      for (const rel of relBetween.get(pairKey(id, nb)) ?? []) {
        if (best == null || relRank(rel) < relRank(best)) best = rel;
      }
    }
    if (best != null) return best;
    // 중심 방향이 없으면(미해결/고립) 아무 incident rel
    for (const nb of neighbors.get(id) ?? []) {
      for (const rel of relBetween.get(pairKey(id, nb)) ?? []) {
        if (best == null || relRank(rel) < relRank(best)) best = rel;
      }
    }
    return best;
  };
  // 노드별 중심방향 rel은 한 번만 계산해 캐시한다 — 결정론 레이아웃의 **정렬 키**로만 쓴다
  // (cmp 1차 키). stroke는 상태 전용으로 바뀌어 이 rel을 색으로 재사용하지 않으므로,
  // 색이 아니라 정렬 의미를 담아 `sortRelById`로 명명한다(제거 시 /ask 링 배치가 뒤바뀜).
  const sortRelById = new Map<string, string | null>();
  for (const id of byNodeId.keys()) sortRelById.set(id, colorRelOf(id));

  const center = currentNodeId ? byNodeId.get(currentNodeId) ?? null : null;
  const ring1: KgGraphNode[] = [];
  const ring2: KgGraphNode[] = [];
  for (const n of byNodeId.values()) {
    if (n.id === currentNodeId) continue;
    (hopOf(n.id) === 1 ? ring1 : ring2).push(n);
  }

  const cmp = (a: KgGraphNode, b: KgGraphNode) => {
    const ra = relRank(sortRelById.get(a.id) ?? null);
    const rb = relRank(sortRelById.get(b.id) ?? null);
    if (ra !== rb) return ra - rb;
    if (a.materialized !== b.materialized) return a.materialized ? -1 : 1;
    return graphChipText(a).localeCompare(graphChipText(b));
  };
  ring1.sort(cmp);

  const placed: Placed[] = [];
  const byId = new Map<string, Placed>();
  const place = (n: KgGraphNode, x: number, y: number, ring: 0 | 1 | 2) => {
    const p: Placed = {
      node: n,
      x,
      y,
      ring,
      sortRel: sortRelById.get(n.id) ?? null, // 정렬 키 전용(색/stroke 미사용)
      status: conflictStatus.get(n.id),
      inConflict: conflictIds.has(n.id),
    };
    placed.push(p);
    byId.set(n.id, p);
  };

  if (center) place(center, CENTER_X, CENTER_Y, 0);

  const angleAt = (i: number, k: number, offset = 0) =>
    -Math.PI / 2 + offset + (i * 2 * Math.PI) / Math.max(k, 1);
  const ring1Angle = new Map<string, number>();
  ring1.forEach((n, i) => {
    const a = angleAt(i, ring1.length);
    ring1Angle.set(n.id, a);
    place(n, CENTER_X + RING_1 * Math.cos(a), CENTER_Y + RING_1 * Math.sin(a), 1);
  });

  // ring2는 ring1 부모 각도 기준 정렬 → 엣지 교차 감소
  const parentAngle = (n: KgGraphNode): number => {
    for (const nb of neighbors.get(n.id) ?? []) {
      const a = ring1Angle.get(nb);
      if (a != null) return a;
    }
    return Number.POSITIVE_INFINITY;
  };
  ring2.sort((a, b) => {
    const pa = parentAngle(a);
    const pb = parentAngle(b);
    if (pa !== pb) return pa - pb;
    return cmp(a, b);
  });
  // half-step offset으로 ring2를 ring1 사이에 끼운다 — 링당 노드 1개일 때 정상단 수직 정렬 방지.
  const ring2Offset = Math.PI / Math.max(ring2.length, 1);
  ring2.forEach((n, i) => {
    const a = angleAt(i, ring2.length, ring2Offset);
    place(n, CENTER_X + RING_2 * Math.cos(a), CENTER_Y + RING_2 * Math.sin(a), 2);
  });

  return { placed, byId };
}

export default function GraphRingPanel({
  nodes,
  edges,
  currentNodeId,
  compact,
  onNodeClick,
}: {
  nodes: KgGraphNode[];
  edges: KgGraphEdge[];
  currentNodeId: string | null;
  compact: boolean;
  onNodeClick?: (slug: string) => void;
}) {
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  // 모션 민감 사용자는 opacity transition 비활성(WCAG 2.3.3). 공용 훅이 OS 설정
  // 세션 중 변경도 구독해 반영한다.
  const reducedMotion = useReducedMotion();
  const { placed, byId } = useMemo(
    () => buildLayout(nodes, edges, currentNodeId),
    [nodes, edges, currentNodeId],
  );

  // 그릴 엣지: 양 끝점이 배치됐고 self-loop가 아닌 것만(zero-length line 방지).
  // 같은 노드쌍에 여러 rel이 겹칠 때 우선순위 높은 rel(충돌 등)을 **나중에** 그려
  // 위로 올린다 — 안 그러면 겹친 선 중 중요한 엣지가 가려진다(relRank 내림차순 정렬).
  const drawEdges = useMemo(
    () =>
      edges
        .filter((e) => e.src !== e.dst)
        .map((e) => ({ e, s: byId.get(e.src), d: byId.get(e.dst) }))
        .filter((x): x is { e: KgGraphEdge; s: Placed; d: Placed } => !!x.s && !!x.d)
        .sort((a, b) => relRank(b.e.rel) - relRank(a.e.rel)),
    [edges, byId],
  );

  // 범례는 실제로 그려지는 엣지의 rel만 보여준다 — self-loop/미배치로 선이 안 그려지는
  // rel을 범례에만 광고하지 않도록(잘못된 색 scent 방지).
  const relsPresent = useMemo(
    () => orderRels(drawEdges.map((x) => x.e.rel)),
    [drawEdges],
  );

  // 범례 표식 — 등장한 것만(공용 규칙). 종류는 중심 노드 제외(§7). 상태 표식은 placed에서 도출.
  const labelsPresent = useMemo(
    () => presentNodeLabels(placed.map((p) => p.node), currentNodeId),
    [placed, currentNodeId],
  );
  const legendFlags = useMemo(() => {
    let hasPersonal = false;
    let hasPlaceholder = false;
    let hasConflict = false;
    let hasResolvedConflict = false;
    for (const p of placed) {
      const isCenter = p.node.id === currentNodeId;
      // 개인 halo(동심원)·미생성 점선 테두리는 중심 노드에도 그대로 렌더되므로(isCurrent 무관)
      // 범례에 중심을 포함한다 — explorer가 전체 노드에서 도출하는 것과 동일 규칙.
      if (isOwnPersonalNode(p.node)) hasPersonal = true;
      if (!p.node.materialized) hasPlaceholder = true;
      // 충돌 stroke는 중심 노드엔 안 뜬다(nodeStrokeColor isCurrent 우선) — 없는 상태를 광고하지
      // 않도록 충돌 flag만 중심 제외(explorer 앵커 제외와 대칭).
      if (!isCenter && p.inConflict) {
        if (p.status === "RESOLVED") hasResolvedConflict = true;
        else hasConflict = true;
      }
    }
    return { hasPersonal, hasPlaceholder, hasConflict, hasResolvedConflict };
  }, [placed, currentNodeId]);
  const hasAnchor = currentNodeId != null && byId.has(currentNodeId);

  // 호버/포커스/탭 중인 노드의 "실제 텍스트"(전체, 안 잘림). 라벨은 12자로 잘리고
  // 모바일엔 hover가 없으므로, 선택 노드의 전체 내용을 그래프 아래 줄에 보여준다.
  const activePlaced = hoveredId != null ? byId.get(hoveredId) ?? null : null;
  const activeNode = activePlaced?.node ?? null;
  // FE4 — placeholder 충돌 counterpart(conflict status가 있는 미생성 노드)는 KG에 본문이 없으므로
  // slug를 주장 문장으로 readout하지 않고 명시 라벨 + slug(식별자)로 보여준다(칩 뷰와 parity).
  // 참고: claim 노드는 이제 저장된 display_title(사람이 읽는 헤드라인)이 node.title로
  // 투영돼 graphChipText가 그 헤드라인을 반환한다(FE 무변경). 전체 claim 문장 readout은
  // 후속 슬라이스(claim reader 라우트)로 분리됐다 — 여기선 헤드라인/식별자만 보여준다.
  // K9(U4) — :Entity 비클릭 매개 노드는 사람 말투 readout("이 페이지들은 ‘X’을(를) 함께 언급해서
  // 묶였어요 · 페이지 없음")로, bare "개체" chip이 버그처럼 읽히지 않게 한다(person도 동일 프레이밍).
  // 문구는 graph-shared `entityReadout` 단일 출처(노드 인스펙터와 뷰 무관 중립). ring엔
  // 인스펙터가 없어 entity는 여전히 비클릭이며 "페이지 없음"이 그 사실을 알린다.
  const activeText = activeNode
    ? isEntityNode(activeNode)
      ? entityReadout(activeNode)
      : activePlaced?.status != null && !activeNode.materialized
        ? `${CONFLICT_PLACEHOLDER_LABEL} · ${graphChipText(activeNode)}`
        : graphChipText(activeNode)
    : null;

  if (placed.length === 0) return null;

  return (
    <div>
      {/* 축약 범례(공용 GraphLegend) — 등장한 종류/관계/상태만. 폰(compact)은 `범례 보기`로 접어
          /ask 응답 카드가 부풀지 않게 한다. */}
      <div className="mb-2 px-1">
        <GraphLegend
          labels={labelsPresent}
          rels={relsPresent}
          compact={compact}
          hasAnchor={hasAnchor}
          hasPlaceholder={legendFlags.hasPlaceholder}
          hasConflict={legendFlags.hasConflict}
          hasResolvedConflict={legendFlags.hasResolvedConflict}
          hasPersonal={legendFlags.hasPersonal}
        />
      </div>

      <div className="w-full overflow-hidden">
        <svg
          className="mx-auto block h-auto w-full"
          style={{ maxWidth: compact ? "100%" : 420 }}
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          role="img"
          aria-label={`관련 지식 그래프 미니맵: 노드 ${placed.length}개, 관계 ${drawEdges.length}개`}
        >
          {/* 엣지 (노드 아래, 클릭/포커스 비대상) */}
          <g aria-hidden pointerEvents="none">
            {drawEdges.map(({ e, s, d }, i) => {
              const active = hoveredId != null && (e.src === hoveredId || e.dst === hoveredId);
              const dim = hoveredId != null && !active;
              return (
                <line
                  key={`${e.src}-${e.rel}-${e.dst}-${i}`}
                  x1={s.x}
                  y1={s.y}
                  x2={d.x}
                  y2={d.y}
                  stroke={relColor(e.rel)}
                  strokeWidth={active ? 2 : 1}
                  strokeOpacity={dim ? 0.15 : active ? 0.9 : 0.4}
                  // K8.5 FE1 — placeholder(미생성) 끝점에 닿는 엣지는 점선(노드 점선과
                  // 동일 시맨틱). 미해소 모순 counterpart가 placeholder인 우리 데이터에서
                  // CONFLICTS_WITH 선도 점선+빨강으로 보이게(스펙 B1 "점선+빨강 엣지/노드").
                  strokeDasharray={s.node.materialized && d.node.materialized ? undefined : "3 2"}
                />
              );
            })}
          </g>

          {/* 노드 */}
          {placed.map((p) => (
            <GraphNode
              key={p.node.id}
              placed={p}
              dim={hoveredId != null && hoveredId !== p.node.id}
              onHover={setHoveredId}
              onNodeClick={onNodeClick}
              reducedMotion={reducedMotion}
            />
          ))}
        </svg>
      </div>

      {/* 선택/호버 노드의 전체 텍스트 readout — 잘린 라벨·hover 없는 모바일 보완. */}
      <p
        className="mt-1 min-h-[1.5rem] break-words px-1 text-[var(--text-meta)] leading-[1.5] [overflow-wrap:anywhere]"
        style={{ color: activeText ? "var(--color-text-body)" : "var(--color-text-muted)" }}
        aria-live="polite"
      >
        {activeText ?? "노드에 마우스를 올리거나 탭하면 전체 내용이 표시됩니다."}
      </p>
    </div>
  );
}

function GraphNode({
  placed,
  dim,
  onHover,
  onNodeClick,
  reducedMotion,
}: {
  placed: Placed;
  dim: boolean;
  onHover: (id: string | null) => void;
  onNodeClick?: (slug: string) => void;
  reducedMotion: boolean;
}) {
  const [focused, setFocused] = useState(false);
  // `sortRel`(Placed)은 정렬 키 전용이라 stroke/색에 쓰지 않는다.
  const { node, x, y, ring, status, inConflict } = placed;
  const isCurrent = ring === 0;
  const r = isCurrent ? 11 : 8;
  const clickable = isClickableWikiNode(node);

  // fill = 종류(§Slice1 항목 2·5). 중심(현재) 노드는 §7 예외로 strong fill. placeholder는 채도를
  // 낮춰(fillOpacity) materialized와 fill 강도로도 구분(점선 테두리 단일 신호 보강).
  const fill = isCurrent ? "var(--color-text-strong)" : nodeFillColor(node.label);
  const fillOpacity = isCurrent || node.materialized ? 1 : 0.4;
  // stroke = 상태 전용(공용 nodeStrokeColor). 현재 노드 strong > 충돌 > divider. 충돌 warn 판정은
  // inConflict(인접 CONFLICTS_WITH)로, status 유무가 아니다(status-less 충돌도 warn 유지, K8).
  const stroke = nodeStrokeColor({ anchor: isCurrent, inConflict, status });

  const text = graphChipText(node);
  // K7.3 — caller 본인 personal 노드만 구분(두 플래그 AND). rel-색(stroke)·label(fill)과
  // 충돌하지 않게 personal accent halo로 표식하고, aria에 "내 개인 메모"를 덧붙인다.
  const isPersonal = isOwnPersonalNode(node);
  // 종류(+미생성) 라벨은 인스펙터·칩·explorer와 공유(nodeKindLabel).
  const ariaLabel = `${nodeKindLabel(node)}${
    isPersonal ? `·${PERSONAL_NODE_LABEL}` : ""
  }: ${text}${
    !isCurrent && inConflict
      ? `, ${status === "RESOLVED" ? "충돌 해소" : status === "UNRESOLVED" ? "충돌 미해소" : "충돌"}`
      : ""
  }`;
  // 좌/우 가장자리 노드는 텍스트를 중심쪽으로 뻗어 viewBox 밖 잘림 방지.
  const anchor =
    x > CENTER_X + LABEL_SIDE_THRESHOLD
      ? "end"
      : x < CENTER_X - LABEL_SIDE_THRESHOLD
        ? "start"
        : "middle";
  const labelDy = y + (ring === 2 && y > CENTER_Y ? r + 13 : r + 12);

  const graphic = (
    <>
      {/* 투명 탭/포커스 타깃 */}
      <circle cx={x} cy={y} r={HIT_RADIUS} fill="transparent" />
      {/* 키보드 포커스 링 — SVG <g>는 CSS outline이 안 보이므로 SVG로 그린다(WCAG 2.4.7). */}
      {focused ? (
        <circle
          cx={x}
          cy={y}
          r={r + 4}
          fill="none"
          stroke="var(--color-text-strong)"
          strokeWidth={2}
          pointerEvents="none"
        />
      ) : null}
      {/* K7.3 — 내 개인 메모 노드 accent halo. rel-색 stroke 바깥의 별도 동심원이라
          관계 색을 가리지 않고 "내 것"만 표식한다(personal↔company 시각 구분). */}
      {isPersonal ? (
        <circle
          cx={x}
          cy={y}
          r={r + 3.5}
          fill="none"
          stroke="var(--color-accent)"
          strokeWidth={1.5}
          pointerEvents="none"
        />
      ) : null}
      <circle
        cx={x}
        cy={y}
        r={r}
        fill={fill}
        fillOpacity={fillOpacity}
        stroke={stroke}
        strokeWidth={isCurrent ? 2.5 : 1.5}
        strokeDasharray={node.materialized ? undefined : "2 2"}
      />
      <text
        x={x}
        y={labelDy}
        textAnchor={anchor}
        fontSize={isCurrent ? 12 : 11}
        fontWeight={isCurrent ? 700 : 500}
        fill="var(--color-text-body)"
      >
        {truncate(text)}
      </text>
    </>
  );

  // hover/focus는 outer 래퍼에 둔다 — 비클릭 노드도 키보드 포커스로 엣지 강조가 동작.
  // focus 상태는 별도로 추적해 키보드 포커스 링을 그린다(WCAG 2.4.7).
  const hover = {
    onMouseEnter: () => onHover(node.id),
    onMouseLeave: () => onHover(null),
    onFocus: () => {
      setFocused(true);
      onHover(node.id);
    },
    onBlur: () => {
      setFocused(false);
      onHover(null);
    },
  };
  const wrapStyle = {
    opacity: dim ? 0.35 : 1,
    ...(reducedMotion ? {} : { transition: "opacity 120ms" }),
    // 포커스 표시는 SVG focus-ring circle로 그리므로 기본 UA outline은 끈다.
    outline: "none",
  } as const;

  const href = wikiNodeHref(node);
  if (clickable && href) {
    return (
      <a
        href={href}
        aria-label={ariaLabel}
        onClick={(ev) => {
          if (!onNodeClick) return;
          // modifier 클릭(새 탭/창)은 기본 동작 보존.
          if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
          ev.preventDefault();
          onNodeClick(node.slug!);
        }}
        style={{ cursor: "pointer", ...wrapStyle }}
        {...hover}
      >
        {/* 네이티브 호버 툴팁은 노드 메타데이터가 아니라 실제 텍스트(전체, 안 잘림)만. */}
        <title>{text}</title>
        {graphic}
      </a>
    );
  }
  // 비링크 노드(주장/소스/문서 등): 이동할 곳이 없으므로 클릭/탭하면 아래 readout에
  // 전체 텍스트를 고정한다(모바일은 hover가 없음). aria-label은 SR용 타입 맥락 유지.
  return (
    <g
      role="img"
      aria-label={ariaLabel}
      tabIndex={0}
      style={{ cursor: "pointer", ...wrapStyle }}
      onClick={() => onHover(node.id)}
      {...hover}
    >
      <title>{text}</title>
      {graphic}
    </g>
  );
}
