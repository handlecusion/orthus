"""K4 — typed Cypher template registry (docs/kg-model.md §4, 구현 명세 §6.1).

LLM의 자유 Cypher 생성은 없다 — 코드에 등록된 사전 컴파일 쿼리 문자열과
Pydantic 파라미터 모델만 실행 대상이다. `depth`/`max_hops` 상한은 Literal
타입이 하드코딩한다(Cypher 가변 길이 패턴 상한은 파라미터 불가 — kg-model §4).
값 파라미터(slug 등)는 driver 바인딩으로만 전달한다 — f-string/`%`/`.format`
으로 Cypher를 만들면 게이트 위반이며, unit 정적 검사가 write 키워드 부재와
함께 이를 고정한다(`tests/unit/test_kg_templates.py`).

구현 메모(K4, 계약 의미 불변): kg-model §4 reference는 `RETURN p, r, n`이지만
가변 길이 패턴의 중간 hop 노드는 변수에 바인딩되지 않아 driver가 속성 없는
stub으로 hydrate한다. 결과 매퍼가 모든 노드의 id 속성을 필요로 하므로
`MATCH path = ... RETURN path`로 반환한다 — Bolt Path 구조는 경로 위 전체
노드를 속성 포함으로 전달한다. MATCH 패턴 자체는 reference와 동일하다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, Field, model_validator

from orthus.kg.client import kg_owner_scope_enabled
from orthus.kg.visibility import visibility_predicate

# --- 파라미터 모델 (Literal이 depth/hop 상한 하드코딩의 구현) ----------------------


class NeighborsParams(BaseModel):
    slug: str = Field(min_length=1)
    depth: Literal[1, 2] = 1


class PathBetweenParams(BaseModel):
    slug_a: str = Field(min_length=1)
    slug_b: str = Field(min_length=1)
    max_hops: Literal[2, 3, 4] = 4

    @model_validator(mode="after")
    def _distinct_slugs(self) -> "PathBetweenParams":
        # Neo4j shortestPath는 동일 시작/끝 노드를 기본 설정에서 에러로 거부
        # 하므로 게이트가 선차단한다(§6.5 회귀 #6).
        if self.slug_a == self.slug_b:
            raise ValueError("identical_slugs")
        return self


class ConflictsOfParams(BaseModel):
    slug: str = Field(min_length=1)


class PageConflictsParams(BaseModel):
    # K8.2 — 시작점은 WikiPage slug(claim 아님). 패널 union(B2-a)과 /ask conflict
    # page-resolve(B3)가 공유한다.
    slug: str = Field(min_length=1)


class ProvenanceChainParams(BaseModel):
    slug: str = Field(min_length=1)


class EntityMentionsParams(BaseModel):
    # K6 — name_norm은 wiki slug가 아니므로 게이트 slug 선검증 대상이 아니다
    # (slug_params=()). 정규화는 persist 시점(orthus/kg/entities.py)에서 이뤄지며,
    # 호출자는 정규화된 name_norm을 전달한다.
    name_norm: str = Field(min_length=1)


class EntityNeighborsParams(BaseModel):
    # K9 — 시작점은 **WikiPage slug**(page-rooted, B3). 패널이 현재 페이지가 언급한
    # 엔티티를 매개로 그 엔티티를 함께 언급한 다른 페이지를 찾는다. slug는 company-only
    # 선검증 대상이라 slug_resolution에 등록한다(EntityMentions와 달리 resolvable slug 보유).
    slug: str = Field(min_length=1)


class ExpandNodeParams(BaseModel):
    # E1b — 임의 (label, id) 1-hop 확장. label allowlist는 owner-scope 5개 라벨(Entity 제외 —
    # Entity는 expand_entity). id는 merge key 값(page_id/doc_id/row_id UUID 문자열).
    label: Literal["WikiPage", "WikiClaim", "WikiSource", "Document", "StructuredFact"]
    id: str = Field(min_length=1)


class EntityOverviewParams(BaseModel):
    # 전용 지식그래프 화면 랜딩 개체 지도 — 입력 없음(company 전체 top 개체 co-mention).
    # slug/이름 파라미터가 없어 slug_resolution=(), bind_params=(). 상한은 setting_params.
    pass


class ExpandEntityParams(BaseModel):
    # E1b — Entity 노드 1-hop(MENTIONED_IN). entity_key는 **namespaced** entity_node_key
    # (company:kind:name_norm) — Neo4j :Entity 노드 merge 키와 일치(project.py). endpoint/gate가
    # 노드 id에서 unprefixed로 PG 확인 후, Cypher 바인딩용 namespaced를 이 필드로 넘긴다. company_always.
    entity_key: str = Field(min_length=1)


# --- 사전 컴파일 Cypher (read-only 절만 — 정적 검사 대상) --------------------------
#
# 지식 관계 rel allowlist — neighbors와 path_between이 **공유**한다. `IN_PROJECT`는
# 제외한다: :Project 허브(고정 4노드, 수백~수천 page 연결)는 지식 관계가 아니라
# 프로젝트 귀속일 뿐이다. K4 실측(2026-06-12)에서 neighbors가 허브를 지나면 LIMIT
# 50 결과가 100% 같은 프로젝트 형제 page로 도배돼 제외했고, path_between도 같은 이유로
# 제외한다(K6 PR3, 2026-06-12): 허브를 타면 같은 프로젝트의 임의 두 page가 항상
# a→:Project→b 2-hop으로 "연결"돼, 진짜 지식 연결 부재(missing_link)를 가린다.
# project 귀속 자체는 노드 props(`project`)로 이미 노출되므로 정보 손실이 없다.
# K6 entity substrate rel(MENTIONED_IN/RELATES_TO)도 substrate-only라 제외(결정③).
# (kg-model §4 / 구현 명세 §6.1·§9.3).

_KNOWLEDGE_RELS = "SUPPORTS|CONFLICTS_WITH|BACKLINK|DERIVED_FROM|EXTRACTED_FROM"

_NEIGHBORS_BY_DEPTH: dict[int, str] = {
    1: (
        "MATCH path = (p:WikiPage {slug:$slug})"
        f"-[:{_KNOWLEDGE_RELS}*1..1]-(n) "
        "RETURN path LIMIT $limit"
    ),
    2: (
        "MATCH path = (p:WikiPage {slug:$slug})"
        f"-[:{_KNOWLEDGE_RELS}*1..2]-(n) "
        "RETURN path LIMIT $limit"
    ),
}

_PATH_BETWEEN_BY_HOPS: dict[int, str] = {
    2: (
        "MATCH path = shortestPath("
        f"(a:WikiPage {{slug:$slug_a}})-[:{_KNOWLEDGE_RELS}*..2]-(b:WikiPage {{slug:$slug_b}})) "
        "RETURN path LIMIT $limit"
    ),
    3: (
        "MATCH path = shortestPath("
        f"(a:WikiPage {{slug:$slug_a}})-[:{_KNOWLEDGE_RELS}*..3]-(b:WikiPage {{slug:$slug_b}})) "
        "RETURN path LIMIT $limit"
    ),
    4: (
        "MATCH path = shortestPath("
        f"(a:WikiPage {{slug:$slug_a}})-[:{_KNOWLEDGE_RELS}*..4]-(b:WikiPage {{slug:$slug_b}})) "
        "RETURN path LIMIT $limit"
    ),
}

# K8.1 (B1) — 반대편 라벨 제약 제거: `(o:WikiClaim)` → `(o)`. placeholder counterpart
# (`:WikiPage {materialized:false}`, dangling conflict dst)가 claim 라벨에 안 잡혀 빈 결과가
# 되던 결함(docs/kg-k8-plan.md §0/D1)을 닫는다. CONFLICTS_WITH 엣지는 wiki_links에서만 나오고
# co-projection은 MENTIONED_IN/RELATES_TO만 만들어 `(o)`가 제3 노드 타입에 닿을 수 없으므로
# 무라벨이 안전하다(명시 union보다 placeholder 라벨 선택과의 결합을 줄인다).
_CONFLICTS_OF = (
    "MATCH (c:WikiClaim {slug:$slug})-[k:CONFLICTS_WITH]-(o) RETURN c, k, o LIMIT $limit"
)

# K8.2 (B2/B3) — 페이지 → 그 페이지의 claim들 → 모순 counterpart. 패널 union과 /ask conflict
# page-resolve가 공유하는 단일 게이트 템플릿. page↔claim은 BACKLINK 엣지로 그래프에 실재한다
# (consolidate가 claim.related_pages로 page.backlinks를 채움 → wiki_links rel='backlink').
# OPTIONAL MATCH라 모순이 0건이어도 시작 page `p`를 반환해 grounding이 비지 않는다(zero-conflict
# = grounded "모순 없음" 답변, demote 아님 — 개발검토 #1). 무방향 BACKLINK이되 중간을
# (c:WikiClaim)로 제약해 page↔page backlink 혼입을 막고, `(o)`는 무라벨(K8.1과 동일 — placeholder
# counterpart 포함). bl/k 엣지를 함께 RETURN해 그래프가 p–c–o로 연결돼 보이게 한다.
_PAGE_CONFLICTS = (
    "MATCH (p:WikiPage {slug:$slug}) "
    "OPTIONAL MATCH (p)-[bl:BACKLINK]-(c:WikiClaim)-[k:CONFLICTS_WITH]-(o) "
    "RETURN p, bl, c, k, o LIMIT $limit"
)

_PROVENANCE_CHAIN = (
    "MATCH (c:WikiClaim {slug:$slug})-[sr:SUPPORTS]->(s:WikiSource)"
    "-[er:EXTRACTED_FROM]->(d:Document) "
    "RETURN c, sr, s, er, d LIMIT $limit"
)

# K6 — "이 이름이 언급된 지식". name_norm은 (kind 무관) 동명 entity 복수에 매칭될
# 수 있다(person:김대표 vs org:김대표) — 둘 다 반환한다. substrate-only이며 K6에는
# 호출자가 없다(K4b 후속) — 게이트/회귀 테스트로만 검증한다(kg-model §0/§5).
_ENTITY_MENTIONS = (
    "MATCH (e:Entity {name_norm:$name_norm})-[m:MENTIONED_IN]->(n) RETURN e, m, n LIMIT $limit"
)

# K9 — entity_neighbors(page-rooted, 패널 K9.2). 현재 페이지 `p`가 언급한 엔티티 `e`를
# 매개로 그 엔티티를 함께 언급한 다른 페이지 `q`를 찾는다 — 검색이 못 주는 비명시 cross-page
# 연결("이 페이지와 인물/제품/프로젝트가 겹치는 다른 페이지"). company-only 데이터(엔티티는
# company page만 mention, entities.py)라 owner-variant가 없고 `predicate_kind='company_always'`
# 로 flag 무관 서빙된다(C-A). **RETURN path 필수**(C-B): bare `RETURN p, e, q`는 incident
# MENTIONED_IN 엣지를 잃어 엔티티 다리가 소실된다(_flatten은 Path만 엣지 hydrate). `e`가 path에
# 바인딩돼 kind CASE + mention_count 전역 ORDER BY가 유효하고 한 row=한 path라 truncated(B2)가
# 정직하다. **per-entity 캡은 Cypher에 없다**(R1 — collect()는 RETURN path·truncated 동시 파괴) —
# 흔한 엔티티 제외는 `e.mention_count < $hub_threshold` degree ceiling(C-E/U1, page-only 집계와
# 일치), 신호 정렬은 kind 가중(project/system > person > org) + mention_count 오름차순(희소 우선).
# per-entity ≤3 표시 캡은 FE 전용(서버 nodes/edges/truncated 불변). $hub_threshold는 게이트가
# settings에서 bound $param으로 주입(R9 — rebuild 없이 튜닝 가능).
_ENTITY_NEIGHBORS = (
    "MATCH path = (p:WikiPage {slug:$slug})<-[:MENTIONED_IN]-(e:Entity)"
    "-[:MENTIONED_IN]->(q:WikiPage) "
    "WHERE q <> p AND e.mention_count < $hub_threshold "
    "RETURN path "
    "ORDER BY CASE e.entity_kind WHEN 'project' THEN 0 WHEN 'system' THEN 0 "
    "WHEN 'person' THEN 1 ELSE 2 END ASC, e.mention_count ASC "
    "LIMIT $limit"
)


# --- owner-variant 사전 컴파일 (K7.2) ---------------------------------------------
#
# 단일 fragment(`visibility_predicate`)를 **모든 bound 노드 AND 모든 bound 엣지**에
# 합성한다(B1). 시작노드는 slug가 아니라 resolve된 page_id로 바인딩한다 — Neo4j MATCH
# `{slug:$slug}`는 동명 cross-owner 노드를 전부 매칭하므로 page_id 단일 노드 바인딩으로
# 차단한다(B4). flag-off는 위 v1 문자열을 **그대로** 반환하므로 byte-identical(§3 Step 2,
# `_kg_flag_off_cypher_golden.json`이 고정 = rollback 전제).


def _all_visible(path_var: str) -> str:
    """path 변수의 전체 노드/엣지에 owner predicate 합성(B1). 내부 변수는 `_n`/`_r`로
    v1 템플릿의 `n`/`p`와 충돌을 피한다."""
    return (
        f"all(_n IN nodes({path_var}) WHERE {visibility_predicate('_n')}) "
        f"AND all(_r IN relationships({path_var}) WHERE {visibility_predicate('_r')})"
    )


def _all_company(path_var: str) -> str:
    """framing A — company-only predicate(`scope='company'`)를 전체 노드/엣지에. owner의
    자기 personal hop도 제외한다(비-owner/admin도 framing A를 실행하므로 절대 누수 불가)."""
    return (
        f"all(_n IN nodes({path_var}) WHERE _n.scope = 'company') "
        f"AND all(_r IN relationships({path_var}) WHERE _r.scope = 'company')"
    )


_NEIGHBORS_OWNER_BY_DEPTH: dict[int, str] = {
    d: (
        "MATCH path = (p:WikiPage {page_id:$page_id})"
        f"-[:{_KNOWLEDGE_RELS}*1..{d}]-(n) "
        f"WHERE {_all_visible('path')} "
        "RETURN path LIMIT $limit"
    )
    for d in (1, 2)
}

# E1b — label → Neo4j merge key(KgGraphNode.id 추출 우선순위와 정합). WikiPage/Claim/Source는
# 모두 page_id(전부 wiki_pages row, project.py emit_node(label,"page_id",...)).
_EXPAND_MERGE_KEY: dict[str, str] = {
    "WikiPage": "page_id",
    "WikiClaim": "page_id",
    "WikiSource": "page_id",
    "Document": "doc_id",
    "StructuredFact": "row_id",
}

# flag-off (company-only, 술어 0) — merge key로 직접 MATCH. IN_PROJECT 제외 유지. RETURN path 필수.
_EXPAND_BY_LABEL: dict[str, str] = {
    label: (
        f"MATCH path = (p:{label} {{{mk}:$id}})"
        f"-[:{_KNOWLEDGE_RELS}*1..1]-(n) "
        "RETURN path LIMIT $limit"
    )
    for label, mk in _EXPAND_MERGE_KEY.items()
}

# owner-variant — _all_visible('path')(nodes+relationships) + $caller. neighbors owner-variant 동형.
_EXPAND_OWNER_BY_LABEL: dict[str, str] = {
    label: (
        f"MATCH path = (p:{label} {{{mk}:$id}})"
        f"-[:{_KNOWLEDGE_RELS}*1..1]-(n) "
        f"WHERE {_all_visible('path')} "
        "RETURN path LIMIT $limit"
    )
    for label, mk in _EXPAND_MERGE_KEY.items()
}

# Entity — company_always, MENTIONED_IN(entity→page 방향), owner 술어 없음. RETURN path(엣지 hydrate).
_EXPAND_ENTITY = (
    "MATCH path = (e:Entity {entity_key:$entity_key})-[:MENTIONED_IN]->(n) RETURN path LIMIT $limit"
)

# 전용 지식그래프 화면 랜딩 — 개체(Entity) co-mention 지도. 함께 언급된(RELATES_TO) 개체 쌍을
# 언급량(mention_count) 합이 큰 순으로 상한(`$overview_limit`)까지 돌려준다 — 가장 중심적인
# 지식 허브가 먼저 보이는 대표 지도다. company_always(Entity는 항상 company scope, project.py).
# `$limit`(kg_query_limit=50 cap)과 충돌하지 않도록 별도 `$overview_limit`를 쓴다(settings 주석).
_ENTITY_OVERVIEW = (
    "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) "
    "RETURN a, r, b "
    "ORDER BY coalesce(a.mention_count, 0) + coalesce(b.mention_count, 0) DESC "
    "LIMIT $overview_limit"
)

_PATH_BETWEEN_OWNER_BY_HOPS: dict[int, str] = {
    h: (
        "MATCH path = shortestPath("
        f"(a:WikiPage {{page_id:$page_id_a}})-[:{_KNOWLEDGE_RELS}*..{h}]-"
        f"(b:WikiPage {{page_id:$page_id_b}})) "
        f"WHERE {_all_visible('path')} "
        "RETURN path LIMIT $limit"
    )
    for h in (2, 3, 4)
}

# framing A (SETTLED #2) — company-only-predicate path_between. 비-owner/admin이 실행하는
# "직접 연결(전부 회사)" 경로. bare v1(_PATH_BETWEEN_BY_HOPS, 술어 0)이 아니다 — flag-on +
# personal row 투영 시 v1은 타 owner personal hop을 순회하므로(B1/B2 누수) 반드시 명시적
# company predicate를 단다(docs/kg-k7.2-plan §3 Step 4 / parent §3).
_PATH_BETWEEN_COMPANY_BY_HOPS: dict[int, str] = {
    h: (
        "MATCH path = shortestPath("
        f"(a:WikiPage {{page_id:$page_id_a}})-[:{_KNOWLEDGE_RELS}*..{h}]-"
        f"(b:WikiPage {{page_id:$page_id_b}})) "
        f"WHERE {_all_company('path')} "
        "RETURN path LIMIT $limit"
    )
    for h in (2, 3, 4)
}

# K8.1 (B1) — owner-variant도 동일 완화(`(o:WikiClaim)` → `(o)`). 한쪽만 완화하면
# owner-scope-ON 운영자에게 placeholder counterpart 누락 버그가 잔존한다(scope 의존 정합 깨짐).
# 무라벨 `(o)`에 `visibility_predicate('o')`는 그대로 유지 — 라벨 제약이 사라진 만큼 술어가
# counterpart 경계의 유일한 가드다(placeholder는 source claim 네임스페이스 상속).
_CONFLICTS_OF_OWNER = (
    "MATCH (c:WikiClaim {page_id:$page_id})-[k:CONFLICTS_WITH]-(o) "
    f"WHERE {visibility_predicate('c')} AND {visibility_predicate('o')} "
    f"AND {visibility_predicate('k')} "
    "RETURN c, k, o LIMIT $limit"
)

# K8.2 owner-variant — page_id 시작 바인딩(B4 cross-owner slug 충돌 차단) + 모든 bound 노드
# (p/c/o) AND 모든 bound 엣지(bl/k)에 visibility_predicate(B1). OPTIONAL MATCH WHERE는 optional
# 부분에만 걸려 conflict가 없거나 비가시면 p만 반환된다(zero-conflict grounding 보존). BACKLINK
# 엣지 bl도 가드한다 — owner-variant에서 unbound 엣지가 경계 누출 벡터가 되지 않게(개발검토 #3).
_PAGE_CONFLICTS_OWNER = (
    "MATCH (p:WikiPage {page_id:$page_id}) "
    f"WHERE {visibility_predicate('p')} "
    "OPTIONAL MATCH (p)-[bl:BACKLINK]-(c:WikiClaim)-[k:CONFLICTS_WITH]-(o) "
    f"WHERE {visibility_predicate('bl')} AND {visibility_predicate('c')} "
    f"AND {visibility_predicate('k')} AND {visibility_predicate('o')} "
    "RETURN p, bl, c, k, o LIMIT $limit"
)

_PROVENANCE_CHAIN_OWNER = (
    "MATCH (c:WikiClaim {page_id:$page_id})-[sr:SUPPORTS]->(s:WikiSource)"
    "-[er:EXTRACTED_FROM]->(d:Document) "
    f"WHERE {visibility_predicate('c')} AND {visibility_predicate('s')} "
    f"AND {visibility_predicate('d')} AND {visibility_predicate('sr')} "
    f"AND {visibility_predicate('er')} "
    "RETURN c, sr, s, er, d LIMIT $limit"
)


def _eff_owner_scope(owner_scope: bool | None) -> bool:
    """owner-scope 유효값 — 호출자가 명시(게이트가 요청당 1회 읽어 주입, TOCTOU 차단)하면
    그 값을, None이면 flag를 읽는다(테스트/owner_variants_present/golden 등 비-게이트 호출의
    하위호환). 게이트는 항상 명시값을 넘겨 bind(page_id/slug)와 cypher 변형이 일치한다."""
    return kg_owner_scope_enabled() if owner_scope is None else owner_scope


def _neighbors_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    assert isinstance(params, NeighborsParams)
    table = _NEIGHBORS_OWNER_BY_DEPTH if _eff_owner_scope(owner_scope) else _NEIGHBORS_BY_DEPTH
    return table[params.depth]


def _path_between_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    assert isinstance(params, PathBetweenParams)
    table = _PATH_BETWEEN_OWNER_BY_HOPS if _eff_owner_scope(owner_scope) else _PATH_BETWEEN_BY_HOPS
    return table[params.max_hops]


def _path_between_company_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    # framing A — 항상 company-only predicate(flag 무관). 호출자는 run_path_framings(flag-on
    # 전용, §3 Step 4); flag-off 직접 호출은 게이트 2c가 owner_scope_required로 막는다.
    assert isinstance(params, PathBetweenParams)
    return _PATH_BETWEEN_COMPANY_BY_HOPS[params.max_hops]


def _conflicts_of_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    return _CONFLICTS_OF_OWNER if _eff_owner_scope(owner_scope) else _CONFLICTS_OF


def _page_conflicts_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    return _PAGE_CONFLICTS_OWNER if _eff_owner_scope(owner_scope) else _PAGE_CONFLICTS


def _provenance_chain_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    return _PROVENANCE_CHAIN_OWNER if _eff_owner_scope(owner_scope) else _PROVENANCE_CHAIN


def _entity_mentions_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    # D2 — entity_key에 owner 성분이 없어 owner-variant가 구조적 no-op이다. owner-variant를
    # 만들지 않고 v1을 유지한다. K9에서 `predicate_kind='company_always'`로 재분류해 /ask 소비자
    # wiring을 허용하되(B3 — entity-rooted), owner 술어는 없다(flag 무관 company-only). owner-scope
    # ON에서도 v1을 그대로 반환한다(entity는 company-only projection이라 누수 아님, owner_scope 무시).
    # **promotable**: personal-entity 슬라이스가 owner-variant + B-row를 얻으면 `owner` 종류로
    # 승격한다(deferred→owner 동형 staging, terminal bypass 아님 — kg-k9-plan §5).
    return _ENTITY_MENTIONS


def _entity_neighbors_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    # company_always — entity는 company-only projection이라 owner-variant가 구조적 no-op이고
    # owner_scope 인자를 무시한다(게이트가 stage-4에서 company_always를 항상 flag-OFF company-fork로
    # 태워 $caller를 미바인딩하므로, 이 함수가 owner 변형을 골라선 안 된다). 단일 변형(C-A).
    return _ENTITY_NEIGHBORS


def _expand_node_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    assert isinstance(params, ExpandNodeParams)
    table = _EXPAND_OWNER_BY_LABEL if _eff_owner_scope(owner_scope) else _EXPAND_BY_LABEL
    return table[params.label]


def _expand_entity_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    # company_always — owner_scope 무시(entity는 company-only projection, gate stage-4 company-fork).
    return _EXPAND_ENTITY


def _entity_overview_cypher(params: BaseModel, owner_scope: bool | None = None) -> str:
    # company_always — 랜딩 개체 지도는 company 전체 top 개체 co-mention(owner 성분 없음).
    return _ENTITY_OVERVIEW


# --- registry --------------------------------------------------------------------


@dataclass(frozen=True)
class KgTemplate:
    name: str
    params_model: type[BaseModel]
    # 사전 컴파일 변형 중 선택만(보간 금지). (params, owner_scope=None) — 게이트가 요청당
    # 1회 읽은 owner_scope를 주입하고(TOCTOU 차단), None이면 함수가 flag를 읽는다(하위호환).
    cypher_for: Callable[..., str]
    # K7.2: slug 선검증 + page_id resolve를 함께 구동하는 단일 선언 (field, label).
    # `label`이 read까지 흘러 resolve_slug가 kind 디스패치한다. () = resolvable slug 없음.
    slug_resolution: tuple[tuple[str, str], ...]
    bind_params: tuple[str, ...]  # flag-OFF driver 바인딩 필드 (v1, 선택자 제외)
    # flag-ON 바인딩 — (resolved_field, id_param). resolve된 page_id를 이 id_param으로 바인딩.
    owner_bind: tuple[tuple[str, str], ...]
    # 경계 술어 종류: 'owner'=visibility_predicate, 'company'=scope='company'(framing A),
    # 'company_always'=owner-술어 면제 + flag 무관 서빙 + 소비자 wiring 허용(K9 entity_neighbors/
    # entity_mentions — 데이터가 구조상 company-only, promotable), 'deferred'=술어 없음 + 소비자
    # wiring 금지(현재 미사용). registry-completeness + owner_variants_present의 분류 축(§3 Step 7).
    predicate_kind: Literal["owner", "company", "company_always", "deferred"]
    description: str
    # 서버가 settings에서 주입하는 추가 bind $param (bind_key, settings_attr). `limit`처럼
    # 클라이언트 입력이 아니라 서버 고정값을 Cypher $param으로 바인딩한다(게이트 stage 6). K9
    # entity_neighbors의 `$hub_threshold`(=settings.kg_entity_hub_threshold)가 첫 사용처 —
    # rebuild 없이 튜닝 가능하게 Cypher 상수가 아닌 bound $param으로 둔다(R9).
    setting_params: tuple[tuple[str, str], ...] = ()
    # K9.3a (U6) — kg_query_runs 로그에서 **완전히 drop**할 param 키. `redact_pii`는 이메일/전화만
    # 지우고 사람·조직 이름 자체(entity_mentions의 `name_norm`)는 Direct PII로 남는다 → 로깅 carve-out을
    # 역전해 아예 기록하지 않는다(게이트 stage 4). `setting_params`(서버 주입)와 대칭으로, 어떤 param이
    # 로그 표면에 부적합한지를 템플릿이 선언한다. 빈 tuple이면 기존 동작(전체 params redact 후 기록).
    log_drop_params: tuple[str, ...] = ()


TEMPLATES: dict[str, KgTemplate] = {
    "neighbors": KgTemplate(
        name="neighbors",
        params_model=NeighborsParams,
        cypher_for=_neighbors_cypher,
        slug_resolution=(("slug", "WikiPage"),),
        bind_params=("slug",),
        owner_bind=(("slug", "page_id"),),
        predicate_kind="owner",
        description="페이지 주변 1-2 hop 관계 (K4 page graph API의 단일 소비 템플릿)",
    ),
    "path_between": KgTemplate(
        name="path_between",
        params_model=PathBetweenParams,
        cypher_for=_path_between_cypher,
        slug_resolution=(("slug_a", "WikiPage"), ("slug_b", "WikiPage")),
        bind_params=("slug_a", "slug_b"),
        owner_bind=(("slug_a", "page_id_a"), ("slug_b", "page_id_b")),
        predicate_kind="owner",
        description='"A와 B는 무슨 관계?" — 지식 rel 최단 경로 (hop ≤ 4, IN_PROJECT 제외)',
    ),
    "path_between_company": KgTemplate(
        # framing A (SETTLED #2) — company-only-predicate path_between. run_path_framings
        # (flag-on)만 호출. 비-owner/admin 경로라 company predicate 필수(§3 Step 4).
        name="path_between_company",
        params_model=PathBetweenParams,
        cypher_for=_path_between_company_cypher,
        slug_resolution=(("slug_a", "WikiPage"), ("slug_b", "WikiPage")),
        bind_params=("slug_a", "slug_b"),
        owner_bind=(("slug_a", "page_id_a"), ("slug_b", "page_id_b")),
        predicate_kind="company",
        description='framing A — "직접 연결(전부 회사)" company-only path (two-framing §3)',
    ),
    "conflicts_of": KgTemplate(
        name="conflicts_of",
        params_model=ConflictsOfParams,
        cypher_for=_conflicts_of_cypher,
        slug_resolution=(("slug", "WikiClaim"),),
        bind_params=("slug",),
        owner_bind=(("slug", "page_id"),),
        predicate_kind="owner",
        description="claim의 충돌 claim 추적 (CONFLICTS_WITH 양방향)",
    ),
    "page_conflicts": KgTemplate(
        name="page_conflicts",
        params_model=PageConflictsParams,
        cypher_for=_page_conflicts_cypher,
        slug_resolution=(("slug", "WikiPage"),),
        bind_params=("slug",),
        owner_bind=(("slug", "page_id"),),
        predicate_kind="owner",
        description="페이지의 claim들이 가진 모순(CONFLICTS_WITH) — 패널 union + /ask conflict page-resolve 공유",
    ),
    "provenance_chain": KgTemplate(
        name="provenance_chain",
        params_model=ProvenanceChainParams,
        cypher_for=_provenance_chain_cypher,
        slug_resolution=(("slug", "WikiClaim"),),
        bind_params=("slug",),
        owner_bind=(("slug", "page_id"),),
        predicate_kind="owner",
        description="claim→source→document 근거 체인",
    ),
    "entity_mentions": KgTemplate(
        name="entity_mentions",
        params_model=EntityMentionsParams,
        cypher_for=_entity_mentions_cypher,
        slug_resolution=(),  # name_norm은 wiki slug 아님 — resolvable slug 없음
        bind_params=("name_norm",),
        owner_bind=(),
        # K9 — deferred→company_always 재분류(C-A/B3): /ask entity intent(K9.3a) wiring 허용.
        # owner 술어 없음(company-only data), gate stage-4가 company-fork 강제($caller 미바인딩).
        predicate_kind="company_always",
        # K9.3a (U6) — name_norm은 사람/조직명(Direct PII)이라 kg_query_runs 로그에서 drop한다.
        log_drop_params=("name_norm",),
        description="이 이름(name_norm)이 언급된 지식 — K6 substrate (K9.3a /ask entity intent 소비)",
    ),
    "entity_neighbors": KgTemplate(
        # K9 — page-rooted 다리(패널 K9.2). 현재 페이지가 언급한 엔티티를 매개로 그 엔티티를
        # 함께 언급한 다른 페이지를 찾는다. company-only data → company_always(C-A): owner-variant
        # 없음, gate stage-4가 company-fork 강제($caller/owner_id 미바인딩). slug는 company-only
        # 선검증 대상이라 slug_resolution 보유(EntityMentions와 달리 resolvable slug). owner_bind=()
        # — page_id/$caller 없음(company_always는 owner resolve 루프를 안 탄다).
        name="entity_neighbors",
        params_model=EntityNeighborsParams,
        cypher_for=_entity_neighbors_cypher,
        slug_resolution=(("slug", "WikiPage"),),  # company 선검증 구동(404 판정)
        bind_params=("slug",),
        owner_bind=(),
        predicate_kind="company_always",
        setting_params=(("hub_threshold", "kg_entity_hub_threshold"),),
        description="페이지가 공유 엔티티로 엮인 다른 페이지 — K9 비명시 cross-page 발견(패널)",
    ),
    "expand_node": KgTemplate(
        name="expand_node",
        params_model=ExpandNodeParams,
        cypher_for=_expand_node_cypher,
        slug_resolution=(),  # (label,id) precheck는 게이트 신규 분기(D2) — slug_resolution 미사용
        bind_params=("id",),  # flag-OFF: $id 바인딩(label은 cypher 선택자, 바인딩 아님)
        owner_bind=(),  # precheck가 $id/$caller 직접 bind(owner_bind resolve 루프 미사용)
        predicate_kind="owner",
        description="임의 (label,id) 노드의 1-hop 이웃 — E1b 그래프 탐색기(5개 owner 라벨)",
    ),
    "expand_entity": KgTemplate(
        name="expand_entity",
        params_model=ExpandEntityParams,
        cypher_for=_expand_entity_cypher,
        slug_resolution=(),
        bind_params=("entity_key",),
        owner_bind=(),
        predicate_kind="company_always",
        log_drop_params=(
            "entity_key",
        ),  # entity_key=namespaced(kind:name_norm) — person name_norm Direct PII
        description="Entity 노드의 언급 페이지 1-hop(MENTIONED_IN) — E1b 그래프 탐색기",
    ),
    "entity_overview": KgTemplate(
        name="entity_overview",
        params_model=EntityOverviewParams,
        cypher_for=_entity_overview_cypher,
        slug_resolution=(),  # 입력 slug 없음(company 전체 개체 지도)
        bind_params=(),  # 입력 파라미터 없음 — 상한만 setting_params로 주입
        owner_bind=(),
        predicate_kind="company_always",
        # `$limit`(gate bind, kg_query_limit=50 cap)과 충돌 없이 별도 이름으로 상한 주입.
        setting_params=(("overview_limit", "kg_overview_limit"),),
        description="전용 지식그래프 화면 랜딩 — company top 개체 co-mention 지도",
    ),
}


def all_cypher_variants() -> tuple[str, ...]:
    """등록된 사전 컴파일 Cypher 전수 — write 키워드 정적 검사(§6.2)의 입력. K7.2 owner/
    company variant도 포함(write-keyword 정적검사 대상). flag-off byte-identity는 별도
    golden 테스트가 본다(이 합집합 멤버십으로 대체 금지)."""
    return (
        *_NEIGHBORS_BY_DEPTH.values(),
        *_PATH_BETWEEN_BY_HOPS.values(),
        _CONFLICTS_OF,
        _PAGE_CONFLICTS,
        _PROVENANCE_CHAIN,
        _ENTITY_MENTIONS,
        _ENTITY_NEIGHBORS,  # K9 — company_always(write-keyword 정적검사 대상)
        # K7.2 owner/company variants
        *_NEIGHBORS_OWNER_BY_DEPTH.values(),
        *_PATH_BETWEEN_OWNER_BY_HOPS.values(),
        *_PATH_BETWEEN_COMPANY_BY_HOPS.values(),
        _CONFLICTS_OF_OWNER,
        _PAGE_CONFLICTS_OWNER,
        _PROVENANCE_CHAIN_OWNER,
        # E1b expand variants (write-keyword 정적검사 대상)
        *_EXPAND_BY_LABEL.values(),
        *_EXPAND_OWNER_BY_LABEL.values(),
        _EXPAND_ENTITY,
        _ENTITY_OVERVIEW,  # 랜딩 개체 지도 — company_always(write-keyword 정적검사 대상)
    )


def owner_variants_present() -> bool:
    """flag-on일 때 모든 owner-bearing 템플릿(`predicate_kind=='owner'`)의 Cypher가
    canonical `visibility_predicate` fragment를 담고 있는지 — inter-PR fail-closed 가드
    (§6, L3)의 단일 판정. company(framing A)는 `scope='company'` 술어, company_always(K9
    entity)·deferred는 면제(owner-variant 없는 company-only data — 술어 검사 비대상).

    이 함수는 flag를 owner-scope ON으로 가정하고 cypher_for 결과를 검사한다(호출자가
    flag 상태를 보장). owner predicate fragment는 `_n`/start-node var별로 다른 변수명을
    쓰므로, 변수-무관한 핵심 토큰(`.scope = 'company' OR `)으로 판정한다.

    path 템플릿은 노드 술어만으로 부족하다 — relationships(path)까지 가드해야 한다(B1
    rel-clause green-by-vacuity 차단, 코드리뷰). 노드만 가드한 owner path 템플릿이 토큰
    grep만으로 True를 받아 런타임 fail-closed가 거짓 안심을 주던 구멍을 닫는다."""
    token = ".scope = 'company' OR "  # visibility_predicate의 변수-무관 핵심 토큰
    for tmpl in TEMPLATES.values():
        if tmpl.predicate_kind == "owner":
            sample = _OWNER_SAMPLE_PARAMS.get(tmpl.name)
            cypher = tmpl.cypher_for(sample)
            if "$caller" not in cypher or token not in cypher:
                return False
            # path 템플릿: nodes(path) AND relationships(path) 둘 다 술어로 감싸야 한다.
            if "nodes(path)" in cypher and (
                "relationships(path)" not in cypher
                or cypher.count(token) < 2  # 노드 절 + 엣지 절 = 최소 2회
            ):
                return False
        elif tmpl.predicate_kind == "company":
            sample = _OWNER_SAMPLE_PARAMS.get(tmpl.name)
            company_cypher = tmpl.cypher_for(sample)
            if "_n.scope = 'company'" not in company_cypher:
                return False
            # company path 템플릿도 relationships(path)까지 company 술어로 감싸야 한다.
            if "nodes(path)" in company_cypher and "relationships(path)" not in company_cypher:
                return False
    return True


# 템플릿별 대표 파라미터(변형 선택용). owner_variants_present는 owner/company 템플릿에만
# cypher_for를 호출하지만(company_always/deferred는 면제), registry-completeness
# (`test_non_deferred_templates_have_owner_sample_params`)이 deferred 외 전 템플릿에 sample을
# 요구하므로 company_always(K9 entity)도 함께 등록한다(누락 시 cypher_for(None) 위험 가드, R2).
_OWNER_SAMPLE_PARAMS: dict[str, BaseModel] = {
    "neighbors": NeighborsParams(slug="_probe", depth=1),
    "path_between": PathBetweenParams(slug_a="_a", slug_b="_b", max_hops=2),
    "path_between_company": PathBetweenParams(slug_a="_a", slug_b="_b", max_hops=2),
    "conflicts_of": ConflictsOfParams(slug="_probe"),
    "page_conflicts": PageConflictsParams(slug="_probe"),
    "provenance_chain": ProvenanceChainParams(slug="_probe"),
    "entity_mentions": EntityMentionsParams(name_norm="_probe"),  # K9 company_always(R2)
    "entity_neighbors": EntityNeighborsParams(slug="_probe"),  # K9 company_always(R2)
    "expand_node": ExpandNodeParams(label="WikiPage", id="_probe"),  # E1b owner
    "expand_entity": ExpandEntityParams(entity_key="_probe"),  # E1b company_always(R2)
    "entity_overview": EntityOverviewParams(),  # 랜딩 개체 지도 company_always(R2, 무입력)
}
