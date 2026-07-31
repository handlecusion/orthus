"""K2 — SoR(Postgres + wiki-store frontmatter) → projection plan 변환.

순수 결정론이다: LLM 0회, **Neo4j 비의존**(이 모듈은 neo4j를 import하지 않는다 —
같은 SoR 입력이면 같은 plan이 나옴을 unit 테스트로 고정한다, 구현 명세 §4.1).
v1 입력은 `scope='company'` row만이다(P8 정합) — 아래 SELECT의 WHERE절 자체가
K2 boundary 회귀(`test_owner_scope_rows_not_projected`)의 검증 대상이다.

projection은 SoR을 수리하거나 속성을 발명하지 않는다(kg-model §2 엣지 저작
원칙): frontmatter 불일치는 보강 속성/provenance 엣지 생략 + warning 카운트로
수렴하고, 매칭되지 않는 conflict task 속성은 null로 둔다.

batch(rebuild/sync)와 K3 outbox 단일-row 재투영이 전부 같은 `build()`를 지나
세 경로의 변환 결과가 구조적으로 동일하다(구현 명세 §5.4).
"""

from __future__ import annotations

import itertools
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from orthus.db import session
from orthus.kg.client import kg_owner_scope_enabled
from orthus.kg.schema import (
    PROJECTS,
    WIKI_KIND_LABEL_MAP,
    WIKI_LINK_REL_MAP,
    KgEdgeRow,
    KgNodeRow,
    Label,
    Rel,
)
from orthus.kg.visibility import (
    COMPANY_NS,
    doc_visible_to,
    entity_node_key,
    entity_node_key_for,
    ns_for,
    ns_key,
    owner_inclusive_sql,
    owner_scope_columns,
)
from orthus.tables import (
    documents,
    kg_entities,
    kg_entity_mentions,
    structured_rows,
    wiki_links,
    wiki_pages,
)
from orthus.wiki import store as wiki_store

_WIKI_NODE_KINDS = ("page", "claim", "source")
_MENTION_PAGE_KINDS = ("page", "claim")  # kg_entity_mentions.page_id 대상(§9.1)

# RELATES_TO co-mention page당 폭증 cap — entity_key 정렬 후 앞 N개만 쌍 생성
# (결정론). 측정 후 조정 가능, 상한 완화는 별도 결정(kg-model §2).
_CO_MENTION_MAX_ENTITIES_PER_PAGE = 25


class ScopeFilter(Enum):
    """projection 입력 scope.

    - `COMPANY` — v1. SELECT가 `scope='company'`만, scope/owner_id 컬럼 미선택 →
      flag-off 질의 문자열이 v1과 byte-identical(fast-rollback 전제, §1.5 snapshot).
    - `COMPANY_PLUS_OWNER` — K7. SELECT가 `company OR (personal AND owner IS NOT
      NULL)`로 넓어지고 scope/owner_id를 함께 고른다. owner 컬럼 매핑(kg-model §3):
      `wiki_pages`/`structured_rows`→`owner_id`, `documents`→`user_id`.

    어느 필터를 쓸지는 `active_scope_filter()`(D3 hard-guard) 단일 지점이 정한다 —
    `build()`는 필터를 모른다(들어온 row의 scope/owner를 그대로 키로 쓸 뿐, 순수).
    """

    COMPANY = "company"
    COMPANY_PLUS_OWNER = "company_plus_owner"


def active_scope_filter() -> ScopeFilter:
    """rebuild/sync/outbox `load_one`이 공유하는 단일 결정 지점.

    owner-scope hard-guard(`kg_owner_scope_enabled()` = `ORTHUS_KG_OWNER_SCOPE_ENABLED
    AND ORTHUS_OWNER_SCOPE_ENABLED`)가 꺼져 있으면 `COMPANY`다 — 이때 모든 SELECT가
    v1과 byte-identical이라(아래 `_*_select`) 코드만 머지하고 flag를 안 켜면 동작
    불변이다. rebuild/sync는 batch 시작 시 이 함수로 live flag를 읽는다. **outbox
    worker는 이 함수를 쓰지 않고** 이벤트의 저장된 scope에서 필터를 유도한다
    (`scope_filter_for_event`) — apply 시점 live flag 재독이 flag-flip race를 내기
    때문이다(코드리뷰 #5)."""
    return ScopeFilter.COMPANY_PLUS_OWNER if kg_owner_scope_enabled() else ScopeFilter.COMPANY


# owner-inclusive SELECT 술어는 `visibility.owner_inclusive_sql`이 단일 출처다
# (코드리뷰 #10 — write 경계 술어를 read 경계와 한 모듈에서 대조). 아래 `_*_select`가
# 전부 그걸 쓴다.


# --- SoR row 모델 (project 내부 통신 단위 — 원칙 2) -----------------------------


# scope/owner_id는 K7 owner-scope 투영 입력이다. COMPANY 필터(flag off)에서는
# SELECT가 두 컬럼을 고르지 않으므로 _mapping에 키가 없고 — 아래 기본값
# (company/None)이 채워져 build()는 늘 scope/owner_id를 노드/엣지에 emit한다(v2).
class PageRow(BaseModel):
    page_id: UUID
    slug: str
    kind: str  # page | claim | source
    title: str
    confidence: str | None = None
    # claim 노드 사람이 읽는 헤드라인 라벨(nullable). PageRow(**r._mapping) 매핑이
    # 값을 드롭하지 않도록 명시 선언한다. 투영은 저장값을 slug처럼 복사만 한다.
    display_title: str | None = None
    content_hash: str
    project: str
    updated_at: datetime
    scope: str = "company"
    owner_id: UUID | None = None


class DocRow(BaseModel):
    doc_id: UUID
    source: str
    project: str
    title: str | None = None  # 노드 표시 텍스트(본문 아님, 불변식 5) — 그래프/ask 라벨용
    source_db_name: str | None = None
    updated_at: datetime
    scope: str = "company"
    owner_id: UUID | None = None  # documents는 owner_id 컬럼이 없다 — user_id를 label


class FactRow(BaseModel):
    row_id: UUID
    record_type: str
    confidence: str
    source_doc_id: UUID | None = None
    record_key: str | None = None  # 노드 표시 텍스트(본문 아님) — fact의 사람이 읽는 키
    valid_until_raw: str | None = None  # properties->>'valid_until' 원문 — 파싱은 build에서
    updated_at: datetime
    scope: str = "company"
    owner_id: UUID | None = None


class LinkRow(BaseModel):
    src_page_id: UUID
    src_slug: str
    src_kind: str
    dst_slug: str
    rel: str
    src_scope: str = "company"  # src page의 scope/owner — dst 네임스페이스 해석 + 엣지 scope
    src_owner_id: UUID | None = None


class EntityRow(BaseModel):
    """:Entity 투영 입력 — kg_entities SoR(K6). 본문 없음(이름/메타만)."""

    entity_key: str
    entity_kind: str
    name_norm: str
    display_name: str
    first_seen: datetime
    last_seen: datetime
    updated_at: datetime


class MentionRow(BaseModel):
    """MENTIONED_IN/RELATES_TO 투영 입력 — kg_entity_mentions ⋈ kg_entities ⋈
    wiki_pages(company page|claim)에서 join. page_kind로 dst 라벨을 정한다."""

    entity_key: str
    page_id: UUID
    page_kind: str  # page | claim
    slug: str  # mention된 page의 현재 slug (RELATES_TO evidence_slug)


class SourceFront(BaseModel):
    """:WikiSource frontmatter 보강 속성 — PG에 없는 SoR 필드(kg-model §3)."""

    source_type: str
    source_ref: str


class TaskMeta(BaseModel):
    """CONFLICTS_WITH 엣지 속성 재료 — conflict WikiTask frontmatter에서 유도(§4.3)."""

    detected_at: datetime | None = None
    reason: str | None = None
    status: str = "UNRESOLVED"  # UNRESOLVED | RESOLVED


class SourceRows(BaseModel):
    """load_* 출력 컨테이너. `slug_map`/`doc_ids`/`conflict_index`는 증분 load에서도
    전체 네임스페이스를 담는다 — 변경 row의 dst resolve/provenance 검증이 변경분
    밖을 가리킬 수 있어서다."""

    pages: list[PageRow] = Field(default_factory=list)
    docs: list[DocRow] = Field(default_factory=list)
    facts: list[FactRow] = Field(default_factory=list)
    links: list[LinkRow] = Field(default_factory=list)
    entities: list[EntityRow] = Field(default_factory=list)  # K6 — full load만(아래)
    mentions: list[MentionRow] = Field(default_factory=list)  # K6
    source_front: dict[str, SourceFront] = Field(default_factory=dict)  # source slug 기준
    # K8.6 — 키 (namespace, slug_a, slug_b): company task는 ns='company', personal은 owner
    # UUID str. 같은 claim 쌍이 owner별로 분리된 status를 갖는다(_build_conflict_index).
    conflict_index: dict[tuple[str, str, str], TaskMeta] = Field(default_factory=dict)
    # 네임스페이스화한 slug_map(B4): key는 (namespace, slug) — namespace는 company
    # row면 'company', personal row면 owner UUID 문자열(`ns_for`). 같은 slug의
    # company/owner-A/owner-B 노드가 서로 다른 엔트리라, owner-A의 링크가 owner-B의
    # 노드로 해석될 수 없다. value는 (page_id, kind, scope, owner_id).
    slug_map: dict[tuple[str, str], tuple[UUID, str, str, UUID | None]] = Field(
        default_factory=dict
    )
    # doc_id → (scope, owner_id). provenance 엣지가 min-of-endpoints scope/owner(B3)를
    # 계산하려면 dst document의 scope/owner가 필요하다(존재 검증 + 엣지 scope 동시).
    doc_meta: dict[UUID, tuple[str, UUID | None]] = Field(default_factory=dict)
    full: bool = False  # full load일 때만 ExpectedKeys 계산(§4.6 prune 입력)
    warnings: dict[str, int] = Field(default_factory=dict)

    @property
    def doc_ids(self) -> set[UUID]:
        """provenance 존재 검증용 — doc_meta 키 집합(기존 build() 호출부 호환)."""
        return set(self.doc_meta)

    def resolve_dst(
        self, src_scope: str, src_owner_id: UUID | None, dst_slug: str
    ) -> tuple[UUID, str, str, UUID | None] | None:
        """링크 dst를 src 네임스페이스에서 해석한다 — `resolve_namespace` 의미
        (company rows ∪ this-owner personal rows, **never cross-owner**, personal 우선).

        company src는 company 네임스페이스만 본다. personal-A src는 A 우선 →
        없으면 company. owner-B는 절대 보지 않는다(B4 cross-owner 격리)."""
        ns = ns_for(src_scope, src_owner_id)
        if ns != COMPANY_NS:
            hit = self.slug_map.get((ns, dst_slug))
            if hit is not None:
                return hit
        return self.slug_map.get((COMPANY_NS, dst_slug))


class ExpectedKeys(BaseModel):
    """rebuild prune 입력 — SoR에 존재해야 하는 키 전수(§4.6).

    wiki 키는 라벨별로 분리한다 — `_upsert_page_row`가 기존 row의 kind를
    UPDATE할 수 있어(같은 slug 정체성), 합집합 set이면 kind가 바뀐 page_id의
    구 라벨 노드를 prune이 영원히 지우지 못한다(K2 리뷰 교정, 2026-06-12).
    """

    page_ids_by_label: dict[str, set[str]] = Field(default_factory=dict)  # Label.value 기준
    placeholder_slugs: set[str] = Field(default_factory=set)
    doc_ids: set[str] = Field(default_factory=set)
    row_ids: set[str] = Field(default_factory=set)
    entity_keys: set[str] = Field(default_factory=set)  # K6 — :Entity prune(검토 BLOCKER)
    edge_triples: set[tuple[str, str, str]] = Field(default_factory=set)


class KgProjectionPlan(BaseModel):
    nodes: list[KgNodeRow] = Field(default_factory=list)
    edges: list[KgEdgeRow] = Field(default_factory=list)
    expected_keys: ExpectedKeys | None = None
    counts: dict[str, int] = Field(default_factory=dict)


# --- SoR SELECT (§4.2 — 전부 scope 필터가 WHERE에 박힌다) ------------------------


def load_all(*, scope_filter: ScopeFilter) -> SourceRows:
    return _load(since=None, scope_filter=scope_filter)


def load_changed(since: datetime, *, scope_filter: ScopeFilter) -> SourceRows:
    return _load(since=since, scope_filter=scope_filter)


def scope_filter_for_event(scope: str) -> ScopeFilter:
    """outbox 이벤트의 **저장된** scope에서 reload 필터를 유도한다(코드리뷰 #5).

    personal 이벤트는 enqueue 시점에 owner-scope flag가 켜져 있었을 때만 적재된다
    (`outbox.enqueue_kg_event`). 따라서 그 이벤트는 owner-inclusive로 다시 읽어야 노드가
    실제로 안착한다 — apply 시점의 **live flag**를 다시 읽으면(`active_scope_filter`),
    enqueue-while-on → drain-while-off로 flag가 뒤집힌 사이 personal row가 COMPANY 필터에
    걸러져 빈 reload→no-op upsert가 되고, 이벤트는 applied로 소비돼 노드가 영구 누락된다.
    이벤트가 들고 있는 scope를 권위로 삼아 그 race를 닫는다. company 이벤트는 COMPANY로
    유지(flag-off에서 v1 byte-identical reload). flag-off에서 personal 노드를 비우려면 그건
    rebuild가 한다(deactivation runbook), apply 경로가 아니다."""
    return ScopeFilter.COMPANY_PLUS_OWNER if scope == "personal" else ScopeFilter.COMPANY


class LoadOneCache:
    """`run_once` batch 1회 동안 공유하는 read-only 스냅샷 캐시(O(corpus) per-event 제거).

    `load_one`은 이벤트마다 전 company slug-map(~9천 row)과 doc-meta를 다시 읽고,
    wiki_page upsert마다 전 task 파일을 glob/parse해 conflict index를 짓는다 —
    apply_one이 이벤트 단위라(outbox.run_once 루프) batch당 O(corpus × 이벤트 수)다.
    같은 batch 안에서는 SoR 스냅샷이 안정적이므로(짧은 생명주기, 다음 poll에 갱신)
    scope_filter별로 한 번만 로드해 재사용한다.

    conflict index는 **lazy**다 — conflict 링크가 실제로 있는 이벤트에서만 짓는다
    (대부분의 wiki_page는 conflict가 없어 전사 task glob이 순수 낭비였다). cache가
    없으면(직접 호출/테스트) load_one은 종전대로 매 호출 로드한다."""

    def __init__(self, scope_filter: ScopeFilter) -> None:
        self.scope_filter = scope_filter
        self._slug_map: dict[tuple[str, str], tuple[UUID, str, str, UUID | None]] | None = None
        self._doc_meta: dict[UUID, tuple[str, UUID | None]] | None = None
        self._conflict_index: dict[tuple[str, str, str], TaskMeta] | None = None

    def slug_map(self, s) -> dict[tuple[str, str], tuple[UUID, str, str, UUID | None]]:
        if self._slug_map is None:
            self._slug_map = _load_slug_map(s, self.scope_filter)
        return self._slug_map

    def doc_meta(self, s) -> dict[UUID, tuple[str, UUID | None]]:
        if self._doc_meta is None:
            self._doc_meta = _load_doc_meta(s, self.scope_filter)
        return self._doc_meta

    def conflict_index(
        self, warnings: dict[str, int], *, s: Any = None
    ) -> dict[tuple[str, str, str], TaskMeta]:
        if self._conflict_index is None:
            self._conflict_index = _build_conflict_index(
                warnings, scope_filter=self.scope_filter, s=s
            )
        return self._conflict_index


def _links_have_conflict(links: list[LinkRow]) -> bool:
    return any(link.rel == "conflicts" for link in links)


def load_one(
    entity_kind: str,
    entity_id: UUID,
    *,
    scope_filter: ScopeFilter | None = None,
    cache: "LoadOneCache | None" = None,
) -> SourceRows:
    """K3 outbox 단일 **upsert** 이벤트 재투영용 — batch와 같은 build()를 지난다(§5.4).

    **control-flow inversion(§2):** scope는 이제 입력 인자가 아니라 reload의
    **출력**이다 — owner-inclusive 필터로 row를 다시 읽고 그 row가 들고 있는
    scope/owner_id를 그대로 키로 쓴다(`PageRow.scope` 등).

    `scope_filter`는 호출자가 명시한다 — outbox worker는 **이벤트의 저장된 scope**에서
    유도한 필터를 넘겨(`scope_filter_for_event`) flag-flip race를 닫는다(코드리뷰 #5).
    None이면 live `active_scope_filter()`로 폴백한다(직접 호출/테스트 편의). flag가 꺼져
    있고 폴백이면 COMPANY라 company row만 통과한다(K7 이전과 동일).

    `cache`는 outbox worker가 `run_once` batch 1회 동안 넘기는 공유 스냅샷이다 —
    slug_map/doc_meta를 batch당 한 번만 읽고, conflict index를 conflict 링크가 있는
    이벤트에서만 lazy로 짓는다(O(corpus) per-event 제거). None이면 종전대로 매 호출
    로드한다(직접 호출/테스트 편의). cache의 scope_filter는 이 호출의 scope_filter와
    일치해야 한다(worker가 보장 — 둘 다 이벤트 저장 scope에서 유도).

    **delete 경로는 이 reload를 재사용하지 않는다(§2 dual):** delete 시 PG row는 이미
    없어 reload가 비고 scope를 잃는다. exact-scope DELETE는 **enqueued 이벤트의**
    scope/owner_id(입력)를 쓰며 `outbox._apply_change`가 담당한다 — 이 함수는 upsert
    전용이다."""
    if scope_filter is None:
        scope_filter = active_scope_filter()
    rows = SourceRows(full=False)
    with session() as s:
        rows.slug_map = cache.slug_map(s) if cache is not None else _load_slug_map(s, scope_filter)
        rows.doc_meta = cache.doc_meta(s) if cache is not None else _load_doc_meta(s, scope_filter)
        if entity_kind == "wiki_page":
            page = s.execute(
                _page_select(scope_filter).where(wiki_pages.c.page_id == entity_id)
            ).first()
            if page is not None:
                rows.pages = [PageRow(**page._mapping)]
                rows.links = _load_links(s, scope_filter, src_page_ids={entity_id})
                rows.source_front = _load_source_front(rows.pages, rows.warnings)
                # conflict index는 conflict 링크가 있는 page 이벤트에서만 짓는다
                # (대부분 page는 conflict가 없어 전사 task glob이 순수 낭비였다).
                # build()는 CONFLICTS_WITH 엣지에서만 이 인덱스를 lookup하므로 conflict
                # 링크가 없으면 빈 인덱스로도 결과가 동일하다.
                if _links_have_conflict(rows.links):
                    rows.conflict_index = (
                        cache.conflict_index(rows.warnings, s=s)
                        if cache is not None
                        else _build_conflict_index(rows.warnings, scope_filter=scope_filter, s=s)
                    )
            else:
                # kind='task' row일 수 있다 — 노드로 투영하지 않지만(§4.2),
                # conflict task면 related 쌍의 CONFLICTS_WITH 엣지 속성 재투영
                # 재료를 싣는다(§5.4: 노드 없이 conflict index 갱신 → 속성 재SET).
                # 엣지 존재는 여전히 wiki_links가 결정한다.
                # K8.6 — task row를 owner-inclusive scope로 조회해 company task **또는**
                # owner-scope일 때 personal task(resolve→outbox 신선도)를 다룬다. load_task는
                # 그 row의 scope/owner로 호출하고, 인덱스는 scope_filter 전체로 빌드해
                # emit이 ns로 lookup한다(company/owner 분리 status).
                # task row는 `_page_select`로 못 가져온다 — 그 select은
                # `kind IN (page,claim,source)`(=`_WIKI_NODE_KINDS`, task 제외)라
                # `kind=='task'`와 AND하면 항상 공집합이다(K8 회귀). task row를 직접
                # 조회하되 scope만 owner-inclusive로 건다(company task 또는 owner-scope
                # personal task). slug/scope/owner_id만 필요하다(노드 미투영, §4.2).
                task_scope_where = (
                    (wiki_pages.c.scope == "company")
                    if scope_filter is ScopeFilter.COMPANY
                    else owner_inclusive_sql(wiki_pages.c.scope, wiki_pages.c.owner_id)
                )
                task_row = s.execute(
                    select(wiki_pages.c.slug, wiki_pages.c.scope, wiki_pages.c.owner_id).where(
                        wiki_pages.c.page_id == entity_id,
                        wiki_pages.c.kind == "task",
                        task_scope_where,
                    )
                ).first()
                if task_row is not None:
                    task = wiki_store.load_task(
                        task_row.slug, scope=task_row.scope, owner_id=task_row.owner_id
                    )
                    # related가 비면 _load_links의 조건절이 전부 빠져 전사 링크를
                    # 로드한다 — batch 경로(_load)와 같은 비-빈 가드가 계약이다
                    # (리뷰 교정; 빈 related conflict task 이벤트는 no-op).
                    if task is not None and task.kind == "conflict" and task.related:
                        rows.links = _load_links(s, scope_filter, conflict_slugs=set(task.related))
                        rows.conflict_index = (
                            cache.conflict_index(rows.warnings, s=s)
                            if cache is not None
                            else _build_conflict_index(
                                rows.warnings, scope_filter=scope_filter, s=s
                            )
                        )
        elif entity_kind == "document":
            doc = s.execute(
                _doc_select(scope_filter).where(documents.c.doc_id == entity_id)
            ).first()
            if doc is not None:
                rows.docs = [DocRow(**doc._mapping)]
        elif entity_kind == "structured_row":
            fact = s.execute(
                _fact_select(scope_filter).where(structured_rows.c.row_id == entity_id)
            ).first()
            if fact is not None:
                rows.facts = [FactRow(**fact._mapping)]
        else:
            raise ValueError(f"unknown entity_kind: {entity_kind}")
    return rows


def _page_select(scope_filter: ScopeFilter):
    cols = [
        wiki_pages.c.page_id,
        wiki_pages.c.slug,
        wiki_pages.c.kind,
        wiki_pages.c.title,
        wiki_pages.c.confidence,
        # claim 노드 라벨용 헤드라인(nullable). COMPANY/owner 양 변형이 이 cols를
        # 공유하므로 한 곳 추가로 둘 다 적용된다. 문자열은 snapshot 마커
        # ("owner_id"/"IS NOT NULL") 미포함이라 flag-off snapshot 테스트 무회귀.
        wiki_pages.c.display_title,
        wiki_pages.c.content_hash,
        wiki_pages.c.project,
        wiki_pages.c.updated_at,
    ]
    if scope_filter is ScopeFilter.COMPANY:
        # v1 byte-identical — scope/owner_id 컬럼 미선택(§1.5 snapshot).
        return select(*cols).where(
            wiki_pages.c.scope == "company", wiki_pages.c.kind.in_(_WIKI_NODE_KINDS)
        )
    scope_col, owner_col = owner_scope_columns(wiki_pages)
    return select(*cols, scope_col, owner_col).where(
        owner_inclusive_sql(scope_col, owner_col),
        wiki_pages.c.kind.in_(_WIKI_NODE_KINDS),
    )


def _doc_select(scope_filter: ScopeFilter):
    cols = [
        documents.c.doc_id,
        documents.c.source,
        documents.c.project,
        documents.c.title,
        documents.c.source_db_name,
        documents.c.updated_at,
    ]
    if scope_filter is ScopeFilter.COMPANY:
        return select(*cols).where(documents.c.scope == "company")
    # documents에는 owner_id 컬럼이 없다 — owner = user_id(kg-model §3). 단 user_id는
    # NOT NULL이라 그대로 라벨링하면 company 문서도 owner_id가 채워져 'company→owner_id
    # NULL' 불변이 깨진다 → SoT 헬퍼가 scope='personal'일 때만 user_id, company면 NULL(CASE).
    # owner_expr(CASE)는 SELECT 컬럼 전용이다 — WHERE 술어는 raw user_id를 그대로 넘겨야
    # flag-off 스냅샷과 byte-identical(CASE를 WHERE에 넣으면 IS NOT NULL 형태가 달라짐).
    scope_col, owner_expr = owner_scope_columns(documents)
    return select(*cols, scope_col, owner_expr).where(
        owner_inclusive_sql(scope_col, documents.c.user_id)
    )


def _fact_select(scope_filter: ScopeFilter):
    cols = [
        structured_rows.c.row_id,
        structured_rows.c.record_type,
        structured_rows.c.confidence,
        structured_rows.c.source_doc_id,
        structured_rows.c.record_key,
        structured_rows.c.properties["valid_until"].astext.label("valid_until_raw"),
        structured_rows.c.updated_at,
    ]
    if scope_filter is ScopeFilter.COMPANY:
        return select(*cols).where(structured_rows.c.scope == "company")
    scope_col, owner_col = owner_scope_columns(structured_rows)
    return select(*cols, scope_col, owner_col).where(owner_inclusive_sql(scope_col, owner_col))


def _entity_select(scope: str):
    return (
        select(
            kg_entities.c.entity_key,
            kg_entities.c.entity_kind,
            kg_entities.c.name_norm,
            kg_entities.c.display_name,
            kg_entities.c.first_seen,
            kg_entities.c.last_seen,
            kg_entities.c.updated_at,
        )
        .where(kg_entities.c.scope == scope)
        .order_by(kg_entities.c.entity_key)
    )


def _load_mentions(s, scope: str) -> list[MentionRow]:
    """company page|claim에 걸린 mention만 — 양쪽 `scope='company'` join 필터로
    mixed-scope 누출을 막는다(검토 B3). evidence_slug는 page의 현재 slug."""
    q = (
        select(
            kg_entities.c.entity_key,
            kg_entity_mentions.c.page_id,
            wiki_pages.c.kind.label("page_kind"),
            wiki_pages.c.slug,
        )
        .join(kg_entities, kg_entities.c.entity_id == kg_entity_mentions.c.entity_id)
        .join(wiki_pages, wiki_pages.c.page_id == kg_entity_mentions.c.page_id)
        .where(
            kg_entities.c.scope == scope,
            wiki_pages.c.scope == scope,
            wiki_pages.c.kind.in_(_MENTION_PAGE_KINDS),
        )
        .order_by(kg_entity_mentions.c.entity_id, kg_entity_mentions.c.page_id)
    )
    return [MentionRow(**r._mapping) for r in s.execute(q).all()]


def _load_slug_map(
    s, scope_filter: ScopeFilter
) -> dict[tuple[str, str], tuple[UUID, str, str, UUID | None]]:
    """네임스페이스화한 slug → 노드 맵(B4). COMPANY 필터는 v1 byte-identical 질의
    (scope/owner_id 미선택)이고, 모든 엔트리가 'company' 네임스페이스다."""
    if scope_filter is ScopeFilter.COMPANY:
        rows = s.execute(
            select(wiki_pages.c.slug, wiki_pages.c.page_id, wiki_pages.c.kind).where(
                wiki_pages.c.scope == "company", wiki_pages.c.kind.in_(_WIKI_NODE_KINDS)
            )
        ).all()
        return {(COMPANY_NS, r.slug): (r.page_id, r.kind, "company", None) for r in rows}
    scope_col, owner_col = owner_scope_columns(wiki_pages)
    rows = s.execute(
        select(
            wiki_pages.c.slug,
            wiki_pages.c.page_id,
            wiki_pages.c.kind,
            scope_col,
            owner_col,
        ).where(
            owner_inclusive_sql(scope_col, owner_col),
            wiki_pages.c.kind.in_(_WIKI_NODE_KINDS),
        )
    ).all()
    return {
        (ns_for(r.scope, r.owner_id), r.slug): (r.page_id, r.kind, r.scope, r.owner_id)
        for r in rows
    }


def _load_doc_meta(s, scope_filter: ScopeFilter) -> dict[UUID, tuple[str, UUID | None]]:
    """doc_id → (scope, owner_id). COMPANY 필터는 v1 byte-identical(doc_id만) 질의이고
    모든 엔트리가 (company, None)이다. owner 필터는 user_id를 owner로 읽는다.

    SoT 헬퍼(owner_scope_columns)는 SELECT-column 판(_doc_select)이다. 여기는 raw user_id를
    SELECT해 Python에서 후처리(r.user_id)하므로 의도적 미통일 — CASE label을 넣으면 결과
    컬럼명이 owner_id로 바뀌어 r.user_id 접근이 깨진다(company doc → owner NULL 규칙은 동일).
    """
    if scope_filter is ScopeFilter.COMPANY:
        return {
            r[0]: ("company", None)
            for r in s.execute(select(documents.c.doc_id).where(documents.c.scope == "company"))
        }
    rows = s.execute(
        select(documents.c.doc_id, documents.c.scope, documents.c.user_id).where(
            owner_inclusive_sql(documents.c.scope, documents.c.user_id)
        )
    ).all()
    # company 문서는 owner NULL(위 _doc_select와 동일 불변) — user_id를 owner로 보지 않는다.
    return {r.doc_id: (r.scope, r.user_id if r.scope == "personal" else None) for r in rows}


def _load_links(
    s,
    scope_filter: ScopeFilter,
    *,
    src_page_ids: set[UUID] | None = None,
    conflict_slugs: set[str] | None = None,
) -> list[LinkRow]:
    """src page join으로 scope를 필터한다 — wiki_links에는 scope 컬럼이 없다
    (kg-model §3). `src_page_ids`는 증분(변경 src page 한정), `conflict_slugs`는
    변경 conflict task의 related 쌍에 해당하는 conflicts 엣지 속성 재투영용이다.
    owner 필터일 때만 src_scope/src_owner_id를 함께 골라 dst 네임스페이스 해석과
    엣지 scope(B3)에 쓴다 — COMPANY 필터는 v1 byte-identical."""
    base_cols = [
        wiki_links.c.src_page_id,
        wiki_pages.c.slug.label("src_slug"),
        wiki_pages.c.kind.label("src_kind"),
        wiki_links.c.dst_slug,
        wiki_links.c.rel,
    ]
    if scope_filter is ScopeFilter.COMPANY:
        cols = base_cols
        src_scope_where = wiki_pages.c.scope == "company"
    else:
        cols = [
            *base_cols,
            wiki_pages.c.scope.label("src_scope"),
            wiki_pages.c.owner_id.label("src_owner_id"),
        ]
        src_scope_where = owner_inclusive_sql(wiki_pages.c.scope, wiki_pages.c.owner_id)
    q = (
        select(*cols)
        .join(wiki_pages, wiki_pages.c.page_id == wiki_links.c.src_page_id)
        .where(src_scope_where, wiki_pages.c.kind.in_(_WIKI_NODE_KINDS))
    )
    conds = []
    if src_page_ids is not None:
        conds.append(wiki_links.c.src_page_id.in_(src_page_ids))
    if conflict_slugs:
        conds.append(
            (wiki_links.c.rel == "conflicts")
            & (wiki_pages.c.slug.in_(conflict_slugs) | wiki_links.c.dst_slug.in_(conflict_slugs))
        )
    if conds:
        cond = conds[0]
        for extra in conds[1:]:
            cond = cond | extra
        q = q.where(cond)
    return [LinkRow(**r._mapping) for r in s.execute(q).all()]


def _load_source_front(pages: list[PageRow], warnings: dict[str, int]) -> dict[str, SourceFront]:
    """:WikiSource의 source_type/source_ref는 PG에 없다 → frontmatter join(§4.2).
    로드 실패는 보강 속성 생략 + warning 카운트 — SoR 불일치를 수리하지 않는다."""
    front: dict[str, SourceFront] = {}
    for page in pages:
        if page.kind != "source":
            continue
        if page.scope != ScopeFilter.COMPANY.value:
            # personal source frontmatter 적재는 owner 컨텍스트가 필요해 K7.1에서 미정의.
            # company store를 잘못 읽으면(같은 slug의 company source) personal source가
            # 엉뚱한 doc로 provenance 엣지를 만든다 → 읽지 않고 보강 생략(노드는 투영,
            # source_type/source_ref=None, provenance 엣지 없음 — 정직한 deferral).
            warnings["personal_source_front_deferred"] = (
                warnings.get("personal_source_front_deferred", 0) + 1
            )
            continue
        model = wiki_store.load_source(page.slug, scope=ScopeFilter.COMPANY.value)
        if model is None:
            warnings["frontmatter_missing"] = warnings.get("frontmatter_missing", 0) + 1
            continue
        front[page.slug] = SourceFront(source_type=model.source_type, source_ref=model.source_ref)
    return front


def _distinct_personal_task_owners(s: Any) -> list[UUID]:
    """K8.6 — personal conflict task를 가진 distinct owner_id. owner-scope 투영에서만 호출."""
    rows = s.execute(
        select(wiki_pages.c.owner_id)
        .where(
            wiki_pages.c.scope == "personal",
            wiki_pages.c.kind == "task",
            wiki_pages.c.owner_id.isnot(None),
        )
        .distinct()
    ).all()
    return [r[0] for r in rows]


def _build_conflict_index(
    warnings: dict[str, int],
    *,
    scope_filter: ScopeFilter = ScopeFilter.COMPANY,
    s: Any = None,
) -> dict[tuple[str, str, str], TaskMeta]:
    """§4.3 + K8.6 — conflict WikiTask frontmatter가 SoR(PG row는 인덱스). 키는
    `(namespace, slug_a, slug_b)`: company task는 ns='company', owner-scope에서는 personal
    task도 owner UUID 문자열 ns로 적재한다 — 같은 claim 쌍이 company/owner별로 **분리된**
    status를 갖는다(emit_edge가 `ns_for(src)`로 lookup). 같은 (ns, claim 쌍)에 task가 복수면
    최신 created_at 우선. related에 claim 아닌 slug가 섞여도 무해 — 엣지 존재는 wiki_links가
    결정하고 이 인덱스는 속성 lookup일 뿐이다.

    company-scope-only(flag-off scope_filter=COMPANY)는 K8.6 이전과 동일한 company 인덱스다
    (키만 ns 접두 추가 — emit도 company ns로 lookup하므로 동작 불변). owner enumeration은
    `scope_filter=COMPANY_PLUS_OWNER`일 때만, distinct personal-task owner마다 1회 glob.
    `s`(선택): owner enumeration 쿼리용 — None이면 자체 세션을 연다(직접 호출/테스트 편의)."""
    index: dict[tuple[str, str, str], TaskMeta] = {}
    latest: dict[tuple[str, str, str], datetime] = {}

    def _ingest(task_scope: str, owner_id: UUID | None) -> None:
        ns = ns_for(task_scope, owner_id)
        for task_slug in wiki_store.list_slugs("task", scope=task_scope, owner_id=owner_id):
            task = wiki_store.load_task(task_slug, scope=task_scope, owner_id=owner_id)
            if task is None:
                warnings["frontmatter_missing"] = warnings.get("frontmatter_missing", 0) + 1
                continue
            if task.kind != "conflict":
                continue
            meta = TaskMeta(
                detected_at=task.created_at,
                reason=task.description,
                status="RESOLVED" if task.resolved else "UNRESOLVED",
            )
            for a, b in itertools.combinations(sorted(set(task.related)), 2):
                key = (ns, a, b)
                if key not in latest or task.created_at > latest[key]:
                    latest[key] = task.created_at
                    index[key] = meta

    _ingest(ScopeFilter.COMPANY.value, None)
    if scope_filter is ScopeFilter.COMPANY_PLUS_OWNER:
        if s is not None:
            owners = _distinct_personal_task_owners(s)
        else:
            with session() as own_s:
                owners = _distinct_personal_task_owners(own_s)
        for owner_id in owners:
            _ingest("personal", owner_id)
    return index


def _load(*, since: datetime | None, scope_filter: ScopeFilter) -> SourceRows:
    company = ScopeFilter.COMPANY.value
    rows = SourceRows(full=since is None)
    with session() as s:
        page_q = _page_select(scope_filter)
        doc_q = _doc_select(scope_filter)
        fact_q = _fact_select(scope_filter)
        if since is not None:
            page_q = page_q.where(wiki_pages.c.updated_at > since)
            doc_q = doc_q.where(documents.c.updated_at > since)
            fact_q = fact_q.where(structured_rows.c.updated_at > since)
        rows.pages = [PageRow(**r._mapping) for r in s.execute(page_q).all()]
        rows.docs = [DocRow(**r._mapping) for r in s.execute(doc_q).all()]
        rows.facts = [FactRow(**r._mapping) for r in s.execute(fact_q).all()]
        rows.slug_map = _load_slug_map(s, scope_filter)
        rows.doc_meta = _load_doc_meta(s, scope_filter)

        if since is None:
            rows.links = _load_links(s, scope_filter)
        else:
            # 변경 src page의 링크 + 변경 conflict task의 related 쌍에 해당하는
            # conflicts 엣지(속성 재투영 — 엣지 존재는 여전히 wiki_links가 결정).
            changed_page_ids = {p.page_id for p in rows.pages}
            # K8.6 — conflict task changed slugs, owner-inclusive(scope_filter). company task는
            # ns='company', owner-scope면 personal task도 각 owner ns로(_build_conflict_index가
            # 분리 status). 각 task를 그 row의 scope/owner로 로드한다. conflict_slugs는 ns 무관
            # 합집합 — _load_links가 그 slug에 닿는 conflicts 엣지를 끌어오고 status는 emit이 ns로
            # 분리 lookup. (rebuild가 삭제 수렴 권위지만 sync도 personal conflict 변경을 반영.)
            if scope_filter is ScopeFilter.COMPANY:
                changed_tasks = [
                    (r[0], company, None)
                    for r in s.execute(
                        select(wiki_pages.c.slug).where(
                            wiki_pages.c.scope == company,
                            wiki_pages.c.kind == "task",
                            wiki_pages.c.updated_at > since,
                        )
                    )
                ]
            else:
                changed_tasks = [
                    (r.slug, r.scope, r.owner_id)
                    for r in s.execute(
                        select(wiki_pages.c.slug, wiki_pages.c.scope, wiki_pages.c.owner_id).where(
                            owner_inclusive_sql(wiki_pages.c.scope, wiki_pages.c.owner_id),
                            wiki_pages.c.kind == "task",
                            wiki_pages.c.updated_at > since,
                        )
                    )
                ]
            conflict_slugs: set[str] = set()
            for task_slug, t_scope, t_owner in changed_tasks:
                task = wiki_store.load_task(task_slug, scope=t_scope, owner_id=t_owner)
                if task is not None and task.kind == "conflict":
                    conflict_slugs |= set(task.related)
            if changed_page_ids or conflict_slugs:
                rows.links = _load_links(
                    s, scope_filter, src_page_ids=changed_page_ids, conflict_slugs=conflict_slugs
                )

        # K6 entity sublayer — **full rebuild에서만**, **company-only(D2)** 투영한다.
        # co-mention RELATES_TO는 page 전체 mention을 봐야 결정론적이라, 변경분만 보는
        # incremental에선 부분 투영이 된다 → kg-sync는 entity를 건드리지 않고
        # 다음 full rebuild가 권위로 수렴시킨다(entity는 substrate-only·batch-only,
        # kg-model §2 / 구현 명세 §9.1). owner-scope에서도 entity_key에 owner 성분이
        # 없어 동명 cross-owner MERGE가 경계를 깨므로 company-only를 유지한다(D2).
        if since is None:
            rows.entities = [
                EntityRow(**r._mapping) for r in s.execute(_entity_select(company)).all()
            ]
            rows.mentions = _load_mentions(s, company)

    rows.source_front = _load_source_front(rows.pages, rows.warnings)
    with session() as s:
        rows.conflict_index = _build_conflict_index(rows.warnings, scope_filter=scope_filter, s=s)
    return rows


def pg_now() -> datetime:
    """watermark 후보 — projection 스냅샷 시작 시각은 호스트 시계가 아니라
    PG `now()`다(§4.7: `updated_at`도 PG 시계이므로 같은 시계로 비교한다)."""
    with session() as s:
        return s.execute(select(func.now())).scalar_one()


# --- build: SourceRows → KgProjectionPlan (순수 결정론) --------------------------


def _parse_valid_until(raw: str | None, warnings: dict[str, int]) -> datetime | None:
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        warnings["valid_until_parse_failed"] = warnings.get("valid_until_parse_failed", 0) + 1
        return None  # 발명 금지(§4.9)


def _node_key(label: Label, merge_value: str) -> str:
    return merge_value


def _owner_prop(owner_id: UUID | None) -> str | None:
    """노드/엣지 props에 싣는 owner_id 표현 — UUID 문자열 또는 None(company).

    `$caller`(K7.2 게이트)도 같은 문자열로 바인딩되므로 `n.owner_id = $caller`가
    성립한다. wire에는 절대 싣지 않는다(§1.2) — 이건 그래프 내부 술어용이다."""
    return str(owner_id) if owner_id is not None else None


def _edge_scope_owner(
    s1: str, o1: UUID | None, s2: str, o2: UUID | None
) -> tuple[str, UUID | None]:
    """엣지 scope/owner = **더 제한적인 끝점**(personal이 이김, B3). 어느 끝점도
    personal이면 엣지는 personal이고 owner는 그 personal 끝점의 owner다.

    cross-owner 엣지는 구조적으로 생기지 않는다(B4 — personal-A 링크는 company이거나
    A로만 해석된다). 따라서 두 끝점이 모두 personal이면 같은 owner이고, 아래 분기는
    먼저 만난 personal owner를 반환해도 안전하다."""
    if s1 == "personal":
        return "personal", o1
    if s2 == "personal":
        return "personal", o2
    return "company", None


# provenance owner-namespace 가드(B4)는 `visibility.doc_visible_to`가 단일 출처다
# (코드리뷰 #10 — slug resolve_dst와 같은 규칙을 한 모듈에서 함께 본다).


def _source_front_for(rows: "SourceRows", page: PageRow) -> SourceFront | None:
    """:WikiSource 페이지의 frontmatter 보강을 **company 페이지에만** 매핑한다(코드리뷰 #2).

    `source_front`는 `_load_source_front`가 company source만 적재해 bare slug로 키잉한
    맵이다(personal source frontmatter는 owner 컨텍스트 미정의라 K7.1에서 deferral). personal
    source 페이지에 대해 bare slug로 lookup하면 같은 slug의 **company** 항목을 집어 personal
    노드에 company source_type/source_ref가 박히고 엉뚱한 company doc로 provenance 엣지가 나간다
    (`doc_visible_to`는 company doc을 모두에게 visible로 봐 못 막는다). 그래서 company 페이지일
    때만 lookup하고 personal은 None으로 정직하게 비운다(B4)."""
    if page.scope != COMPANY_NS:
        return None
    return rows.source_front.get(page.slug)


def build(rows: SourceRows) -> KgProjectionPlan:
    warnings = dict(rows.warnings)
    nodes: list[KgNodeRow] = []
    edges: list[KgEdgeRow] = []
    expected = ExpectedKeys() if rows.full else None

    def emit_edge(
        rel: Rel,
        src_label: Label,
        src: tuple[str, str],
        dst_label: Label,
        dst: tuple[str, str],
        s1: str,
        o1: UUID | None,
        s2: str,
        o2: UUID | None,
        props: dict[str, Any] | None = None,
    ) -> None:
        """모든 엣지를 단일 지점으로 통과시켜 scope/owner_id(B1/B3)를 빠짐없이 SET.

        scope/owner는 더 제한적인 끝점에서 온다(`_edge_scope_owner`). owner predicate가
        relationship에도 걸리므로(K7.2 path 템플릿) 누락 시 owner 자신의 personal
        엣지가 조용히 탈락하거나(property-2 실패) rel-predicate가 vacuous-green이 된다.

        끝점 scope는 반드시 'company'|'personal'이어야 한다(코드리뷰): garbage scope는
        `_edge_scope_owner`에서 조용히 company로 수렴해 personal 엣지를 모두에게 노출할 수
        있다. 새 끝점 라벨이 잘못된 scope를 넘기면 read 시 누수 대신 projection(rebuild) 때
        바로 터지게 한다(no-RLS 경계는 모든 끝점 scope 정확성에 의존)."""
        if s1 not in ("company", "personal") or s2 not in ("company", "personal"):
            raise ValueError(f"emit_edge: invalid endpoint scope {s1!r}/{s2!r} for {rel}")
        escope, eowner = _edge_scope_owner(s1, o1, s2, o2)
        p = dict(props or {})
        p["scope"] = escope
        p["owner_id"] = _owner_prop(eowner)
        edges.append(
            KgEdgeRow(rel=rel, src_label=src_label, src=src, dst_label=dst_label, dst=dst, props=p)
        )

    def emit_node(
        label: Label,
        merge_key: str,
        merge_value: str,
        scope: str,
        owner: UUID | None,
        props: dict[str, Any] | None = None,
    ) -> None:
        """모든 노드를 단일 지점으로 통과시켜 scope/owner_id(B1)를 빠짐없이 SET — `emit_edge`의
        노드 짝(코드리뷰 #4). 이전엔 6개 노드 사이트가 scope/owner_id를 손으로 props에 박아,
        새 노드 타입이 술어 없이 추가되면 RLS 없는 그래프에서 조용히 모두에게 보이거나(잘못된
        scope) owner predicate 탈락으로 불가시가 됐다. 이 funnel을 거치면 노드 타입을 추가해도
        scope/owner_id 스탬프를 빠뜨릴 수 없다(`owner`는 raw UUID — `_owner_prop`가 여기서
        문자열화). caller props에 scope/owner_id를 또 넣지 않는다(여기가 권위).

        scope는 반드시 'company'|'personal'이어야 한다(코드리뷰): _scope_of의 read-side
        기본값은 scope 부재 노드를 company(가시)로 취급하므로, 새 라벨이 scope를 빠뜨리거나
        잘못 넘기면 personal 노드가 조용히 모두에게 노출된다. 그 누수를 read 시점이 아니라
        projection(rebuild) 때 즉시 터뜨린다(no-RLS 경계는 라벨마다 정확한 scope에 의존)."""
        if scope not in ("company", "personal"):
            raise ValueError(f"emit_node: invalid scope {scope!r} for {label}")
        p = dict(props or {})
        p["scope"] = scope
        p["owner_id"] = _owner_prop(owner)
        nodes.append(KgNodeRow(label=label, merge_key=merge_key, merge_value=merge_value, props=p))

    # :Project 허브 — enum 고정 4행(§4.2). company-global이라 scope=company/owner=null
    # (owner predicate가 허브 노드에 매칭되려면 scope 속성 필요, B1). prune은 PROJECTS
    # 상수를 권위로 쓰므로 ExpectedKeys에 별도 필드를 두지 않는다.
    for name in PROJECTS:
        emit_node(Label.PROJECT, "name", name, "company", None)

    # wiki 노드 3종 — kind → 라벨 분배. last_accessed_at은 props에 넣지 않는다
    # (읽기 경로 관측 속성 보존 — §4.2). scope/owner_id는 emit_node가 일괄 SET
    # (B1 — owner predicate가 모든 bound 노드에 걸린다).
    for page in rows.pages:
        label = WIKI_KIND_LABEL_MAP[page.kind]
        pid = str(page.page_id)
        p_scope = page.scope
        p_owner = page.owner_id
        props: dict[str, Any] = {
            "slug": page.slug,
            # 노드 라벨은 display_title(있으면) → title 폴백으로 일반화한다. claim만
            # display_title이 채워져 slug=title이던 문제가 풀리고, page/source는
            # display_title=NULL이라 page.title로 떨어져 기존 동작이 불변이다 — 노드
            # 종류별 특수분기 없이 한 규칙으로. LLM 0회(저장값 복사만, 투영 결정론).
            "title": page.display_title or page.title,
            "confidence": page.confidence,
            "content_hash": page.content_hash,  # diff 키(kg-model §3)
        }
        if label is Label.WIKI_PAGE:
            props.update(
                {
                    # placeholder 승격 매칭 키(§2) — 실체 page도 ns_slug를 들고 있어야
                    # store.promote_placeholders가 같은 네임스페이스 placeholder를
                    # 정확히 찾아 승격한다. page_id NOT NULL이라 stale-placeholder
                    # 카운터(page_id NULL AND ns_slug NULL)에는 잡히지 않는다.
                    "ns_slug": ns_key(p_scope, p_owner, page.slug),
                    "project": page.project,
                    "updated_at": page.updated_at,
                    "materialized": True,
                }
            )
        if label is Label.WIKI_SOURCE:
            front = _source_front_for(rows, page)
            # 키를 항상 포함한다 — 생략하면 Neo4j `SET +=`가 옛 값을 못 지워
            # frontmatter가 사라진 노드의 stale source_ref가 rebuild로도 안
            # 지워진다(None은 속성 삭제로 동작, K2 리뷰 교정 2026-06-12).
            props.update(
                {
                    "source_type": front.source_type if front else None,
                    "source_ref": front.source_ref if front else None,
                }
            )
        emit_node(label, "page_id", pid, p_scope, p_owner, props)
        if expected is not None:
            expected.page_ids_by_label.setdefault(label.value, set()).add(pid)

        # IN_PROJECT — wiki page만(§4.2). dst는 company-global Project 허브라 엣지
        # scope/owner는 src page를 따른다(personal page → personal 엣지, B3). 비-enum
        # project는 허브가 없어 누락되므로 카운트만 남긴다(발명 금지).
        if label is Label.WIKI_PAGE:
            if page.project in PROJECTS:
                emit_edge(
                    Rel.IN_PROJECT,
                    Label.WIKI_PAGE,
                    ("page_id", pid),
                    Label.PROJECT,
                    ("name", page.project),
                    p_scope,
                    p_owner,
                    "company",
                    None,
                )
            else:
                warnings["unknown_project"] = warnings.get("unknown_project", 0) + 1

        # provenance: corpus_doc + UUID 파싱 가능 + doc 실존일 때만 — Document
        # placeholder를 만들지 않는다(§4.2). 엣지 scope/owner = min(source page, doc)
        # (B3 — 어느 쪽이든 personal이면 엣지가 personal).
        if label is Label.WIKI_SOURCE:
            front = _source_front_for(rows, page)
            if front is not None and front.source_type == "corpus_doc":
                try:
                    ref = UUID(front.source_ref)
                except ValueError:
                    ref = None
                if ref is not None and ref in rows.doc_meta:
                    d_scope, d_owner = rows.doc_meta[ref]
                    # owner-namespace 가드(B4): source의 네임스페이스 밖 doc로는 연결 금지.
                    if doc_visible_to(p_scope, p_owner, d_scope, d_owner):
                        emit_edge(
                            Rel.EXTRACTED_FROM,
                            Label.WIKI_SOURCE,
                            ("page_id", pid),
                            Label.DOCUMENT,
                            ("doc_id", str(ref)),
                            p_scope,
                            p_owner,
                            d_scope,
                            d_owner,
                        )
                    else:
                        warnings["provenance_cross_owner_skipped"] = (
                            warnings.get("provenance_cross_owner_skipped", 0) + 1
                        )

    # :Document — owner = user_id(documents에 owner_id 컬럼 없음, kg-model §3).
    for doc in rows.docs:
        did = str(doc.doc_id)
        emit_node(
            Label.DOCUMENT,
            "doc_id",
            did,
            doc.scope,
            doc.owner_id,
            {
                "title": doc.title,  # 노드 표시 텍스트(본문 아님, 불변식 5)
                "source": doc.source,
                "project": doc.project,
                "source_db_name": doc.source_db_name,
            },
        )
        if expected is not None:
            expected.doc_ids.add(did)
        if doc.project in PROJECTS:
            emit_edge(
                Rel.IN_PROJECT,
                Label.DOCUMENT,
                ("doc_id", did),
                Label.PROJECT,
                ("name", doc.project),
                doc.scope,
                doc.owner_id,
                "company",
                None,
            )
        else:
            warnings["unknown_project"] = warnings.get("unknown_project", 0) + 1

    # :StructuredFact
    for fact in rows.facts:
        rid = str(fact.row_id)
        emit_node(
            Label.STRUCTURED_FACT,
            "row_id",
            rid,
            fact.scope,
            fact.owner_id,
            {
                # 표시 텍스트(본문 아님) — 없으면 read에서 UUID fallback.
                "title": fact.record_key,
                "record_type": fact.record_type,
                "confidence": fact.confidence,
                "valid_until": _parse_valid_until(fact.valid_until_raw, warnings),
            },
        )
        if expected is not None:
            expected.row_ids.add(rid)
        if fact.source_doc_id is not None and fact.source_doc_id in rows.doc_meta:
            d_scope, d_owner = rows.doc_meta[fact.source_doc_id]
            # owner-namespace 가드(B4): fact의 네임스페이스 밖 doc로는 연결 금지.
            if doc_visible_to(fact.scope, fact.owner_id, d_scope, d_owner):
                emit_edge(
                    Rel.EXTRACTED_FROM,
                    Label.STRUCTURED_FACT,
                    ("row_id", rid),
                    Label.DOCUMENT,
                    ("doc_id", str(fact.source_doc_id)),
                    fact.scope,
                    fact.owner_id,
                    d_scope,
                    d_owner,
                )
            else:
                warnings["provenance_cross_owner_skipped"] = (
                    warnings.get("provenance_cross_owner_skipped", 0) + 1
                )

    # wiki_links 엣지 + dangling placeholder(§4.4). projection은 SoR을 검열하지
    # 않는다 — self-link도 그대로 투영(§4.9). dst는 src 네임스페이스에서 해석한다
    # (`resolve_dst` — company ∪ this-owner, never cross-owner, B4). 미해석 dst는
    # ns_key(src.scope, src.owner, slug)로 키잉한 placeholder다 — 같은 slug의
    # company/owner-A/owner-B placeholder가 서로 다른 노드가 된다.
    placeholders: dict[str, tuple[str, str, UUID | None]] = {}  # ns_slug → (slug, scope, owner)
    for link in rows.links:
        rel = WIKI_LINK_REL_MAP.get(link.rel)
        if rel is None:
            warnings["unknown_rel"] = warnings.get("unknown_rel", 0) + 1
            continue
        src_label = WIKI_KIND_LABEL_MAP[link.src_kind]
        s_scope = link.src_scope
        s_owner = link.src_owner_id
        resolved = rows.resolve_dst(s_scope, s_owner, link.dst_slug)
        if resolved is not None:
            dst_pid, dst_kind, d_scope, d_owner = resolved
            dst_label = WIKI_KIND_LABEL_MAP[dst_kind]
            dst = ("page_id", str(dst_pid))
        else:
            dst_label = Label.WIKI_PAGE
            nsk = ns_key(s_scope, s_owner, link.dst_slug)
            dst = ("ns_slug", nsk)
            placeholders[nsk] = (link.dst_slug, s_scope, s_owner)
            # placeholder는 src 네임스페이스에 속한다 — 엣지 scope도 src를 따른다.
            d_scope, d_owner = s_scope, s_owner
        props: dict[str, Any] = {}
        if rel is Rel.CONFLICTS_WITH:
            # K8.6 — conflict status는 src 네임스페이스의 conflict task에서 온다. company link는
            # ns='company', personal link(owner-scope)는 owner ns로 분리 lookup(같은 claim 쌍이
            # company/owner별로 다른 status를 가질 수 있다). placeholder counterpart도 src ns 상속.
            ns = ns_for(s_scope, s_owner)
            pair = sorted((link.src_slug, link.dst_slug))
            meta = rows.conflict_index.get((ns, pair[0], pair[1]))
            props = {
                "detected_at": meta.detected_at if meta else None,
                "conflict_reason": meta.reason if meta else None,
                "status": meta.status if meta else "UNRESOLVED",
                "resolved_favoring_id": None,  # v1 항상 null(kg-model §2)
            }
        emit_edge(
            rel,
            src_label,
            ("page_id", str(link.src_page_id)),
            dst_label,
            dst,
            s_scope,
            s_owner,
            d_scope,
            d_owner,
            props=props,
        )

    for nsk in sorted(placeholders):
        slug, ph_scope, ph_owner = placeholders[nsk]
        emit_node(
            Label.WIKI_PAGE,
            "ns_slug",
            nsk,
            ph_scope,
            ph_owner,
            {"ns_slug": nsk, "slug": slug, "materialized": False},
        )
    if expected is not None:
        # ExpectedKeys.placeholder_slugs는 이제 ns_slug 공간을 담는다(§2) —
        # store.prune 2단계가 ns_slug로 매칭한다.
        expected.placeholder_slugs = set(placeholders)

    # :Entity (K6) — kg_entities SoR에서 결정론 투영(본문 없음, 이름/메타만).
    # full load에서만 채워진다(_load의 since 가드) — incremental은 rows.entities가
    # 비어 no-op이고 prune도 full 전용이라 정합한다.
    # company-only(D2) — scope=company/owner=null props를 싣는다(B1: owner predicate가
    # entity 노드에 매칭되려면 scope 속성 필요, 누락 시 모두에게 불가시). 노드 키는
    # `company:` 접두 슬롯(entity_node_key) — PG entity_key는 unprefixed 유지.
    #
    # K9 — `mention_count`(degree) 노드 속성(B1/R3/C-E): entity_neighbors가 흔한 엔티티를
    # `e.mention_count < $hub_threshold`로 제외하는 read 게이트 입력. **page-only 집계**다 —
    # 다리 Cypher의 `q:WikiPage`(page-kind만, claim은 :WikiClaim)와 denominator를 일치시켜야
    # threshold 튜닝이 어긋나지 않는다(R3). rows.mentions는 page+claim(`_MENTION_PAGE_KINDS`)을
    # 담으므로 `page_kind=='page'`만 센다(page+claim 전체 재사용 금지). 각 mention은 (entity,page)
    # UNIQUE라(entities.py on_conflict) page-kind mention 수 = 그 엔티티를 언급한 distinct page 수.
    mention_count_by_key: dict[str, int] = {}
    for m in rows.mentions:
        if m.page_kind == "page":
            mention_count_by_key[m.entity_key] = mention_count_by_key.get(m.entity_key, 0) + 1
    for ent in rows.entities:
        node_key = entity_node_key(COMPANY_NS, ent.entity_kind, ent.name_norm)
        emit_node(
            Label.ENTITY,
            "entity_key",
            node_key,
            "company",
            None,
            {
                "name_norm": ent.name_norm,
                "display_name": ent.display_name,
                "entity_kind": ent.entity_kind,
                "first_seen": ent.first_seen,
                "last_seen": ent.last_seen,
                "mention_count": mention_count_by_key.get(ent.entity_key, 0),
            },
        )
        if expected is not None:
            expected.entity_keys.add(node_key)  # prune은 접두 키로 비교

    # MENTIONED_IN(entity→page|claim) + RELATES_TO{co_mention}(같은 page co-mention
    # 쌍). page_id는 company page|claim(_load_mentions join 필터)이고 entity도
    # company라 두 엣지 모두 company/null(B1 predicate 매칭). co-mention은 page_id
    # 그룹에서 유도하고, 무방향 중복은 entity_key 정렬 1방향 + page당 cap.
    # 엣지 끝점도 접두 키를 쓴다(노드 merge_value와 일치해야 MATCH 성립). by_page는
    # raw entity_key로 묶고(co-mention dedup/정렬 — 접두는 상수라 순서 불변) emit에서 접두.
    by_page: dict[tuple[str, str, str], list[str]] = {}  # (page_id, label, slug) → entity_keys
    for m in rows.mentions:
        dst_label = WIKI_KIND_LABEL_MAP[m.page_kind]
        pid = str(m.page_id)
        emit_edge(
            Rel.MENTIONED_IN,
            Label.ENTITY,
            ("entity_key", entity_node_key_for(COMPANY_NS, m.entity_key)),
            dst_label,
            ("page_id", pid),
            "company",
            None,
            "company",
            None,
        )
        by_page.setdefault((pid, dst_label.value, m.slug), []).append(m.entity_key)

    for (_pid, _label, slug), eks in sorted(by_page.items()):
        uniq = sorted(set(eks))[:_CO_MENTION_MAX_ENTITIES_PER_PAGE]
        for a, b in itertools.combinations(uniq, 2):
            emit_edge(
                Rel.RELATES_TO,
                Label.ENTITY,
                ("entity_key", entity_node_key_for(COMPANY_NS, a)),
                Label.ENTITY,
                ("entity_key", entity_node_key_for(COMPANY_NS, b)),
                "company",
                None,
                "company",
                None,
                props={"kind": "co_mention", "evidence_slug": slug},
            )

    # 엣지 dedup — wiki_links는 PK가 없고 MERGE 키가 (src, rel, dst)라 중복 row는
    # 같은 엣지로 수렴한다. 마지막 occurrence의 props가 이긴다(같은 입력이면 동일).
    deduped: dict[tuple[str, str, str, str, str], KgEdgeRow] = {}
    for e in edges:
        deduped[(e.src_label.value, e.src[1], e.rel.value, e.dst_label.value, e.dst[1])] = e
    edges = list(deduped.values())
    if expected is not None:
        expected.edge_triples = {(e.src[1], e.rel.value, e.dst[1]) for e in edges}

    counts: dict[str, int] = {}
    for node in nodes:
        key = f"nodes_{node.label.value}"
        counts[key] = counts.get(key, 0) + 1
    counts["nodes_placeholder"] = len(placeholders)
    counts[f"nodes_{Label.WIKI_PAGE.value}"] = counts.get(
        f"nodes_{Label.WIKI_PAGE.value}", 0
    ) - len(placeholders)
    for e in edges:
        key = f"edges_{e.rel.value}"
        counts[key] = counts.get(key, 0) + 1
    for name, n in warnings.items():
        counts[f"warning_{name}"] = n

    return KgProjectionPlan(nodes=nodes, edges=edges, expected_keys=expected, counts=counts)
