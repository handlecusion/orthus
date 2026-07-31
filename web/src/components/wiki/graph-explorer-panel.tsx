"use client";

/**
 * E2 — 인터랙티브 KG 탐색기 (Neo4j Browser식).
 *
 * 고정 2-링 스냅샷(graph-ring-panel, /ask에서 계속 사용) 대신 wiki 페이지 그래프 패널에서
 * 쓰는 누적 탐색기다: 노드 더블클릭/더블탭/Enter → `GET /wiki/graph/expand` 1-hop fetch →
 * 클라이언트 병합 → force 레이아웃 재배치. 단일클릭=선택(readout), 드래그=노드 고정(pin),
 * 배경 드래그=pan, 휠/핀치=zoom.
 *
 * 경계(설계 §4.2.1): 좌표 계산은 graph-force-layout.ts 순수 함수에만 있고 여기는 좌표를
 * 구독(useAnimatedPositions)해 그릴 뿐이다. read-only — 서버 write 없음, 캡 200 도달 시
 * 확장 차단. 누적 상태 키는 (label, scope, id)(K7.2), 렌더 dedup은 기존 패널과 동일하게
 * id first-wins.
 */
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { api, ApiError, type KgGraphEdge, type KgGraphNode } from "@/lib/api";
import { useReducedMotion } from "@/lib/use-reduced-motion";
import {
  capOrderedRelGroups,
  conflictMetaForNode,
  CONFLICT_PLACEHOLDER_LABEL,
  entityReadout,
  graphChipText,
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
import {
  type InspectorRelationGroup,
  EmptyInspectorHint,
  GraphNodeInspector,
  InspectorCenteredState,
  SECONDARY_BUTTON_STYLE,
} from "@/components/wiki/graph-node-inspector";
import { ErrorBoundary } from "@/components/error-boundary";
import {
  computeForceLayout,
  type LayoutEdgeInput,
  type LayoutNodeInput,
  type LayoutPoint,
} from "@/components/wiki/graph-force-layout";

// viewBox 기본 치수 — wiki 페이지/`/ask` 관련-그래프 패널의 기존 정사각 캔버스(회귀 고정).
// 전용 화면은 viewWidth/viewHeight prop으로 가로형(landscape)을 요청해 넓은 화면을 채운다.
const DEFAULT_VIEW_W = 360;
const DEFAULT_VIEW_H = 360;
// 데스크톱 캔버스 폭 상한 기본값 — wiki 페이지/`/ask` 관련-그래프 패널의 기존 폭(회귀 고정).
const DEFAULT_MAX_WIDTH_PX = 480;
const MAX_NODES = 200; // 누적 총상한 (D4)
const LABEL_MAX = 12;
// 탭 타깃 목표 지름(화면 px). viewBox 고정 반지름은 카드 렌더 폭에 따라 화면 크기가
// 변해 폰(카드 ~300–330px)에서 44px 계약에 미달했다(E3 실측 38–42px) — svg 렌더 배율을
// ResizeObserver로 측정해 world 반지름을 역보정한다: r = (TAP/2)/ratio/zoom → 화면
// 지름은 뷰포트/줌과 무관하게 항상 TAP_TARGET_PX.
const TAP_TARGET_PX = 46;
const DRAG_THRESHOLD = 4; // svg 좌표 기준 — 이하 이동은 클릭
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 3;
// 도킹↔시트 모드 단일 임계(px). compact(<760, P5 svg maxWidth 용도)와 **별개**다 — 이 임계로만
// 인스펙터 배치를 고른다(임계 이중정의 금지). window.innerWidth는 상시 사이드바+rail+콘텐츠 패딩을
// 안 빼 실 그래프 폭을 과대추정하므로, 도킹은 여유 있는 폭에서만(≥1100) 켠다. 미만은 하단 시트.
const INSPECTOR_DOCK_WIDTH = 1100;
const INSPECTOR_WIDTH_PX = 280;

const EXPLORER_COPY = {
  capBlocked: `노드 ${MAX_NODES}개 상한에 도달했습니다. 그래프를 리셋한 뒤 다시 탐색하세요.`,
  placeholderNode: "미생성 노드는 본문이 없어 확장할 수 없습니다.",
  expandNotFound: "이 노드는 확장할 수 없습니다 (접근 불가이거나 존재하지 않음).",
  expandUnavailable: "지식 그래프를 일시적으로 확장할 수 없습니다.",
  expandNone: "추가로 연결된 이웃이 없습니다.",
  serverTruncated: " (서버 상한으로 일부만 표시)",
  hint: "노드를 클릭하면 내용이, 더블클릭(더블탭)하면 이웃이 펼쳐집니다. 배경을 끌면 이동, 휠/핀치로 확대합니다.",
} as const;

function relRank(rel: string | null): number {
  if (rel == null) return REL_GROUP_ORDER.length;
  const i = REL_GROUP_ORDER.indexOf(rel as (typeof REL_GROUP_ORDER)[number]);
  return i < 0 ? REL_GROUP_ORDER.length : i;
}

function truncate(s: string): string {
  return s.length > LABEL_MAX ? `${s.slice(0, LABEL_MAX - 1)}…` : s;
}

// K7.2 — 누적 상태 키. UUID/slug id 공간이 한 namespace에 섞이고 owner placeholder가
// slug를 공유할 수 있어 (label, scope, id)로 키한다. JSON.stringify는 충돌-안전.
function nodeKey(n: KgGraphNode): string {
  return JSON.stringify([n.label, n.scope, n.id]);
}

// 서버 canonical 엣지 신원(gate `kg_edge_dedup_key`)과 동일 — cappedGraphView와 같은 규약.
function edgeKey(e: KgGraphEdge): string {
  return JSON.stringify([e.src, e.rel, e.dst, e.scope]);
}

// setPointerCapture는 이미 해제된/비활성 pointerId에 NotFoundError를 던질 수 있다
// (연속 탭·합성 이벤트·일부 모바일 브라우저). 캡처는 최적화일 뿐 필수가 아니므로 무시(E3).
function safeCapturePointer(el: Element, pointerId: number): void {
  try {
    el.setPointerCapture(pointerId);
  } catch {
    // no-op — 캡처 실패 시에도 pointermove/up은 요소 위에서 계속 수신된다.
  }
}

function hash01(s: string, salt: number): number {
  let h = 0x811c9dc5 ^ salt;
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0) / 0x100000000;
}

// 확장 노드 주변 스폰 위치 — 부모 좌표 + key 결정론 오프셋(입력 순서 무관).
function spawnNear(origin: LayoutPoint, key: string): LayoutPoint {
  const angle = hash01(key, 3) * 2 * Math.PI;
  const radius = 36 + hash01(key, 4) * 36;
  return { x: origin.x + radius * Math.cos(angle), y: origin.y + radius * Math.sin(angle) };
}

type GraphState = {
  nodesByKey: Map<string, KgGraphNode>;
  edgesByKey: Map<string, KgGraphEdge>;
  spawnSeeds: Map<string, LayoutPoint>; // 노드 최초 등장 시 시작 좌표(레이아웃 seed)
};

function emptyGraphState(): GraphState {
  return { nodesByKey: new Map(), edgesByKey: new Map(), spawnSeeds: new Map() };
}

/**
 * 누적 병합(불변). 노드는 (label,scope,id) 키로 dedup하고 K7/K9 필드
 * (`scope`/`is_own_personal`/`mention_count`/`entity_kind`)는 최초 값을 보존한다 —
 * 단 placeholder→materialized 승격과 title 보강만 허용(더 풍부한 사본으로 교체).
 * 캡(MAX_NODES) 도달 시 신규 노드를 더 받지 않고 capped=true를 알린다. 엣지는 양 끝
 * id가 누적 집합에 있는 것만 추가한다.
 */
function mergeGraph(
  prev: GraphState,
  nodes: KgGraphNode[],
  edges: KgGraphEdge[],
  origin?: LayoutPoint,
): { next: GraphState; added: number; capped: boolean } {
  const nodesByKey = new Map(prev.nodesByKey);
  const edgesByKey = new Map(prev.edgesByKey);
  const spawnSeeds = new Map(prev.spawnSeeds);
  let added = 0;
  let capped = false;
  const knownIds = new Set<string>();
  for (const n of nodesByKey.values()) knownIds.add(n.id);
  for (const n of nodes) {
    const key = nodeKey(n);
    const existing = nodesByKey.get(key);
    if (existing) {
      // 승격 병합만 — 필드 보존 원칙(C1) 아래 더 구체적인 사본으로 교체.
      if ((!existing.materialized && n.materialized) || (existing.title == null && n.title != null)) {
        nodesByKey.set(key, { ...existing, ...n });
      }
      continue;
    }
    if (nodesByKey.size >= MAX_NODES) {
      capped = true;
      continue;
    }
    nodesByKey.set(key, n);
    knownIds.add(n.id);
    added += 1;
    if (origin) spawnSeeds.set(n.id, spawnNear(origin, key));
  }
  for (const e of edges) {
    if (!knownIds.has(e.src) || !knownIds.has(e.dst)) continue;
    const key = edgeKey(e);
    if (!edgesByKey.has(key)) edgesByKey.set(key, e);
  }
  return { next: { nodesByKey, edgesByKey, spawnSeeds }, added, capped };
}

/**
 * 위치 구독 훅(경계 조건 3) — 목표 좌표 맵이 바뀌면 이전 표시 좌표에서 목표로 rAF 보간한다.
 * reduced-motion이면 즉시 점프. 신규 키는 목표 위치에서 시작(스폰 seed가 이미 부모 근처라
 * 시각적 점프가 작다). ref 접근/setState는 전부 effect·rAF 콜백 안에서만 일어난다.
 */
function useAnimatedPositions(
  target: Map<string, LayoutPoint>,
  animate: boolean,
): Map<string, LayoutPoint> {
  const [displayed, setDisplayed] = useState(target);
  const displayedRef = useRef(target); // effect/rAF 안에서만 읽고 쓴다
  useEffect(() => {
    let raf = 0;
    if (!animate) {
      raf = requestAnimationFrame(() => {
        displayedRef.current = target;
        setDisplayed(target);
      });
      return () => cancelAnimationFrame(raf);
    }
    const from = displayedRef.current;
    const start = performance.now();
    const DURATION = 320;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / DURATION);
      const ease = 1 - (1 - t) * (1 - t) * (1 - t); // easeOutCubic
      const frame = new Map<string, LayoutPoint>();
      for (const [key, to] of target) {
        const prev = from.get(key) ?? to;
        frame.set(key, {
          x: prev.x + (to.x - prev.x) * ease,
          y: prev.y + (to.y - prev.y) * ease,
        });
      }
      displayedRef.current = frame;
      setDisplayed(frame);
      if (t < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target, animate]);
  return displayed;
}

type ViewTransform = { tx: number; ty: number; k: number };
const VIEW_DEFAULT: ViewTransform = { tx: 0, ty: 0, k: 1 };

export default function GraphExplorerPanel({
  seedNodes,
  seedEdges,
  anchorNodeId,
  compact,
  onNodeNavigate,
  maxWidthPx,
  viewWidth,
  viewHeight,
}: {
  seedNodes: KgGraphNode[];
  seedEdges: KgGraphEdge[];
  anchorNodeId: string | null;
  compact: boolean;
  onNodeNavigate?: (slug: string) => void;
  // 데스크톱 캔버스 폭 상한(px). 기본 `DEFAULT_MAX_WIDTH_PX`(480)은 wiki 페이지/`/ask`의
  // 좁은 관련-그래프 패널 폭이다. 그래프가 주인공인 전용 화면(/dashboard/graph)은 더 큰 값을
  // 넘겨 캔버스를 넓힌다. compact(<760)는 기존대로 100%라 이 값의 영향을 받지 않는다.
  maxWidthPx?: number;
  // viewBox 치수(= 캔버스 종횡비). 기본 360x360 정사각은 좁은 사이드 패널용이라 넓은 화면에서
  // 좌우가 남는다. 전용 화면은 가로형(예: 880x520)을 넘겨 폭을 채운다. svg는 `h-auto w-full`
  // 이라 렌더 높이 = 렌더 폭 × (viewHeight/viewWidth)로 따라온다. 좌표 변환/pan-zoom/탭타깃
  // 보정이 모두 이 두 값 기준이라 값만 바꾸면 일관되게 동작한다.
  viewWidth?: number;
  viewHeight?: number;
}) {
  const VIEW_W = viewWidth ?? DEFAULT_VIEW_W;
  const VIEW_H = viewHeight ?? DEFAULT_VIEW_H;
  const reducedMotion = useReducedMotion();
  const svgRef = useRef<SVGSVGElement | null>(null);
  // svg 렌더 배율(화면px / viewBox unit) — 탭 타깃 화면-px 보정용(TAP_TARGET_PX 주석).
  // 초기 측정은 rAF(동기 setState-in-effect 회피), 이후 ResizeObserver 콜백이 갱신.
  const [renderRatio, setRenderRatio] = useState(1);
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const update = () => {
      const w = el.getBoundingClientRect().width;
      if (w > 0) setRenderRatio(w / VIEW_W);
    };
    const raf = requestAnimationFrame(update);
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, [VIEW_W]);

  // 누적 = 시드 + 확장 델타의 순수 파생(useMemo). 시드가 바뀌어도(depth 토글·entity 합류)
  // 확장 델타는 유지돼 누적에 더해진다(리셋 아님). slug 이동은 부모가 key로 remount.
  const [expansions, setExpansions] = useState<
    { nodes: KgGraphNode[]; edges: KgGraphEdge[]; origin: LayoutPoint }[]
  >([]);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // 드래그로 고정(pin)한 노드 좌표 — 레이아웃에 pinned로 전달된다(Neo4j pin 동작).
  const [pinned, setPinned] = useState<Map<string, LayoutPoint>>(() => new Map());
  // 드래그 진행 중 라이브 좌표 — 레이아웃 재계산 없이 그 노드/엣지만 따라온다.
  const [liveDrag, setLiveDrag] = useState<{ id: string; point: LayoutPoint } | null>(null);
  const [view, setView] = useState<ViewTransform>(VIEW_DEFAULT);

  // 도킹↔시트 모드 — 클라이언트 전용 lazy initial state로 최초 렌더에 확정(자가유발 CLS 방지).
  // GraphExplorerPanel은 dynamic(ssr:false)라 hydration mismatch가 성립하지 않으므로 window를
  // 최초 렌더에 읽어도 안전하다. 이후 resize는 debounce로만 반영.
  const [dockMode, setDockMode] = useState(
    () => typeof window !== "undefined" && window.innerWidth >= INSPECTOR_DOCK_WIDTH,
  );
  const sheetMode = !dockMode;
  useEffect(() => {
    let t: number | undefined;
    const onResize = () => {
      window.clearTimeout(t);
      t = window.setTimeout(
        () => setDockMode(window.innerWidth >= INSPECTOR_DOCK_WIDTH),
        150,
      );
    };
    window.addEventListener("resize", onResize);
    return () => {
      window.clearTimeout(t);
      window.removeEventListener("resize", onResize);
    };
  }, []);

  // 인스펙터 포커스/복귀 관리용 refs.
  const inspectorRef = useRef<HTMLDivElement | null>(null);
  const graphWrapRef = useRef<HTMLDivElement | null>(null); // 폰 시트 오픈 시 1회 scrollIntoView 앵커
  const lastNodeElRef = useRef<SVGGElement | null>(null); // 마지막 선택 노드 <g>(닫기 시 포커스 복귀)
  const prevActiveRef = useRef<string | null>(null); // 시트 scrollIntoView: 오픈 전이(null→값)만 발화
  const sheetHandleStartY = useRef<number | null>(null); // 폰 시트 grab-handle 스와이프다운 시작 Y

  const graph = useMemo(() => {
    let st = mergeGraph(emptyGraphState(), seedNodes, seedEdges).next;
    for (const ex of expansions) st = mergeGraph(st, ex.nodes, ex.edges, ex.origin).next;
    return st;
  }, [seedNodes, seedEdges, expansions]);

  // 렌더 dedup — 기존 패널과 동일하게 id first-wins(엣지 끝점이 id 공간이라 렌더 신원은 id).
  const nodesById = useMemo(() => {
    const m = new Map<string, KgGraphNode>();
    for (const n of graph.nodesByKey.values()) if (!m.has(n.id)) m.set(n.id, n);
    return m;
  }, [graph.nodesByKey]);

  const drawEdges = useMemo(
    () =>
      [...graph.edgesByKey.values()]
        .filter((e) => e.src !== e.dst && nodesById.has(e.src) && nodesById.has(e.dst))
        // 겹친 선 중 우선순위 높은 rel(충돌 등)을 나중에 그려 위로 올린다(K5.1 동일).
        .sort((a, b) => relRank(b.rel) - relRank(a.rel)),
    [graph.edgesByKey, nodesById],
  );

  // CONFLICTS_WITH 상태 + 충돌 여부(공용 nodeConflictSets). stroke는 상태 전용이라 종류 색(구
  // colorRelById)은 stroke에 싣지 않는다 — fill=종류로 이동. **충돌 warn 판정은 conflictIds
  // (인접 CONFLICTS_WITH 존재)로**, status property 유무가 아니다(status-less 충돌도 warn 유지, K8).
  const { status: conflictStatus, conflictIds } = useMemo(
    () => nodeConflictSets(drawEdges),
    [drawEdges],
  );

  // 인스펙터 충돌 표기 — 선택 노드 1개만 계산한다(전-노드 Map 미생성). "충돌" 판정은 인접
  // CONFLICTS_WITH 존재(status 유무 아님)이며 병합은 칩과 동일 규칙(`conflictMetaForNode`→
  // `mergeConflictMeta`). 노드 stroke/placeholder readout은 아래 `conflictStatus`(status-property
  // 키)를 계속 쓴다(PR 1은 색 미변경). **의도된 임시 이중 출처**: status 없는 CONFLICTS_WITH에서
  // 두 파생이 갈릴 수 있으나 stroke는 colorRel 경로로도 warn을 그려 시각적으로 수렴한다. **PR 2(색
  // 재설계)에서 stroke를 conflict meta 단일 출처로 이전**하고 `conflictStatus`를 제거할 것.
  const activeConflict = useMemo(
    () => (activeId == null ? null : conflictMetaForNode(drawEdges, activeId)),
    [activeId, drawEdges],
  );

  // 선택 노드의 인접 관계 — rel별 그룹핑 + 상대 dedup + 캡(그룹 5 / 전체 20, 허브의 시트 스크롤
  // 벽 방지) + 히어로 요약용 distinct 상대 수를 **한 번의 drawEdges 스캔**으로 함께 구한다.
  const { groups: inspectorRelations, connectedCount: inspectorConnectedCount } = useMemo(() => {
    if (activeId == null) return { groups: [] as InspectorRelationGroup[], connectedCount: 0 };
    const byRel = new Map<string, Map<string, KgGraphNode>>();
    const connected = new Set<string>();
    for (const e of drawEdges) {
      const otherId = e.src === activeId ? e.dst : e.dst === activeId ? e.src : null;
      if (otherId == null) continue;
      const other = nodesById.get(otherId);
      if (!other) continue;
      connected.add(otherId); // 캡 무관 전체 distinct 상대(히어로 요약)
      let bucket = byRel.get(e.rel);
      if (!bucket) {
        bucket = new Map();
        byRel.set(e.rel, bucket);
      }
      if (!bucket.has(otherId)) bucket.set(otherId, other);
    }
    // cap/order는 칩과 동일 공용 규칙(capOrderedRelGroups) 사용.
    const allByRel = new Map<string, KgGraphNode[]>();
    for (const [rel, bucket] of byRel) allByRel.set(rel, [...bucket.values()]);
    const { groups } = capOrderedRelGroups(
      allByRel,
      (rel, all, shown): InspectorRelationGroup => ({ rel, total: all.length, shown }),
    );
    return { groups, connectedCount: connected.size };
  }, [activeId, drawEdges, nodesById]);

  // 레이아웃(순수 함수 경계) — seed는 스폰 좌표(확장 부모 근처, 노드당 고정)라 같은 누적
  // 집합이면 항상 같은 좌표가 나온다(결정론). 위치 전이는 useAnimatedPositions가 보간.
  const targetPositions = useMemo(() => {
    const layoutNodes: LayoutNodeInput[] = [];
    for (const n of nodesById.values()) {
      const pin =
        pinned.get(n.id) ?? (n.id === anchorNodeId ? { x: 0, y: 0 } : undefined);
      layoutNodes.push({ key: n.id, pinned: pin, seed: graph.spawnSeeds.get(n.id) });
    }
    const layoutEdges: LayoutEdgeInput[] = drawEdges.map((e) => ({
      sourceKey: e.src,
      targetKey: e.dst,
    }));
    return computeForceLayout(layoutNodes, layoutEdges);
  }, [nodesById, drawEdges, pinned, anchorNodeId, graph.spawnSeeds]);

  const animated = useAnimatedPositions(targetPositions, !reducedMotion);
  const positionOf = useCallback(
    (id: string): LayoutPoint | null => {
      if (liveDrag && liveDrag.id === id) return liveDrag.point;
      return animated.get(id) ?? targetPositions.get(id) ?? null;
    },
    [liveDrag, animated, targetPositions],
  );

  // 사이드바發 재중심화 — 대상 world 좌표를 화면 중앙(시트면 가시 상단 밴드)으로 이동. 좌표는
  // **targetPositions(정착 좌표) 우선**으로 "정착 후 드리프트"를 막고, null/NaN이면 무동작
  // (`translate(NaN NaN)`은 throw가 아니라 silent 렌더 실패라 그래프가 조용히 사라진다).
  const recenterOn = useCallback(
    (id: string) => {
      const p = targetPositions.get(id) ?? positionOf(id);
      if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) return;
      setView((v) => {
        const cyTarget = sheetMode ? VIEW_H * 0.25 : VIEW_H / 2;
        const tx = -p.x * v.k;
        const ty = cyTarget - VIEW_H / 2 - p.y * v.k;
        if (!Number.isFinite(tx) || !Number.isFinite(ty)) return v;
        return { ...v, tx, ty };
      });
    },
    [targetPositions, positionOf, sheetMode, VIEW_H],
  );

  // 닫기 — 포커스가 인스펙터 내부에 있으면 마지막 선택 노드 <g>로 복귀(WCAG 2.4.3), 아니면 유지.
  const closeInspector = useCallback(() => {
    const inInspector = inspectorRef.current?.contains(document.activeElement) ?? false;
    setActiveId(null);
    if (inInspector) lastNodeElRef.current?.focus?.({ preventScroll: true });
  }, []);

  // 키보드 선택·사이드바 관계 순회 후 인스펙터 리전으로 포커스를 옮긴다(도킹·시트 **모두**). rAF로
  // 리렌더 커밋 이후에 실행해 activeId 변화 여부와 무관하게 동작한다(setActiveId no-op bailout에도
  // 안전 — 고착 플래그 불필요). 이 두 경로에서만 호출한다: 포인터로 노드를 탭해 여는 경우
  // (onNodePointerUp)는 호출하지 않아 노드 <g> 포커스를 유지하고 폰 탭-오픈에서 포커스를 뺏지 않는다.
  // 시트에서도 호출하는 이유: 관계 버튼은 리렌더로 언마운트되므로 도킹이든 시트든 포커스가 body로
  // 유실된다(760~1099 키보드 밴드 포함) — 리전으로 옮겨 WCAG 2.4.3을 지킨다.
  const focusInspectorSoon = useCallback(() => {
    requestAnimationFrame(() => inspectorRef.current?.focus?.({ preventScroll: true }));
  }, []);

  // 사이드바 관계 링크에서 선택 — 선택 + 재중심화(그래프 순회). 관계 버튼은 리렌더로 언마운트
  // 되므로 포커스를 인스펙터 리전으로 옮겨 body로 유실되는 것을 막는다(WCAG 2.4.3).
  const selectFromInspector = useCallback(
    (id: string) => {
      setActiveId(id);
      recenterOn(id);
      focusInspectorSoon();
    },
    [recenterOn, focusInspectorSoon],
  );

  /* --- 좌표 변환: client(px) → svg viewBox → world --- */
  const clientToSvg = useCallback((clientX: number, clientY: number): LayoutPoint => {
    const el = svgRef.current;
    if (!el) return { x: 0, y: 0 };
    const rect = el.getBoundingClientRect();
    const scale = rect.width > 0 ? VIEW_W / rect.width : 1;
    return { x: (clientX - rect.left) * scale, y: (clientY - rect.top) * scale };
  }, [VIEW_W]);
  const svgToWorld = useCallback(
    (p: LayoutPoint, v: ViewTransform): LayoutPoint => ({
      x: (p.x - VIEW_W / 2 - v.tx) / v.k,
      y: (p.y - VIEW_H / 2 - v.ty) / v.k,
    }),
    [VIEW_W, VIEW_H],
  );

  /* --- 확장 --- */
  const expandNode = useCallback(
    async (node: KgGraphNode) => {
      if (pendingId != null) return;
      if (!node.materialized) {
        setNotice(EXPLORER_COPY.placeholderNode);
        return;
      }
      if (expandedIds.has(node.id)) return; // 재요청 방지
      if (nodesById.size >= MAX_NODES) {
        setNotice(EXPLORER_COPY.capBlocked);
        return;
      }
      setPendingId(node.id);
      try {
        const res = await api.expandWikiGraphNode(node.label, node.id);
        if (!res.supported) {
          setNotice(EXPLORER_COPY.expandUnavailable);
          return;
        }
        const origin = positionOf(node.id) ?? { x: 0, y: 0 };
        // added/capped 계산용 병합은 미리 돌려 결과만 알림에 쓰고, 실제 누적은 델타로 쌓아
        // useMemo 파생(graph)이 다시 계산한다(같은 순수 함수라 결과 동일).
        const { added, capped } = mergeGraph(graph, res.nodes, res.edges, origin);
        setExpansions((prev) => [...prev, { nodes: res.nodes, edges: res.edges, origin }]);
        setExpandedIds((prev) => new Set(prev).add(node.id));
        setNotice(
          capped
            ? EXPLORER_COPY.capBlocked
            : added === 0
              ? EXPLORER_COPY.expandNone
              : `${added}개 노드 추가됨${res.truncated ? EXPLORER_COPY.serverTruncated : ""}`,
        );
      } catch (err) {
        setNotice(
          err instanceof ApiError && err.status === 404
            ? EXPLORER_COPY.expandNotFound
            : EXPLORER_COPY.expandUnavailable,
        );
      } finally {
        setPendingId(null);
      }
    },
    [pendingId, expandedIds, nodesById, positionOf, graph],
  );

  /* --- 노드 포인터: 클릭=선택 · 드래그=pin 이동 · 더블클릭/더블탭=확장 --- */
  const nodeGesture = useRef<{
    id: string;
    pointerId: number;
    startSvg: LayoutPoint;
    startWorld: LayoutPoint;
    dragging: boolean;
  } | null>(null);

  const onNodePointerDown = useCallback(
    (node: KgGraphNode) => (ev: ReactPointerEvent<SVGGElement>) => {
      // stopPropagation은 **early-return보다 먼저** — 비주요 버튼(우클릭/휠클릭)도 svg 배경
      // 핸들러로 버블하지 않게 한다. 아니면 그 pointerId가 bgPointers에 등록돼 pointerup에서
      // 배경 클릭-닫기(mouse+pan+!moved)가 방금 연 인스펙터를 즉시 닫는다.
      ev.stopPropagation();
      if (ev.button !== 0 && ev.pointerType === "mouse") return;
      const p = positionOf(node.id);
      if (!p) return;
      safeCapturePointer(ev.currentTarget, ev.pointerId);
      nodeGesture.current = {
        id: node.id,
        pointerId: ev.pointerId,
        startSvg: clientToSvg(ev.clientX, ev.clientY),
        startWorld: p,
        dragging: false,
      };
    },
    [positionOf, clientToSvg],
  );

  const onNodePointerMove = useCallback(
    (ev: ReactPointerEvent<SVGGElement>) => {
      const g = nodeGesture.current;
      if (!g || g.pointerId !== ev.pointerId) return;
      const now = clientToSvg(ev.clientX, ev.clientY);
      const dx = now.x - g.startSvg.x;
      const dy = now.y - g.startSvg.y;
      if (!g.dragging && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
      g.dragging = true;
      setLiveDrag({
        id: g.id,
        point: { x: g.startWorld.x + dx / view.k, y: g.startWorld.y + dy / view.k },
      });
    },
    [clientToSvg, view.k],
  );

  const onNodePointerUp = useCallback(
    (node: KgGraphNode) => (ev: ReactPointerEvent<SVGGElement>) => {
      const g = nodeGesture.current;
      if (!g || g.pointerId !== ev.pointerId) return;
      nodeGesture.current = null;
      if (g.dragging) {
        // 드래그 종료 = 그 자리에 pin(레이아웃에 pinned로 반영, Neo4j pin과 동일).
        const now = clientToSvg(ev.clientX, ev.clientY);
        const point = {
          x: g.startWorld.x + (now.x - g.startSvg.x) / view.k,
          y: g.startWorld.y + (now.y - g.startSvg.y) / view.k,
        };
        setPinned((prev) => new Map(prev).set(g.id, point));
        setLiveDrag(null);
        return;
      }
      // 탭/클릭 — 선택(인스펙터 열기). 폰 더블탭-확장은 폐기(택2): 시트 재중심화가 뷰를 점프시켜
      // 둘째 탭이 빗나가 확장이 유실되므로, 확장은 시트 '이웃 펼치기' 단일 경로로 통일한다.
      // (데스크톱 더블클릭 확장은 onDoubleClick으로 유지.)
      setActiveId(node.id);
      lastNodeElRef.current = ev.currentTarget;
      // 폰 시트: 선택 노드가 시트 뒤로 들어가지 않도록 가시 상단 밴드로 재중심화.
      if (sheetMode) recenterOn(node.id);
    },
    [clientToSvg, view.k, sheetMode, recenterOn],
  );

  const onNodeKeyDown = useCallback(
    (node: KgGraphNode) => (ev: ReactKeyboardEvent<SVGGElement>) => {
      // Shift+Enter = 직접 확장(마우스 더블클릭 1제스처 대등). Enter/Space = 선택(인스펙터 열기).
      if (ev.key === "Enter" && ev.shiftKey) {
        ev.preventDefault();
        void expandNode(node);
        return;
      }
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        setActiveId(node.id);
        lastNodeElRef.current = ev.currentTarget;
        focusInspectorSoon(); // 키보드 선택 → 인스펙터로 포커스 이동(도킹)
      }
    },
    [expandNode, focusInspectorSoon],
  );

  /* --- 배경 pan + 핀치 zoom --- */
  const bgPointers = useRef<Map<number, LayoutPoint>>(new Map());
  const bgGesture = useRef<
    | { kind: "pan"; startSvg: LayoutPoint; startView: ViewTransform; moved: boolean }
    | { kind: "pinch"; startDist: number; startMid: LayoutPoint; startView: ViewTransform }
    | null
  >(null);

  const onBgPointerDown = useCallback(
    (ev: ReactPointerEvent<SVGSVGElement>) => {
      const svgPt = clientToSvg(ev.clientX, ev.clientY);
      bgPointers.current.set(ev.pointerId, svgPt);
      if (svgRef.current) safeCapturePointer(svgRef.current, ev.pointerId);
      const pts = [...bgPointers.current.values()];
      if (pts.length === 2) {
        bgGesture.current = {
          kind: "pinch",
          startDist: Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y),
          startMid: { x: (pts[0].x + pts[1].x) / 2, y: (pts[0].y + pts[1].y) / 2 },
          startView: view,
        };
      } else if (pts.length === 1) {
        bgGesture.current = { kind: "pan", startSvg: svgPt, startView: view, moved: false };
      }
    },
    [clientToSvg, view],
  );

  const onBgPointerMove = useCallback(
    (ev: ReactPointerEvent<SVGSVGElement>) => {
      if (!bgPointers.current.has(ev.pointerId)) return;
      bgPointers.current.set(ev.pointerId, clientToSvg(ev.clientX, ev.clientY));
      const g = bgGesture.current;
      if (!g) return;
      const pts = [...bgPointers.current.values()];
      if (g.kind === "pan" && pts.length === 1) {
        const dx = pts[0].x - g.startSvg.x;
        const dy = pts[0].y - g.startSvg.y;
        if (Math.hypot(dx, dy) >= DRAG_THRESHOLD) g.moved = true; // 배경 클릭-닫기 판정용 팬 래치
        setView({ ...g.startView, tx: g.startView.tx + dx, ty: g.startView.ty + dy });
      } else if (g.kind === "pinch" && pts.length === 2) {
        const dist = Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y);
        const ratio = g.startDist > 0 ? dist / g.startDist : 1;
        const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, g.startView.k * ratio));
        // 핀치 시작 midpoint의 world 지점이 화면상 제자리에 있도록 tx/ty 보정.
        const world = svgToWorld(g.startMid, g.startView);
        setView({
          k,
          tx: g.startMid.x - VIEW_W / 2 - world.x * k,
          ty: g.startMid.y - VIEW_H / 2 - world.y * k,
        });
      }
    },
    [clientToSvg, svgToWorld, VIEW_W, VIEW_H],
  );

  const onBgPointerUp = useCallback(
    (ev: ReactPointerEvent<SVGSVGElement>) => {
      // 노드 pointerUp이 svg로 버블한 경우(bgPointers에 없음)엔 아무것도 하지 않는다 — 이 가드가
      // 없으면 방금 선택으로 연 인스펙터를 배경 닫기 규칙이 즉시 setActiveId(null)로 닫는 확정 버그.
      if (!bgPointers.current.has(ev.pointerId)) return;
      bgPointers.current.delete(ev.pointerId);
      if (bgPointers.current.size !== 0) return;
      const g = bgGesture.current;
      bgGesture.current = null;
      // 배경 클릭 닫기 = 마우스 단일 포인터 + 순수 클릭(팬 아님)만. 터치/펜은 pointerType로 제외
      // (폰 하단 시트 배경 탭 닫기 제외). 팬(moved)은 닫지 않는다.
      if (ev.pointerType === "mouse" && g?.kind === "pan" && !g.moved) {
        closeInspector();
      }
    },
    [closeInspector],
  );

  // pointercancel은 닫기 판정에서 제외(취소된 시퀀스) — 정리만.
  const onBgPointerCancel = useCallback((ev: ReactPointerEvent<SVGSVGElement>) => {
    bgPointers.current.delete(ev.pointerId);
    if (bgPointers.current.size === 0) bgGesture.current = null;
  }, []);

  // 휠 zoom — React onWheel은 passive라 preventDefault가 안 먹는다 → native 리스너.
  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const svgPt = clientToSvg(ev.clientX, ev.clientY);
      setView((v) => {
        const k = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.k * Math.pow(1.0015, -ev.deltaY)));
        const world = svgToWorld(svgPt, v);
        return {
          k,
          tx: svgPt.x - VIEW_W / 2 - world.x * k,
          ty: svgPt.y - VIEW_H / 2 - world.y * k,
        };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [clientToSvg, svgToWorld, VIEW_W, VIEW_H]);

  const zoomBy = useCallback((factor: number) => {
    setView((v) => ({ ...v, k: Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.k * factor)) }));
  }, []);

  // 그래프 리셋 — 확장 델타/pin/뷰를 버리고 현재 시드(앵커 스냅샷)로 복귀.
  const resetGraph = useCallback(() => {
    setExpansions([]);
    setExpandedIds(new Set());
    setPinned(new Map());
    setLiveDrag(null);
    setView(VIEW_DEFAULT);
    setActiveId(null);
    setNotice("그래프를 초기 상태로 되돌렸습니다.");
  }, []);

  // ESC 닫기 — 인스펙터 열림 동안만 document keydown 리스너. deps를 open 불리언으로 게이트해
  // 관계 순회로 activeId가 바뀌어도(열린 상태 유지) 리스너를 재구독하지 않는다.
  const inspectorOpen = activeId != null;
  useEffect(() => {
    if (!inspectorOpen) return;
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") closeInspector();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [inspectorOpen, closeInspector]);

  // 폰 시트 오픈 시 1회 그래프를 뷰포트 상단에 앵커(닫힘→오픈 전이만; 관계 순회로 activeId가
  // 바뀌어도 재발화 안 함). fixed 50dvh 시트가 스크롤 오프셋과 무관하게 그래프 아래를 덮는
  // "보이지 않는 오픈"을 막는다.
  useEffect(() => {
    const wasOpen = prevActiveRef.current != null;
    prevActiveRef.current = activeId;
    if (sheetMode && activeId != null && !wasOpen) {
      graphWrapRef.current?.scrollIntoView({
        block: "start",
        behavior: reducedMotion ? "auto" : "smooth",
      });
    }
  }, [activeId, sheetMode, reducedMotion]);

  /* --- readout / 범례 --- */
  const activeNode = activeId != null ? nodesById.get(activeId) ?? null : null;
  const activeText = activeNode
    ? isEntityNode(activeNode)
      ? entityReadout(activeNode)
      : conflictStatus.get(activeNode.id) != null && !activeNode.materialized
        ? `${CONFLICT_PLACEHOLDER_LABEL} · ${graphChipText(activeNode)}`
        : graphChipText(activeNode)
    : null;
  const activePageHref = activeNode ? wikiNodeHref(activeNode) : null;
  const activeCanExpand = activeNode
    ? activeNode.materialized && !expandedIds.has(activeNode.id)
    : false;
  const activePinned = activeId != null && pinned.has(activeId);

  // 인스펙터에 넘길 액션 콜백 — memo(GraphNodeInspector)가 팬/줌 프레임에 리렌더를 건너뛰도록
  // 안정화한다(팬 중 activeNode/activeId 참조가 그대로라 콜백 신원도 유지된다).
  const onExpandActive = useCallback(() => {
    if (activeNode) void expandNode(activeNode);
  }, [activeNode, expandNode]);
  const onOpenActivePage = useCallback(() => {
    if (!activeNode) return;
    if (onNodeNavigate && activeNode.slug) onNodeNavigate(activeNode.slug);
    else if (activePageHref) window.location.assign(activePageHref);
  }, [activeNode, onNodeNavigate, activePageHref]);
  const onUnpinActive = useCallback(() => {
    setPinned((prev) => {
      if (activeId == null) return prev;
      const m = new Map(prev);
      m.delete(activeId);
      return m;
    });
  }, [activeId]);

  const relsPresent = useMemo(() => orderRels(drawEdges.map((e) => e.rel)), [drawEdges]);
  // 범례 표식 — 등장한 것만(공용 규칙). 종류는 앵커 제외(§7). 충돌 warn/pass는 conflictIds
  // (인접 CONFLICTS_WITH) + status로, 개인/미생성/앵커는 실제 존재 여부로 게이트한다.
  const labelsPresent = useMemo(
    () => presentNodeLabels(nodesById.values(), anchorNodeId),
    [nodesById, anchorNodeId],
  );
  const hasPersonal = useMemo(
    () => [...nodesById.values()].some(isOwnPersonalNode),
    [nodesById],
  );
  const hasPlaceholder = useMemo(
    () => [...nodesById.values()].some((n) => !n.materialized),
    [nodesById],
  );
  const hasAnchor = anchorNodeId != null && nodesById.has(anchorNodeId);
  // 앵커는 stroke가 항상 strong이라 충돌 warn/pass를 시각적으로 못 나타낸다(nodeStrokeColor
  // anchor 우선). 범례가 없는 상태를 광고하지 않도록 충돌 flag에서 앵커를 제외한다(ring이
  // currentNodeId를 제외하는 것과 동일 규칙 — 두 패널 일치).
  const hasResolvedConflict = useMemo(
    () =>
      [...conflictIds].some(
        (id) => id !== anchorNodeId && conflictStatus.get(id) === "RESOLVED",
      ),
    [conflictIds, conflictStatus, anchorNodeId],
  );
  const hasConflict = useMemo(
    () =>
      [...conflictIds].some(
        (id) => id !== anchorNodeId && conflictStatus.get(id) !== "RESOLVED",
      ),
    [conflictIds, conflictStatus, anchorNodeId],
  );
  const expandedCount = nodesById.size;

  if (nodesById.size === 0) return null;

  const highlightId = focusedId ?? activeId;

  // 인스펙터 본문 — 도킹/시트 중 활성 래퍼에서만 렌더되므로 mode는 dockMode로 확정된다.
  const inspectorContent = activeNode ? (
    <GraphNodeInspector
      node={activeNode}
      conflict={activeConflict}
      relations={inspectorRelations}
      connectedCount={inspectorConnectedCount}
      canExpand={activeCanExpand}
      pinned={activePinned}
      pageHref={activePageHref}
      mode={dockMode ? "dock" : "sheet"}
      onSelectNode={selectFromInspector}
      onExpand={onExpandActive}
      onOpenPage={onOpenActivePage}
      onUnpin={onUnpinActive}
      onClose={closeInspector}
    />
  ) : null;

  return (
    <div>
      {/* 범례(종류/관계/상태) + 노드 카운트 — 폰(compact) 접기는 공용 GraphLegend가 처리. */}
      <div className="mb-2 flex items-start justify-between gap-x-3 px-1">
        <GraphLegend
          labels={labelsPresent}
          rels={relsPresent}
          compact={compact}
          hasAnchor={hasAnchor}
          hasPlaceholder={hasPlaceholder}
          hasConflict={hasConflict}
          hasResolvedConflict={hasResolvedConflict}
          hasPersonal={hasPersonal}
        />
        <span
          className="shrink-0 text-[var(--text-meta)]"
          style={{ color: "var(--color-text-muted)" }}
        >
          노드 {expandedCount} / {MAX_NODES}
        </span>
      </div>

      {/* 발견성(모바일) — 시트 모드에서 아직 선택 전이면 탭 가능 신호. 데스크톱 빈 도킹 컬럼은
          EmptyInspectorHint가 담당. */}
      {sheetMode && activeId == null ? (
        <p className="mb-2 px-1 text-[var(--text-meta)]" style={{ color: "var(--color-text-muted)" }}>
          노드를 탭해 정보 보기
        </p>
      ) : null}

      {/* 그래프(flex-1) + 인스펙터 도킹 컬럼(밖 형제). 인스펙터 열림/닫힘이 그래프 렌더 폭·노드
          화면좌표를 바꾸지 않도록 도킹 컬럼은 항상 예약(내부 콘텐츠만 activeId로 토글). */}
      <div ref={graphWrapRef} className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div
            className="relative mx-auto overflow-hidden rounded-[var(--radius-toolbar)]"
            style={{
              border: "1px solid var(--color-toolbar-border)",
              maxWidth: compact ? "100%" : (maxWidthPx ?? DEFAULT_MAX_WIDTH_PX),
            }}
          >
        <svg
          ref={svgRef}
          className="block h-auto w-full"
          style={{ touchAction: "none", cursor: "grab" }}
          viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
          role="img"
          aria-label={`지식 그래프 탐색기: 노드 ${expandedCount}개, 관계 ${drawEdges.length}개. 노드를 더블클릭하면 이웃이 펼쳐집니다.`}
          onPointerDown={onBgPointerDown}
          onPointerMove={onBgPointerMove}
          onPointerUp={onBgPointerUp}
          onPointerCancel={onBgPointerCancel}
        >
          <g
            transform={`translate(${VIEW_W / 2 + view.tx} ${VIEW_H / 2 + view.ty}) scale(${view.k})`}
          >
            {/* 엣지 — 노드 아래, 비인터랙티브 */}
            <g aria-hidden pointerEvents="none">
              {drawEdges.map((e) => {
                const s = positionOf(e.src);
                const d = positionOf(e.dst);
                if (!s || !d) return null;
                const sNode = nodesById.get(e.src);
                const dNode = nodesById.get(e.dst);
                const active =
                  highlightId != null && (e.src === highlightId || e.dst === highlightId);
                const dim = highlightId != null && !active;
                return (
                  <line
                    key={edgeKey(e)}
                    x1={s.x}
                    y1={s.y}
                    x2={d.x}
                    y2={d.y}
                    stroke={relColor(e.rel)}
                    strokeWidth={(active ? 2 : 1) / view.k}
                    strokeOpacity={dim ? 0.15 : active ? 0.9 : 0.4}
                    strokeDasharray={
                      sNode?.materialized && dNode?.materialized ? undefined : "3 2"
                    }
                  />
                );
              })}
            </g>

            {/* 노드 */}
            {[...nodesById.values()].map((node) => {
              const p = positionOf(node.id);
              if (!p) return null;
              return (
                <ExplorerNode
                  key={node.id}
                  node={node}
                  x={p.x}
                  y={p.y}
                  zoom={view.k}
                  hitRadius={TAP_TARGET_PX / 2 / Math.max(renderRatio, 0.2)}
                  isAnchor={node.id === anchorNodeId}
                  status={conflictStatus.get(node.id)}
                  inConflict={conflictIds.has(node.id)}
                  expanded={expandedIds.has(node.id)}
                  pending={pendingId === node.id}
                  dim={highlightId != null && highlightId !== node.id}
                  reducedMotion={reducedMotion}
                  onPointerDown={onNodePointerDown}
                  onPointerMove={onNodePointerMove}
                  onPointerUp={onNodePointerUp}
                  onExpand={expandNode}
                  onKeyDown={onNodeKeyDown}
                  onFocusChange={setFocusedId}
                />
              );
            })}
          </g>
        </svg>

        {/* zoom/리셋 컨트롤 — 터치(핀치 불가 환경)와 키보드 대응 */}
        <div className="absolute right-2 top-2 flex flex-col gap-1">
          {(
            [
              { label: "확대", text: "+", act: () => zoomBy(1.25) },
              { label: "축소", text: "−", act: () => zoomBy(0.8) },
            ] as const
          ).map((b) => (
            <button
              key={b.label}
              aria-label={b.label}
              className="flex h-11 w-11 items-center justify-center rounded-[var(--radius-toolbar)] text-[var(--text-body)] font-bold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
              onClick={b.act}
              style={{
                background: "var(--color-card)",
                border: "1px solid var(--color-toolbar-border)",
                boxShadow: "var(--shadow-toolbar)",
                color: "var(--color-text-strong)",
              }}
              type="button"
            >
              {b.text}
            </button>
          ))}
        </div>
        <div className="absolute left-2 top-2">
          <button
            className="flex min-h-11 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
            onClick={resetGraph}
            style={{
              background: "var(--color-card)",
              border: "1px solid var(--color-toolbar-border)",
              boxShadow: "var(--shadow-toolbar)",
              color: "var(--color-text-strong)",
            }}
            type="button"
          >
            리셋
          </button>
        </div>
          </div>
        </div>

        {/* 인스펙터 도킹 컬럼(데스크톱 ≥1100) — 항상 예약, 내부만 activeId로 토글(폭 불변식). */}
        {dockMode ? (
          <div
            className="flex shrink-0 flex-col overflow-hidden rounded-[var(--radius-toolbar)]"
            style={{
              width: INSPECTOR_WIDTH_PX,
              maxHeight: 560,
              border: "1px solid var(--color-toolbar-border)",
              background: "var(--color-card)",
            }}
          >
            {inspectorContent ? (
              <InspectorRegion
                regionRef={inspectorRef}
                resetKey={activeId}
                onClose={closeInspector}
              >
                {inspectorContent}
              </InspectorRegion>
            ) : (
              <EmptyInspectorHint />
            )}
          </div>
        ) : null}
      </div>

      {/* 폰/좁은 창 하단 시트(non-modal, scrim 없음) — 첫 탭 즉시 가시 피드백 + 상단 그래프 절반
          pass-through. 배경 탭 닫기 제외(닫기 버튼·grab-handle 스와이프다운·ESC만). */}
      {sheetMode && inspectorContent ? (
        <div
          className="fixed inset-x-0 bottom-0 z-40 mx-auto flex flex-col rounded-t-[16px]"
          style={{
            maxWidth: 520,
            maxHeight: "50dvh",
            background: "var(--color-card)",
            borderTop: "1px solid var(--color-toolbar-border)",
            boxShadow: "var(--shadow-toolbar)",
            paddingBottom: "var(--mobile-safe-bottom)",
          }}
        >
          {/* grab-handle — 스와이프다운 닫기는 여기서만 판정(내부 목록 스크롤을 닫기로 오판 방지). */}
          <div
            className="flex shrink-0 cursor-grab touch-none items-center justify-center py-2"
            onPointerDown={(ev) => {
              sheetHandleStartY.current = ev.clientY;
              safeCapturePointer(ev.currentTarget, ev.pointerId);
            }}
            onPointerUp={(ev) => {
              const start = sheetHandleStartY.current;
              sheetHandleStartY.current = null;
              if (start != null && ev.clientY - start > 40) closeInspector();
            }}
            onPointerCancel={() => {
              sheetHandleStartY.current = null;
            }}
          >
            <span
              aria-hidden
              className="h-1 w-10 rounded-full"
              style={{ background: "var(--color-divider)" }}
            />
          </div>
          <InspectorRegion regionRef={inspectorRef} resetKey={activeId} onClose={closeInspector}>
            {inspectorContent}
          </InspectorRegion>
        </div>
      ) : null}

      {/* 확장 결과 알림(aria-live) */}
      <p
        aria-live="polite"
        className="mt-1 min-h-[1.25rem] px-1 text-[var(--text-meta)]"
        style={{ color: "var(--color-text-muted)" }}
      >
        {notice ?? EXPLORER_COPY.hint}
      </p>
      {/* 선택 요약 — 시각-숨김 상시 마운트 aria-live(정보·CTA 단일 출처는 인스펙터). */}
      <p className="sr-only" aria-live="polite">
        {activeText ?? ""}
      </p>
    </div>
  );
}

// 인스펙터 리전 래퍼 — 도킹 컬럼과 폰 시트가 공유하는 단일 출처(ErrorBoundary + role=region +
// aria-label + focus 타깃 ref). a11y/포커스 계약을 한 곳에 모아 두 호스트가 갈리지 않게 한다.
function InspectorRegion({
  regionRef,
  resetKey,
  onClose,
  children,
}: {
  regionRef: RefObject<HTMLDivElement | null>;
  resetKey: unknown;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <ErrorBoundary
      resetKey={resetKey}
      fallback={<InspectorErrorFallback onClose={onClose} />}
      onError={(err) => console.error("inspector render failed", err)}
    >
      <div
        ref={regionRef}
        role="region"
        aria-label="노드 정보"
        tabIndex={-1}
        className="flex min-h-0 flex-1 flex-col outline-none"
      >
        {children}
      </div>
    </ErrorBoundary>
  );
}

// 인스펙터 전용 ErrorBoundary fallback — render-throw 시 사이드바만 강등하고 그래프 누적 상태
// (pan/zoom/pin/expand)는 보존한다(그래프 전체를 칩으로 강등하지 않음).
function InspectorErrorFallback({ onClose }: { onClose: () => void }) {
  return (
    <InspectorCenteredState>
      <p className="text-[var(--text-body-sm)]" style={{ color: "var(--color-text-muted)" }}>
        노드 정보를 표시할 수 없습니다.
      </p>
      <button
        className="inline-flex min-h-11 items-center rounded-[var(--radius-toolbar)] px-3 text-[var(--text-body-sm)] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-text-strong)]"
        onClick={onClose}
        style={SECONDARY_BUTTON_STYLE}
        type="button"
      >
        닫기
      </button>
    </InspectorCenteredState>
  );
}

// React.memo — 팬(setView tx/ty)은 부모 <g> transform만 바꾸므로 노드 props(x/y 월드좌표·zoom·
// colorRel·status…)는 그대로다. 콜백은 부모의 안정 useCallback(커링) + node로 넘겨 신원을 유지하므로
// 팬/줌 프레임마다 ≤200 노드가 리렌더/diff되지 않는다(zoom 프레임은 zoom prop이 바뀌어 정상 리렌더).
const ExplorerNode = memo(function ExplorerNode({
  node,
  x,
  y,
  zoom,
  hitRadius,
  isAnchor,
  status,
  inConflict,
  expanded,
  pending,
  dim,
  reducedMotion,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  onExpand,
  onKeyDown,
  onFocusChange,
}: {
  node: KgGraphNode;
  x: number;
  y: number;
  zoom: number;
  hitRadius: number; // world 단위(zoom 1) — 화면 px 보정 완료 값(TAP_TARGET_PX 참조)
  isAnchor: boolean;
  status?: string;
  inConflict: boolean;
  expanded: boolean;
  pending: boolean;
  dim: boolean;
  reducedMotion: boolean;
  // 부모의 안정 커링 핸들러 — ExplorerNode가 렌더 시 node로 바인딩한다(memo가 스킵하면 재바인딩 없음).
  onPointerDown: (node: KgGraphNode) => (ev: ReactPointerEvent<SVGGElement>) => void;
  onPointerMove: (ev: ReactPointerEvent<SVGGElement>) => void;
  onPointerUp: (node: KgGraphNode) => (ev: ReactPointerEvent<SVGGElement>) => void;
  onExpand: (node: KgGraphNode) => void;
  onKeyDown: (node: KgGraphNode) => (ev: ReactKeyboardEvent<SVGGElement>) => void;
  onFocusChange: (id: string | null) => void;
}) {
  const [focused, setFocused] = useState(false);
  const r = isAnchor ? 11 : 8;
  // fill = 종류(§Slice1 항목 2). 앵커(현재 페이지)는 §7 예외로 strong fill. placeholder(미생성)는
  // 채도를 낮춰(fillOpacity) materialized와 fill 강도로도 구분한다(점선 테두리 단일 신호 보강).
  const fill = isAnchor ? "var(--color-text-strong)" : nodeFillColor(node.label);
  const fillOpacity = isAnchor || node.materialized ? 1 : 0.4;
  // stroke = 상태 전용(종류/rel 미탑재) — 공용 nodeStrokeColor(앵커>충돌>divider). 충돌 warn 판정은
  // inConflict(인접 CONFLICTS_WITH)로, status 유무가 아니다(status-less 충돌도 warn 유지, K8).
  const stroke = nodeStrokeColor({ anchor: isAnchor, inConflict, status });
  const isPersonal = isOwnPersonalNode(node);
  const text = graphChipText(node);
  const canExpand = node.materialized && !expanded;
  // 종류(+미생성) 라벨은 인스펙터·칩과 공유(nodeKindLabel)해 표기가 뷰마다 어긋나지 않게 한다.
  const ariaLabel = `${nodeKindLabel(node)}${
    isPersonal ? `·${PERSONAL_NODE_LABEL}` : ""
  }: ${text}${
    !isAnchor && inConflict
      ? `, ${status === "RESOLVED" ? "충돌 해소" : status === "UNRESOLVED" ? "충돌 미해소" : "충돌"}`
      : ""
  }, Enter로 선택${
    canExpand ? ", Shift+Enter 또는 더블클릭으로 확장" : expanded ? " (확장됨)" : ""
  }`;

  return (
    <g
      role="button"
      aria-label={ariaLabel}
      tabIndex={0}
      style={{
        cursor: isClickableWikiNode(node) || canExpand ? "pointer" : "default",
        opacity: dim ? 0.35 : 1,
        ...(reducedMotion ? {} : { transition: "opacity 120ms" }),
        outline: "none",
      }}
      onPointerDown={onPointerDown(node)}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp(node)}
      onDoubleClick={() => void onExpand(node)}
      onKeyDown={onKeyDown(node)}
      onFocus={() => {
        setFocused(true);
        onFocusChange(node.id);
      }}
      onBlur={() => {
        setFocused(false);
        onFocusChange(null);
      }}
    >
      <title>{text}</title>
      {/* 투명 탭 타깃 */}
      <circle cx={x} cy={y} r={hitRadius / zoom} fill="transparent" />
      {/* 키보드 포커스 링 — SVG <g>엔 CSS outline이 안 보인다(WCAG 2.4.7). */}
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
      {/* K7.3 — 내 개인 메모 accent halo(rel-색 stroke 바깥 동심원). */}
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
      {/* 확장 로딩 스피너 — reduced-motion이면 정적 점선 링만. */}
      {pending ? (
        <circle
          cx={x}
          cy={y}
          r={r + 5.5}
          fill="none"
          stroke="var(--color-text-muted)"
          strokeWidth={1.5}
          strokeDasharray="6 4"
          pointerEvents="none"
        >
          {!reducedMotion ? (
            <animateTransform
              attributeName="transform"
              type="rotate"
              from={`0 ${x} ${y}`}
              to={`360 ${x} ${y}`}
              dur="1s"
              repeatCount="indefinite"
            />
          ) : null}
        </circle>
      ) : null}
      <circle
        cx={x}
        cy={y}
        r={r}
        fill={fill}
        fillOpacity={fillOpacity}
        stroke={stroke}
        strokeWidth={isAnchor ? 2.5 : 1.5}
        strokeDasharray={node.materialized ? undefined : "2 2"}
      />
      {/* 미확장 affordance — 오른쪽 위 작은 + 힌트(확장 가능 scent). */}
      {canExpand && !pending ? (
        <text
          x={x + r + 2}
          y={y - r + 2}
          fontSize={9}
          fontWeight={700}
          fill="var(--color-text-muted)"
          pointerEvents="none"
          aria-hidden
        >
          +
        </text>
      ) : null}
      <text
        x={x}
        y={y + r + 12}
        textAnchor="middle"
        fontSize={isAnchor ? 12 : 11}
        fontWeight={isAnchor ? 700 : 500}
        fill="var(--color-text-body)"
      >
        {truncate(text)}
      </text>
    </g>
  );
});
