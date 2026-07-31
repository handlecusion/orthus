/**
 * Shared helpers + copy for the read-only KG page graph.
 *
 * Extracted from web/src/app/wiki/[...slug]/page.tsx so both the chip-list view
 * (graph-chip-list.tsx) and the K5.1 SVG ring view (graph-ring-panel.tsx) — and
 * future K4b /ask path-view / K7 owner-scope panels — share one source of
 * truth. Consumes the K4 API shape only (WikiPageGraphResponse); no data
 * reshaping, no API calls. Logic here is pure (no JSX).
 */
import type { EntityTeaser, KgGraphEdge, KgGraphNode, WikiPageGraphResponse } from "@/lib/api";

// rel 그룹 표시·드롭 순서(앞이 보호, 뒤가 먼저 잘림). 미지 rel은 뒤에 원문으로 붙는다.
// K9(C-D) — entity rel(MENTIONED_IN/RELATES_TO)은 **맨 뒤**: CONFLICTS_WITH(warn)/SUPPORTS(pass)
// 시각 우선을 보호한다(흔한 엔티티 다리가 모순/근거를 밀어내지 않게).
export const REL_GROUP_ORDER = [
  "CONFLICTS_WITH",
  "SUPPORTS",
  "DERIVED_FROM",
  "BACKLINK",
  "EXTRACTED_FROM",
  "MENTIONED_IN",
  "RELATES_TO",
] as const;

export const REL_LABEL: Record<string, string> = {
  CONFLICTS_WITH: "충돌",
  SUPPORTS: "근거",
  DERIVED_FROM: "파생",
  BACKLINK: "백링크",
  EXTRACTED_FROM: "원본 추출",
  MENTIONED_IN: "언급", // K9 — entity→page 다리
  RELATES_TO: "연관", // K9 — entity↔entity co-mention
};

// 사용자용 한글 종류명(내부 타입명 직역 탈피). 색은 5그룹(NODE_COLOR_GROUP)으로
// 축약하지만 배지 텍스트는 6종을 유지한다 — 주장/소스는 텍스트로 구분되고 색만 공유.
export const NODE_LABEL: Record<string, string> = {
  WikiPage: "위키 문서",
  WikiClaim: "위키 주장",
  WikiSource: "위키 소스",
  Document: "원문 문서",
  StructuredFact: "표 데이터",
  Entity: "개체(인물·조직 등)", // K9 — 인물/조직/프로젝트/도구
};

// rel 표시 라벨(기지 rel은 한글, 미지 rel은 원문). 범례·인스펙터·칩이 같은 규칙을 쓴다.
export function relLabelText(rel: string): string {
  return REL_LABEL[rel] ?? rel;
}

// 노드 종류 배지 문자열(+ 미생성 접미사) 단일 출처 — 인스펙터/칩/aria가 같은 포맷을 쓰도록.
// materialized면 종류명만, 아니면 " · 미생성"을 붙인다(칩·인스펙터 시각 표기 일치).
export function nodeKindLabel(node: KgGraphNode): string {
  const base = NODE_LABEL[node.label] ?? node.label;
  return node.materialized ? base : `${base} · ${GRAPH_COPY.placeholder}`;
}

// 종류(라벨) → fill 색(6 label → 5 color). 노드 fill 전용의 단일 출처다 — stroke는
// 상태(anchor/충돌/개인 halo)만 나르고 종류를 싣지 않는다. personal halo가 blue
// (--color-accent) 고정이므로 personal이 될 수 있는 라벨엔 blue를 배정하지 않고,
// 항상 company-scope인 Entity에 blue를 예약한다. WikiClaim+WikiSource는 "지식 근거"로
// violet을 공유한다(텍스트는 NODE_LABEL로 구분).
export const NODE_COLOR_GROUP: Record<string, string> = {
  Entity: "var(--color-node-entity)", // blue(예약)
  WikiPage: "var(--color-node-wikipage)", // mint
  WikiClaim: "var(--color-node-knowledge)", // violet
  WikiSource: "var(--color-node-knowledge)", // violet(주장과 색 공유)
  Document: "var(--color-node-document)", // slate
  StructuredFact: "var(--color-node-structured)", // pink
};

// 노드 fill 색 단일 접근자. 미지 라벨(서버가 FE 맵보다 먼저 새 라벨 추가)은 **card(배경색)가
// 아니라** 보이는 중립색으로 폴백한다 — card fill + divider stroke는 배경에 묻혀 노드가 거의
// 안 보인다(코드리뷰). text-muted는 card 위에서 식별 가능한 중립 회색이다.
export function nodeFillColor(label: string): string {
  return NODE_COLOR_GROUP[label] ?? "var(--color-text-muted)";
}

// KG 그래프 노드의 stroke = **상태 전용**(종류/rel 미탑재) 단일 출처 — explorer/ring이 같은
// 규칙을 쓴다. 우선순위: 앵커/현재 노드(strong) > 충돌(해소=pass / 그 외=warn) > 중립 divider.
// **충돌 판정은 인접 CONFLICTS_WITH 존재(inConflict)이지 status property 유무가 아니다** —
// status 없는 충돌도 warn을 유지해 K8 "모순 노출" 신호가 사라지지 않게 한다(코드리뷰 K8 회귀).
export function nodeStrokeColor(opts: {
  anchor: boolean;
  inConflict: boolean;
  status?: string;
}): string {
  if (opts.anchor) return "var(--color-text-strong)";
  if (opts.inConflict) {
    return opts.status === "RESOLVED" ? "var(--color-pass-fg)" : "var(--color-warn-fg)";
  }
  return "var(--color-divider)";
}

// 노드별 충돌 상태 + 충돌 여부(인접 CONFLICTS_WITH 존재) 단일 출처. status는 UNRESOLVED-wins로
// 병합(있을 때만), conflictIds는 status 유무와 무관하게 인접 CONFLICTS_WITH가 있는 모든 노드다.
// stroke warn/pass·범례 표식이 이 집합에서 도출된다(status-less 충돌도 포함 — K8 회귀 차단).
export function nodeConflictSets(edges: KgGraphEdge[]): {
  status: Map<string, string>;
  conflictIds: Set<string>;
} {
  const status = new Map<string, string>();
  const conflictIds = new Set<string>();
  for (const e of edges) {
    if (e.rel !== "CONFLICTS_WITH") continue;
    if (e.src === e.dst) continue;
    const st = typeof e.properties?.status === "string" ? e.properties.status : undefined;
    for (const id of [e.src, e.dst]) {
      conflictIds.add(id);
      if (!st) continue;
      if (status.get(id) === "UNRESOLVED") continue;
      status.set(id, st === "UNRESOLVED" ? "UNRESOLVED" : st);
    }
  }
  return { status, conflictIds };
}

// 등장한 종류(라벨) 목록 — 제외 노드(앵커/현재, §7 예외라 종류색이 아님)를 빼고
// NODE_LABEL_PRIORITY 순으로 정렬. explorer/ring 범례가 같은 규칙을 쓰도록 단일 출처.
export function presentNodeLabels(
  nodes: Iterable<{ id: string; label: string }>,
  excludeId: string | null,
): string[] {
  const set = new Set<string>();
  for (const n of nodes) {
    if (n.id === excludeId) continue;
    set.add(n.label);
  }
  return [...set].sort(
    (a, b) => (NODE_LABEL_PRIORITY[a] ?? 99) - (NODE_LABEL_PRIORITY[b] ?? 99),
  );
}

// 정렬 tiebreak용 label 우선순위(낮을수록 먼저). 미지 label은 뒤로.
// K9(C-D) — Entity→5 명시: 없으면 graphChipSort tiebreak가 `?? 99`로 빠져 placeholder 뒤로
// 불안정 정렬된다.
export const NODE_LABEL_PRIORITY: Record<string, number> = {
  WikiPage: 0,
  WikiClaim: 1,
  WikiSource: 2,
  Document: 3,
  StructuredFact: 4,
  Entity: 5,
};

// rel → 의미 색의 단일 출처. 그래프 뷰(SVG 색)와 범례가 같은 색을 쓰도록 한 곳에서
// 정의한다 — 새 rel/색은 여기만 고친다. 충돌/근거는 의미 색(warn/pass), 나머지 지식
// rel은 범례 스와치가 서로 구분되도록 channel 토큰으로 분리한다(미지 rel만 muted).
export function relColor(rel: string): string {
  switch (rel) {
    case "CONFLICTS_WITH":
      return "var(--color-warn-fg)";
    case "SUPPORTS":
      return "var(--color-pass-fg)";
    case "DERIVED_FROM":
      return "var(--color-channel-violet)";
    case "BACKLINK":
      return "var(--color-channel-blue)";
    case "EXTRACTED_FROM":
      return "var(--color-channel-slate)";
    // K9(C-D) — entity rel: muted default·다른 rel과 구분되는 미사용 channel(slate/violet/blue
    // 점유). 범례 swatch가 서로/미지 rel과 구분되게.
    case "MENTIONED_IN":
      return "var(--color-channel-mint)";
    case "RELATES_TO":
      return "var(--color-channel-pink)";
    default:
      return "var(--color-text-muted)";
  }
}

// 칩 뷰용 같은 의미축의 Chip tone(충돌만 강조, 나머지는 중립).
export function relTone(rel: string): "warn" | "neutral" {
  return rel === "CONFLICTS_WITH" ? "warn" : "neutral";
}

// rel 표시 순서 단일 출처: REL_GROUP_ORDER 먼저, 미지 rel은 뒤에 원문으로.
// 범례(ring)·칩 그룹·색 우선순위가 같은 규칙을 쓰도록 buildGraphGroups와 패널이 공유한다.
export function orderRels(seen: Iterable<string>): string[] {
  const set = seen instanceof Set ? seen : new Set(seen);
  const known = REL_GROUP_ORDER.filter((r) => set.has(r));
  const unknown = [...set].filter(
    (r) => !REL_GROUP_ORDER.includes(r as (typeof REL_GROUP_ORDER)[number]),
  );
  return [...known, ...unknown];
}

// "클릭 가능한 위키 페이지 노드인가" + 그 href — 칩 뷰/그래프 뷰가 같은 판정·인코딩을
// 쓰도록 단일화(slug 경로 인코딩 포함).
export function isClickableWikiNode(node: KgGraphNode): boolean {
  return node.label === "WikiPage" && node.materialized && !!node.slug;
}

// K7.3 — "이 노드는 caller 본인의 personal 노드인가"의 단일 판정.
// **두 플래그를 AND**한다(`scope==='personal' && is_own_personal`): 게이트 tripwire가
// foreign personal을 이미 drop하지만, FE는 `scope` 단독을 신뢰하지 않는다(defense-in-depth).
// 칩/그래프 뷰가 personal↔company 구분 렌더를 같은 기준으로 하도록 단일화한다.
export function isOwnPersonalNode(node: KgGraphNode): boolean {
  return node.scope === "personal" && node.is_own_personal === true;
}

// personal 노드 시각 표식 라벨(aria/readout 접미사) 단일 출처.
export const PERSONAL_NODE_LABEL = "내 개인 메모";

// K9 — :Entity 노드 판정. 비클릭 매개 노드(다리)다 — isClickableWikiNode가 이미 false
// (label !== WikiPage)라 클릭/이동 대상이 아니다(C-B/U4 "클릭 불가" 단서의 판정 기준).
export function isEntityNode(node: KgGraphNode): boolean {
  return node.label === "Entity";
}

// K9(U4) — 신규 입사자 첫 등장 plain-language gloss(jargon 해소). 패널 첫 노출/legend 한 줄.
export const ENTITY_GLOSS = "개체 = 이 페이지들이 함께 언급한 같은 인물·프로젝트·도구";

// K9(C-F) — entity union 섹션의 단일 scope 카피(server entity_scope='company' 정합, U6/NH-7).
export const ENTITY_SCOPE_COPY = "회사 문서 기준 연결";

// K9(U4) — 비클릭 :Entity 노드 readout(사람 말투 + 다리 명시). name = graphChipText(=Entity의
// display_name, 함수 hoist). **뷰 무관 중립 문구**: 인스펙터(노드 인스펙터 PR)는 모든 노드에서
// 열리므로 "클릭 불가"는 틀린 단서다 — 여기선 "페이지 없음"(materialized WikiPage만 페이지 열기)만
// 밝히고, "정보만 표시"류 인스펙터 어포던스 문구는 인스펙터 컴포넌트 로컬 카피로 둔다(ring 공유 안전).
export function entityReadout(node: KgGraphNode): string {
  return `이 페이지들은 ‘${graphChipText(node)}’을(를) 함께 언급해서 묶였어요 (개체 · 페이지 없음)`;
}

// K9 — entity_kind 평문 라벨(인스펙터 속성용). null이면 null.
export function entityKindLabel(kind: string | null): string | null {
  if (!kind) return null;
  switch (kind) {
    case "person":
      return "인물";
    case "org":
      return "조직";
    case "project":
      return "프로젝트";
    case "system":
      return "도구";
    default:
      return kind;
  }
}

// K9(R1) — entity per-rel 표시 캡 분모: "shown / 전체 mention_count". 서버 truncated 불변,
// FE 표시 전용(graph-chip-list groupTotal 패턴). mention_count 없거나 shown 이하면 shown만.
export function entityMentionLabel(node: KgGraphNode, shown: number): string {
  const total = node.mention_count;
  return total != null && total > shown ? `${shown} / 전체 ${total}` : `${shown}`;
}

// K9.3b(U5) — /ask entity star 헤더의 **정직한 hub 개수**. hub면(예: "NOVA 47개 페이지) 단순
// re-list는 검색과 다를 바 없는 tautology라 개수를 정직히 표기한다("‘NOVA’ — 47개 페이지에 언급
// (상위 N)"). mention_count(page-only degree)가 분모, shown이 캡 후 표시 수. total<=shown이면
// "‘NOVA’을(를) 언급한 N개 페이지"로 단순 표기(희소 = 그대로 가치). graphChipText=엔티티 display_name.
export function entityStarHeading(node: KgGraphNode, shown: number): string {
  const name = graphChipText(node);
  const total = node.mention_count;
  if (total != null && total > shown) {
    return `‘${name}’ — ${total}개 페이지에 언급 (상위 ${shown})`;
  }
  return `‘${name}’을(를) 언급한 ${shown}개 페이지`;
}

// K9.2(D-TZ B) — 펼침-전 티저 한 줄. 비-person 엔티티명이 있으면 scent 표기, person-only/이름
// 없음은 page 수만(person 억제). "N개 페이지가 ‘Orbit·NOVA’ 공유" / "N개 페이지가 공유 개체로 엮임".
export function entityTeaserText(teaser: EntityTeaser): string {
  if (teaser.entity_names.length > 0) {
    return `${teaser.page_count}개 페이지가 ‘${teaser.entity_names.join("·")}’ 공유`;
  }
  return `${teaser.page_count}개 페이지가 공유 개체로 엮임`;
}

export function wikiSlugPath(slug: string): string {
  return slug.split("/").map(encodeURIComponent).join("/");
}

export function wikiNodeHref(node: KgGraphNode): string | null {
  return isClickableWikiNode(node) ? `/wiki/${wikiSlugPath(node.slug!)}` : null;
}

export const GRAPH_GROUP_CAP = 5;
export const GRAPH_TOTAL_CAP = 20;

// rel별 이미-정렬된 배열 Map을 REL_GROUP_ORDER 순 + 그룹당 5 / 전체 20 cap으로 자른다 —
// 노드 인스펙터(inspectorRelations)와 칩/링(buildGraphGroups)이 **같은 cap/order 규칙**을 쓰도록
// 단일 출처. `makeGroup(rel, all, shown)`으로 각 뷰의 그룹 shape를 만든다. truncated는 그룹당 cap
// 초과 또는 전체 cap 도달로 표시 못 한 항목이 남는지.
export function capOrderedRelGroups<T, G>(
  allByRel: Map<string, T[]>,
  makeGroup: (rel: string, all: T[], shown: T[]) => G,
): { groups: G[]; truncated: boolean } {
  const groups: G[] = [];
  let total = 0;
  let truncated = false;
  for (const rel of orderRels(allByRel.keys())) {
    const all = allByRel.get(rel);
    if (!all || all.length === 0) continue;
    if (total >= GRAPH_TOTAL_CAP) {
      truncated = true; // 표시 못 한 그룹이 남음
      break;
    }
    let shown = all.slice(0, GRAPH_GROUP_CAP);
    if (all.length > GRAPH_GROUP_CAP) truncated = true;
    if (total + shown.length > GRAPH_TOTAL_CAP) {
      shown = shown.slice(0, GRAPH_TOTAL_CAP - total);
      truncated = true;
    }
    total += shown.length;
    groups.push(makeGroup(rel, all, shown));
  }
  return { groups, truncated };
}

// 패널 UI 문구 — 칩/그래프/에러 뷰 공용 단일 출처(JSX 아님).
export const GRAPH_COPY = {
  notCompany: "이 페이지는 회사 그래프 대상이 아닙니다.",
  error: "지식 그래프를 불러오지 못했습니다.",
  disabled: "이 node에서는 지식 그래프를 지원하지 않습니다.",
  unavailable: "지식 그래프를 일시적으로 불러올 수 없습니다.",
  empty: "이 페이지와 연결된 관계가 아직 없습니다.",
  truncated: "일부 관계만 표시합니다.",
  placeholder: "미생성",
  // K9.2 — entity 다리 패널 상태 카피.
  entityExpand: "공유 개체로 엮인 페이지 보기", // U2 펼침 CTA
  entityCollapse: "개체 연결 접기",
  entityEmpty: "공유 개체로 엮인 다른 페이지 없음", // U3 calm 빈 상태(에러/스피너 금지)
  entityDormant: "엔티티 미적재 — rebuild 필요", // U3 운영자 한정(kg_entities=0)
} as const;

// CONFLICTS_WITH 엣지의 sparse properties에서 칩으로 끌어오는 값. status는 항상(있으면),
// K8 FE6에서 conflict_reason/detected_at도 함께(있을 때만 — 매칭 conflict task가 reason을
// 가진 경우에만 투영되므로 sparse하다). gate visibility가 이미 owner/company 경계를 강제하므로
// 칩에 노출해도 page 본문 이하의 메타 노출이다(불변식 5 — 본문 아님, task 한 줄 사유/탐지 시각).
export type GraphChip = {
  node: KgGraphNode;
  hop: number;
  status?: string;
  conflictReason?: string;
  detectedAt?: string;
  // CONFLICTS_WITH 칩의 역할 — "own"=anchor(질의 페이지)에 BACKLINK로 직접 붙은 이 페이지의
  // 주장, "counterpart"=그 주장과 충돌하는 상대. 충돌 그룹에서 양쪽을 배지로 구분한다.
  // anchor가 claim인 conflicts_of에선 BACKLINK 인접이 없어 전부 "counterpart"다(claim-rooted).
  conflictSide?: "own" | "counterpart";
};
export type GraphGroup = { rel: string; chips: GraphChip[]; groupTotal: number };

// K8 FE4 — placeholder conflict counterpart(materialized=false)는 KG에 본문이 없다
// (grounding은 PG/wiki-store 전용 — 하드룰). slug를 주장 문장인 양 dump하지 않도록,
// 칩 뷰와 ring readout이 공유하는 명시 라벨. slug는 식별자로만(muted) 표기한다.
export const CONFLICT_PLACEHOLDER_LABEL = "반대편 주장(페이지 미생성) · 본문 없음";

// 충돌 counterpart인데 페이지가 아직 생성 안 된(placeholder) 노드인가 — 위 라벨 적용 기준.
export function isPlaceholderConflictCounterpart(chip: GraphChip): boolean {
  return chip.conflictSide === "counterpart" && !chip.node.materialized;
}

// sparse CONFLICTS_WITH 엣지 properties에서 칩 메타를 뽑는 단일 출처(string 값만 신뢰).
export type EdgeMeta = { status?: string; conflictReason?: string; detectedAt?: string };
export function edgeConflictMeta(e: KgGraphEdge): EdgeMeta {
  const str = (k: string): string | undefined =>
    typeof e.properties?.[k] === "string" ? (e.properties[k] as string) : undefined;
  return {
    status: str("status"),
    conflictReason: str("conflict_reason"),
    detectedAt: str("detected_at"),
  };
}

// 노드 id → 충돌 메타(노드 인스펙터·노드 stroke 공용 단일 출처). "충돌" 판정은 **status
// property 유무가 아니라 인접 CONFLICTS_WITH rel 존재**다(status 없는 충돌도 hasConflict=true —
// 그래프 stroke는 이미 warn을 그리므로 "그래프=충돌 · 인스펙터=공백" 불일치를 막는다, K8).
// status 병합은 **UNRESOLVED-wins + 승격 시 reason/detected_at 동반 교체**(first-edge-wins
// 순진 복제 금지 — "거짓 해소"를 K8이 차단). 입력 엣지는 호출부가 explorer `drawEdges`와 동일
// 필터/dedup을 거친 집합을 넘긴다(raw 누적 금지). properties는 sparse이므로 메타 부재가 기본.
export type NodeConflictMeta = {
  hasConflict: boolean;
  status?: string;
  conflictReason?: string;
  detectedAt?: string;
};

// 같은 노드/칩에 두 번째+ CONFLICTS_WITH 엣지가 닿을 때의 status/reason/detected_at 병합 규칙 —
// **노드 인스펙터(buildNodeConflictMeta)와 칩/링(buildGraphGroups)의 단일 출처**라 두 뷰가 같은
// 노드에 대해 다른 충돌 상태/사유를 보이지 않는다(3-뷰 일관). K8 "거짓 해소 금지" 안전 방향:
// **UNRESOLVED로만 상위 승격**하고(RESOLVED/미상 → UNRESOLVED), RESOLVED로는 절대 되돌리지 않는다
// (status-less/UNRESOLVED를 초록 "해소"로 바꾸지 않음). 승격 시 reason/detected_at은 승격 엣지 값으로
// **동반 교체**(없으면 비움 — 이전 상태의 메타가 남아 어긋나지 않게). 첫 엣지 값은 호출부의 최초
// 삽입이 담당하고, 이 헬퍼는 두 번째+ 엣지의 병합만 in-place로 처리한다.
export function mergeConflictMeta(
  cur: { status?: string; conflictReason?: string; detectedAt?: string },
  next: EdgeMeta,
): void {
  if (next.status === "UNRESOLVED" && cur.status !== "UNRESOLVED") {
    cur.status = "UNRESOLVED";
    cur.conflictReason = next.conflictReason;
    cur.detectedAt = next.detectedAt;
  }
}

// 한 노드의 충돌 메타만 계산한다(인스펙터가 선택 노드 1개만 읽으므로 전-노드 Map을 매번 만들지
// 않는다). 인접 CONFLICTS_WITH 엣지를 `mergeConflictMeta`(칩과 동일 규칙)로 접는다. 충돌이 없으면
// null. 입력 엣지는 호출부가 explorer `drawEdges`와 동일 필터/dedup을 거친 집합을 넘긴다.
export function conflictMetaForNode(
  edges: KgGraphEdge[],
  nodeId: string,
): NodeConflictMeta | null {
  let meta: NodeConflictMeta | null = null;
  for (const e of edges) {
    if (e.rel !== "CONFLICTS_WITH") continue;
    if (e.src !== nodeId && e.dst !== nodeId) continue;
    const em = edgeConflictMeta(e);
    if (!meta) {
      meta = {
        hasConflict: true,
        status: em.status,
        conflictReason: em.conflictReason,
        detectedAt: em.detectedAt,
      };
    } else {
      mergeConflictMeta(meta, em);
    }
  }
  return meta;
}

// 충돌 상태 평문 라벨(3-뷰 공유). raw RESOLVED/UNRESOLVED는 노출하지 않는다. status property
// 부재 충돌도 "확인이 필요한 충돌"(상태 미상은 오류처럼 읽힘). 해소만 "해소된 충돌".
export function conflictStatusResolved(status?: string): boolean {
  return status === "RESOLVED";
}
export function conflictStatusLabel(status?: string): string {
  return conflictStatusResolved(status) ? "해소된 충돌" : "확인이 필요한 충돌";
}

// detected_at(ISO datetime string) → 날짜만(ko-KR). 파싱 실패는 원문 그대로(빈 표기 금지).
// graph-chip-list에서 승격 — 인스펙터·칩·`/wiki/tasks`가 같은 로케일/포맷을 공유해 같은 task의
// 날짜가 뷰마다 어긋나지 않게 한다(3-뷰 일관).
export function formatDetectedAt(raw: string): string {
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? raw : d.toLocaleDateString("ko-KR");
}

// UUID면 앞 8자, slug placeholder면 그대로. title/slug 없는 노드(문서/사실)도 빈 칩 금지.
export function graphChipText(node: KgGraphNode): string {
  if (node.title) return node.title;
  if (node.slug) return node.slug;
  return /^[0-9a-f]{8}-[0-9a-f-]+$/i.test(node.id) ? node.id.slice(0, 8) : node.id;
}

// 현재 페이지 노드 id에서 엣지를 **무방향**으로 BFS해 hop 거리(id→거리)를 구한다.
// (Cypher가 무방향 -[...]- 이므로 src→dst 방향대로 따라가면 안 됨.)
export function graphHopDistances(
  edges: KgGraphEdge[],
  sourceId: string | null,
): Map<string, number> {
  const dist = new Map<string, number>();
  if (sourceId == null) return dist;
  const adj = new Map<string, string[]>();
  const link = (a: string, b: string) => {
    const list = adj.get(a);
    if (list) list.push(b);
    else adj.set(a, [b]);
  };
  for (const e of edges) {
    link(e.src, e.dst);
    link(e.dst, e.src);
  }
  dist.set(sourceId, 0);
  let frontier = [sourceId];
  let depth = 0;
  while (frontier.length) {
    depth += 1;
    const next: string[] = [];
    for (const u of frontier) {
      for (const v of adj.get(u) ?? []) {
        if (!dist.has(v)) {
          dist.set(v, depth);
          next.push(v);
        }
      }
    }
    frontier = next;
  }
  return dist;
}

// SHOULD-2 — 충돌 그룹을 own↔counterpart **쌍 단위로 인터리브**한다. hop 정렬만 쓰면 own
// claim(hop1, BACKLINK)이 counterpart(hop2)보다 먼저라 cap=5가 own으로 다 차서 정작 상대
// 주장이 가려진다(충돌 표면의 본질을 못 보임). 쌍으로 엮으면 cap이 chip 수로 잘려도 앞쪽
// 쌍은 양쪽(이 페이지+상대)이 함께 남는다. 결정론: 엣지를 own chip 정렬키로 정렬(SSR/hydration
// 안정), 쌍 안은 own→counterpart. 한 counterpart가 여러 own과 충돌하면 counterpart는 첫
// 등장에만(중복 제거). conflicts_of(claim-rooted)는 own이 없어 전부 counterpart라 정렬 순서대로
// 평탄화된다(기존 동작과 동일).
function interleaveConflictPairs(
  chips: GraphChip[],
  conflictEdges: KgGraphEdge[],
  ownIds: Set<string>,
): GraphChip[] {
  const byId = new Map(chips.map((c) => [c.node.id, c]));
  // 한 엣지의 표시 칩들을 own 먼저로 정렬(같은 역할이면 graphChipSort tiebreak).
  const orderEndpoints = (e: KgGraphEdge): GraphChip[] =>
    [e.src, e.dst]
      .filter((id) => byId.has(id))
      .sort((x, y) => {
        const ox = ownIds.has(x) ? 0 : 1;
        const oy = ownIds.has(y) ? 0 : 1;
        if (ox !== oy) return ox - oy;
        return graphChipSort(byId.get(x)!, byId.get(y)!);
      })
      .map((id) => byId.get(id)!);
  const sortedEdges = [...conflictEdges].sort((e1, e2) => {
    const a = orderEndpoints(e1)[0];
    const b = orderEndpoints(e2)[0];
    if (!a || !b) return a ? -1 : b ? 1 : 0;
    return graphChipSort(a, b);
  });
  const out: GraphChip[] = [];
  const used = new Set<string>();
  for (const e of sortedEdges) {
    for (const c of orderEndpoints(e)) {
      if (used.has(c.node.id)) continue;
      used.add(c.node.id);
      out.push(c);
    }
  }
  // 엣지에 닿지 않은 잔여 칩(이론상 없음) — 정렬 순서대로 보강해 누락 방지.
  for (const c of [...chips].sort(graphChipSort)) {
    if (!used.has(c.node.id)) {
      used.add(c.node.id);
      out.push(c);
    }
  }
  return out;
}

export function graphChipSort(a: GraphChip, b: GraphChip): number {
  if (a.hop !== b.hop) return a.hop - b.hop;
  if (a.node.materialized !== b.node.materialized) return a.node.materialized ? -1 : 1;
  const pa = NODE_LABEL_PRIORITY[a.node.label] ?? 99;
  const pb = NODE_LABEL_PRIORITY[b.node.label] ?? 99;
  if (pa !== pb) return pa - pb;
  return graphChipText(a.node).localeCompare(graphChipText(b.node));
}

/**
 * 시작 노드 id = 응답 slug와 같은 노드. 패널/`page_conflicts`는 WikiPage anchor지만,
 * `/ask`의 claim-rooted `conflicts_of`는 WikiClaim anchor다. WikiPage를 **우선** 매칭하고
 * (slug 충돌 시 페이지 우선 — 패널 불변), 없으면 같은 slug의 어떤 라벨 노드든 anchor로 잡는다.
 * 이렇게 해야 claim-rooted 답변에서도 시작 노드 자신이 칩으로 새지 않고(addChip의 currentId
 * 제외) hop 거리 BFS가 동작한다. data.slug가 빈 문자열이면(anchor 미상) null이다.
 */
export function currentGraphNodeId(data: WikiPageGraphResponse): string | null {
  if (!data.slug) return null;
  return (
    data.nodes.find((n) => n.label === "WikiPage" && n.slug === data.slug)?.id ??
    data.nodes.find((n) => n.slug === data.slug)?.id ??
    null
  );
}

/**
 * 응답 → 표시할 rel 그룹 목록 + FE-side truncated 여부.
 * - 칩은 (rel, node) 단위. 한 노드가 여러 rel에 닿으면 그룹마다 1칩.
 * - 현재 페이지 노드는 id로 제외(엣지 끝점 판별 기준). 미해결 시 hop 정렬만 무력화.
 * - 그룹당 5 + 전체 20 cap, REL_GROUP_ORDER 순으로 뒤 그룹부터 잘림.
 */
export type GraphGroupsBuilt = {
  groups: GraphGroup[];
  truncated: boolean;
  currentNodeId: string | null;
};

export function buildGraphGroups(data: WikiPageGraphResponse): GraphGroupsBuilt {
  const nodeById = new Map(data.nodes.map((n) => [n.id, n]));
  const currentId = currentGraphNodeId(data);
  const dist = graphHopDistances(data.edges, currentId);

  // anchor(currentId)에 BACKLINK로 직접 붙은 노드 = 이 페이지의 주장(own). 충돌 칩 역할
  // 배지의 단일 판정 — page_conflicts(anchor=page)에선 c(claim)가 own, o(counterpart)는 아님.
  // conflicts_of(anchor=claim)에선 BACKLINK 인접이 없어 비고 → 전부 counterpart(claim-rooted).
  const ownClaimIds = new Set<string>();
  if (currentId != null) {
    for (const e of data.edges) {
      if (e.rel !== "BACKLINK") continue;
      if (e.src === currentId) ownClaimIds.add(e.dst);
      else if (e.dst === currentId) ownClaimIds.add(e.src);
    }
  }

  // rel → (nodeId → chip), (rel,node) dedup
  const byRel = new Map<string, Map<string, GraphChip>>();
  const addChip = (rel: string, id: string, edgeMeta: EdgeMeta) => {
    if (id === currentId) return;
    const node = nodeById.get(id);
    if (!node) return;
    let bucket = byRel.get(rel);
    if (!bucket) {
      bucket = new Map();
      byRel.set(rel, bucket);
    }
    const existing = bucket.get(id);
    if (!existing) {
      bucket.set(id, {
        node,
        hop: dist.get(id) ?? Number.POSITIVE_INFINITY,
        status: edgeMeta.status,
        conflictReason: edgeMeta.conflictReason,
        detectedAt: edgeMeta.detectedAt,
        conflictSide:
          rel === "CONFLICTS_WITH"
            ? ownClaimIds.has(id)
              ? "own"
              : "counterpart"
            : undefined,
      });
    } else {
      // 같은 counterpart가 두 CONFLICTS_WITH 엣지로 닿을 때(한 페이지의 두 claim이 같은 상대와
      // 충돌) first-edge-wins면 RESOLVED가 미해소를 가릴 수 있다(코드리뷰 #2). `mergeConflictMeta`가
      // UNRESOLVED로만 승격(거짓 해소 차단, K8 과경고=안전)하고 reason/detected_at을 동반 교체한다 —
      // **노드 인스펙터와 동일 규칙**이라 같은 노드가 뷰마다 다른 충돌 상태/사유를 보이지 않는다.
      mergeConflictMeta(existing, edgeMeta);
    }
  };
  for (const e of data.edges) {
    const meta = edgeConflictMeta(e);
    if (currentId != null && (e.src === currentId || e.dst === currentId)) {
      addChip(e.rel, e.src === currentId ? e.dst : e.src, meta);
    } else {
      // 2-hop 이웃-이웃 엣지(또는 현재 노드 미해결): 양 끝점 모두 칩
      addChip(e.rel, e.src, meta);
      addChip(e.rel, e.dst, meta);
    }
  }

  // rel별 표시 배열(all)을 만든다 — 충돌 그룹만 쌍 인터리브(SHOULD-2: cap 안에 own+counterpart가
  // 함께 남게), 나머지는 hop 정렬. cap/order는 공용 capOrderedRelGroups로 통일(인스펙터와 동일 규칙).
  const allByRel = new Map<string, GraphChip[]>();
  for (const rel of orderRels(byRel.keys())) {
    const chipsForRel = [...(byRel.get(rel)?.values() ?? [])];
    const all =
      rel === "CONFLICTS_WITH"
        ? interleaveConflictPairs(
            chipsForRel,
            data.edges.filter((e) => e.rel === "CONFLICTS_WITH"),
            ownClaimIds,
          )
        : chipsForRel.sort(graphChipSort);
    if (all.length > 0) allByRel.set(rel, all);
  }
  const capped = capOrderedRelGroups(allByRel, (rel, all, shown) => ({
    rel,
    chips: shown,
    groupTotal: all.length,
  }));
  return {
    groups: capped.groups,
    truncated: data.truncated || capped.truncated,
    currentNodeId: currentId,
  };
}

/**
 * 칩 리스트와 **동일한 노드 집합**으로 그래프 뷰의 노드/엣지 부분집합을 만든다.
 * buildGraphGroups 결과(groups/currentNodeId)를 권위로 받아 재계산(BFS) 없이
 * 부분집합만 만든다 — 두 뷰의 **노드 집합은 항상 일치**한다.
 * - 노드 = 현재 노드 + 표시 칩 노드들
 * - 엣지 = 양 끝점이 모두 표시 노드 집합에 든 엣지(중복 제거; (src,rel,dst,scope) 단위 —
 *   서버 gate `kg_edge_dedup_key`와 동일 신원. status는 신원 키가 아니다).
 *   주의: cap으로 한쪽 끝점이 빠진 엣지는 제외되므로, ring 범례의 rel 집합이 칩 그룹의
 *   rel 집합보다 좁을 수 있다(노드는 일치, 엣지/범례는 cap 경계에서 부분집합).
 */
export function cappedGraphView(
  data: WikiPageGraphResponse,
  built: Pick<GraphGroupsBuilt, "groups" | "currentNodeId">,
): {
  nodes: KgGraphNode[];
  edges: KgGraphEdge[];
  currentNodeId: string | null;
} {
  const { groups, currentNodeId } = built;

  const keep = new Set<string>();
  if (currentNodeId) keep.add(currentNodeId);
  for (const g of groups) for (const c of g.chips) keep.add(c.node.id);

  const nodeById = new Map(data.nodes.map((n) => [n.id, n]));
  const nodes = [...keep]
    .map((id) => nodeById.get(id))
    .filter((n): n is KgGraphNode => Boolean(n));

  const seenEdge = new Set<string>();
  const edges: KgGraphEdge[] = [];
  for (const e of data.edges) {
    if (!keep.has(e.src) || !keep.has(e.dst)) continue;
    // (src,rel,dst,scope) dedup — 서버의 canonical 엣지 신원(gate `kg_edge_dedup_key`)과 동일
    // 키를 쓴다(코드리뷰 #4 — 두 레이어 규약 일치). projection MERGE가 노드쌍·rel·scope당 1
    // 엣지를 보장하므로 status는 그 엣지의 속성이지 신원이 아니다. JSON.stringify는 충돌-안전.
    const key = JSON.stringify([e.src, e.rel, e.dst, e.scope]);
    if (seenEdge.has(key)) continue;
    seenEdge.add(key);
    edges.push(e);
  }
  return { nodes, edges, currentNodeId };
}
