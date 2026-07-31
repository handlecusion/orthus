"""LLM wiki grounded Q&A and internal task review endpoints."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy import select

from orthus.agentwork import list_agent_work_items
from orthus.audit import audit
from orthus.router import resolve_answer_scope as _resolve_scope
from orthus.agentwork.wiki_link import (
    AGENT_WORK_WIKI_LINK_LOOKBACK_LIMIT,
    enrich_agent_work_items,
)
from orthus.api.deps import get_current_user, get_user_id
from orthus.api.knowledge_token import (
    COLLECTOR_TOKEN_AUTH_MODE,
    WIKI_TASK_MAX_EVIDENCE_URLS,
    WIKI_TASK_MAX_NOTE_CHARS,
    enforce_token_wiki_task_rate_limit,
    get_session_user_or_knowledge_token,
    get_session_user_or_knowledge_write_token,
)
from orthus.auth import AuthenticatedUser, require_node_operator
from orthus.federation import client as central_client
from orthus.federation.query import federated_wiki_answer
from orthus.kg.client import entities_present, kg_owner_scope_enabled
from orthus.kg.gate import (
    KgQueryStatus,
    kg_edge_dedup_key,
    kg_node_dedup_key,
    kg_rebuild_in_progress,
    owner_footprint,
    run_kg_template,
)
from orthus.kg.relations import run_kg_relation, supported_from
from orthus.kg.visibility import entity_key_from_node_id, resolve_slug
from orthus.schemas.canonical import (
    AgentWorkWikiLinkResponse,
    DataGapPageResponse,
    EntityTeaser,
    KgGraphNode,
    KgRelationResponse,
    OwnerFootprint,
    WikiAnswer,
    WikiPage,
    WikiPageGraphResponse,
    WikiSearchItem,
    WikiSearchResponse,
    WikiSourceRef,
    WikiTask,
    WikiTaskCreateRequest,
    WikiTaskCreateResult,
    WikiTaskPageResponse,
    WikiTaskResolution,
)
from orthus.db import session as db_session
from orthus.settings import get_settings
from orthus.tables import wiki_pages as wiki_pages_table
from orthus.wiki import ask
from orthus.wiki import store
from orthus.wiki.author import author_from_qa, qa_claim_slug
from orthus.wiki.retrieve import retrieve
from orthus.wiki.gap import list_gaps_for_wiki_page
from orthus.wiki.slug import clean_wiki_slug

router = APIRouter(prefix="/wiki", tags=["wiki"])


class AskIn(BaseModel):
    question: str
    k: int = 5
    since: date | None = None
    until: date | None = None
    source: str | None = None
    scope: str | None = None
    context_wiki_slug: str | None = None

    @field_validator("context_wiki_slug", mode="before")
    @classmethod
    def _clean_context_wiki_slug(cls, value: object) -> str | None:
        return clean_wiki_slug(value)


class TaskPatchIn(BaseModel):
    resolved: bool


# WTR.2: kind별 의미 있는 resolve 입력. typed allowlist — extra 금지.
# NOTE: 이 셋은 "수동 one-click cleanup resolve(입력/claim write 없음)를 허용하는
# kind"다. `agentwork/service.py::_WIKI_TASK_CLEANUP_KINDS`(auto-resolve 허용 셋)와
# 의도적으로 다르다 — entity_conflict는 KG 충돌이라 자동 resolve는 막되(리뷰 필요)
# 운영자가 수동으로 정리(claim 변경 없음)하는 것은 허용한다.
_CLEANUP_KINDS = {"stale_audit", "dedup", "provenance_fix", "entity_conflict"}
_CONFLICT_DECISIONS = {"keep_existing", "use_incoming", "merge"}


class TaskResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["keep_existing", "use_incoming", "merge", "answered", "cleanup", "dismissed"]
    merged_text: str | None = None  # decision="merge"일 때만 필수/허용
    answer: str | None = None  # decision="answered"일 때만 필수/허용
    note: str | None = None  # 모든 decision optional

    @model_validator(mode="after")
    def _check_decision_fields(self) -> "TaskResolveIn":
        # merged_text는 merge 전용/필수, answer는 answered 전용/필수.
        # 기본값(None)에도 항상 평가되도록 model_validator를 쓴다.
        if self.decision == "merge":
            if not (self.merged_text and self.merged_text.strip()):
                raise ValueError("merged_text required for decision=merge")
        elif self.merged_text is not None:
            raise ValueError("merged_text only allowed for decision=merge")
        if self.decision == "answered":
            if not (self.answer and self.answer.strip()):
                raise ValueError("answer required for decision=answered")
        elif self.answer is not None:
            raise ValueError("answer only allowed for decision=answered")
        return self


@router.post("/ask", response_model=WikiAnswer)
def wiki_ask(body: AskIn, user_id: UUID = Depends(get_user_id)) -> WikiAnswer:
    settings = get_settings()
    if settings.node_kind == "personal":
        return federated_wiki_answer(
            user_id,
            body.question,
            k=body.k,
            since=_start_of_day(body.since),
            until=_exclusive_next_day(body.until),
            source=body.source,
            context_wiki_slug=body.context_wiki_slug,
        )
    # Company (central) node: clamp the requested scope with the same
    # `_resolve_scope` semantics as /ask. With owner-scope enabled an explicit
    # all/personal merges the caller's own personal layer (retrieve still applies
    # the fail-closed owner filter); default stays company-only.
    return ask(
        user_id,
        body.question,
        k=body.k,
        scope=_resolve_scope(body.scope),
        since=_start_of_day(body.since),
        until=_exclusive_next_day(body.until),
        source=body.source,
        context_wiki_slug=body.context_wiki_slug,
    )


@router.get("/search", response_model=WikiSearchResponse)
def wiki_search(
    query: str = Query(..., min_length=1),
    scope: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> WikiSearchResponse:
    """Thin compiled-page search over wiki retrieve (P8.7b MCP surface).

    Returns slug/title/snippet/owner-scope metadata only. The snippet is the
    matching compiled page/claim chunk excerpt — never a raw corpus chunk body —
    so the grounding contract stays intact. Owner visibility is enforced by
    `retrieve` (`_resolve_scope` clamps the request; personal hits require
    owner_id == caller)."""
    effective_scope = _resolve_scope(scope)
    hits = retrieve(current.user_id, query, k=limit, scope=effective_scope)
    items = [
        WikiSearchItem(
            slug=hit.page_slug,
            title=hit.title,
            kind=hit.kind,
            snippet=hit.excerpt,
            score=hit.score,
            scope=hit.source_scope,
        )
        for hit in hits
    ]
    return WikiSearchResponse(query=query, scope=effective_scope, count=len(items), items=items)


@router.post("/tasks", response_model=WikiTaskCreateResult)
def create_wiki_task(
    body: WikiTaskCreateRequest,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_write_token),
) -> WikiTaskCreateResult:
    """Create an `open_question` WikiTask candidate referencing a target slug.

    Writes at the NODE's wiki scope (`_wiki_scope`): on a company node the
    candidate is company-scope (`owner_id=None`), so it lands in the shared
    `/wiki/tasks` review queue that owner AND admin operators see and can resolve
    (WTR.2 gates the actual content change to owner/admin). On a personal node it
    stays the owner's personal scope. It never writes wiki page content — only a
    review task — so the content gate is unchanged; the note + evidence URLs
    become the task description and the target slug is a related page so
    page-specific task panels surface it."""
    # Collector-token callers (the inline/delegated agent) are rate-limited + size-
    # capped so an injected agent can't spam the shared review queue; session/demo/
    # jwt operators are exempt.
    enforce_token_wiki_task_rate_limit(current)
    if current.auth_mode == COLLECTOR_TOKEN_AUTH_MODE:
        if len(body.note) > WIKI_TASK_MAX_NOTE_CHARS:
            raise HTTPException(status_code=413, detail="wiki task note too long")
        if len(body.evidence_urls) > WIKI_TASK_MAX_EVIDENCE_URLS:
            raise HTTPException(status_code=413, detail="too many evidence urls")
    target_slug = _require_wiki_slug(body.slug)
    user_id = current.user_id
    scope, owner_id = _wiki_scope(user_id)
    description = body.note
    if body.evidence_urls:
        description = f"{body.note}\n\nevidence: " + ", ".join(body.evidence_urls)
    task_slug = f"update-candidate-{target_slug}-{uuid4().hex[:8]}"
    task = WikiTask(
        slug=task_slug,
        kind="open_question",
        description=description,
        related=[target_slug],
        created_at=datetime.now(UTC),
        resolved=False,
    )
    store.write_task(
        task,
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
        project="company",
    )
    return WikiTaskCreateResult(
        slug=task_slug,
        target_slug=target_slug,
        kind="open_question",
        scope=scope,
        created=True,
    )


@router.get("/pages/{slug:path}/agent-work", response_model=AgentWorkWikiLinkResponse)
def get_wiki_page_agent_work(
    slug: str,
    scope: str | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentWorkWikiLinkResponse:
    require_node_operator(current)
    resolved_scope, owner_id = _wiki_page_scope(current.user_id, scope)
    # 존재 게이트 — 본문이 필요 없으므로 파일 stat만 본다(load_page와 같은 _path 규칙).
    if not store.exists("page", slug, scope=resolved_scope, owner_id=owner_id):
        return AgentWorkWikiLinkResponse(count=0, items=[])
    items = enrich_agent_work_items(
        list_agent_work_items(current.user_id, limit=AGENT_WORK_WIKI_LINK_LOOKBACK_LIMIT),
        user_id=current.user_id,
        settings=get_settings(),
    )
    matching = [item for item in items if slug in item.wiki_slugs]
    return AgentWorkWikiLinkResponse(count=len(matching), items=matching[:20])


@router.get("/pages/{slug:path}/data-gaps", response_model=DataGapPageResponse)
def get_wiki_page_data_gaps(
    slug: str,
    scope: str | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> DataGapPageResponse:
    # Dual-auth so the orthus-mcp `data_gaps(slug=...)` tool can read a page's open
    # backlog; the token resolves to its owner, keeping the owner-scope filter.
    require_node_operator(current)
    slug = _require_wiki_slug(slug)
    resolved_scope, owner_id = _wiki_page_scope(current.user_id, scope)
    # 존재 게이트 — 본문이 필요 없으므로 파일 stat만 본다(load_page와 같은 _path 규칙).
    if not store.exists("page", slug, scope=resolved_scope, owner_id=owner_id):
        return DataGapPageResponse(count=0, items=[])
    gaps = list_gaps_for_wiki_page(current.user_id, slug, status="open", limit=20)
    return DataGapPageResponse(count=len(gaps), items=gaps)


@router.get("/pages/{slug:path}/tasks", response_model=WikiTaskPageResponse)
def get_wiki_page_tasks(
    slug: str,
    scope: str | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> WikiTaskPageResponse:
    require_node_operator(current)
    slug = _require_wiki_slug(slug)
    resolved_scope, owner_id = _wiki_page_scope(current.user_id, scope)
    # 존재 게이트 — 본문이 필요 없으므로 파일 stat만 본다(load_page와 같은 _path 규칙).
    if not store.exists("page", slug, scope=resolved_scope, owner_id=owner_id):
        return WikiTaskPageResponse(count=0, items=[])
    matching: list[WikiTask] = []
    for task_slug in store.list_slugs("task", scope=resolved_scope, owner_id=owner_id):
        task = store.load_task(task_slug, scope=resolved_scope, owner_id=owner_id)
        if task is None or task.resolved:
            continue
        resolved_related = [
            related
            for related in task.related
            if _wiki_page_exists(related, scope=resolved_scope, owner_id=owner_id)
        ]
        if slug in resolved_related:
            matching.append(task.model_copy(update={"related": resolved_related}))
    matching.sort(key=lambda t: (-t.created_at.timestamp(), t.slug))
    return WikiTaskPageResponse(count=len(matching), items=matching[:20])


@router.get("/kg/footprint", response_model=OwnerFootprint)
def get_owner_kg_footprint(
    response: Response,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> OwnerFootprint:
    """K7.3 — caller **본인**의 personal KG footprint(공유 central 그래프에 올라간 내
    personal 노드/엣지 카운트). owner-only read, owner_id UUID 미노출(카운트만). `$caller`는
    세션에서만(T1). flag-off/owner-scope off/null caller/Neo4j 미가용은 빈 footprint
    (fail-open). personal 데이터라 캐시 금지(T4)."""
    # T4 — personal 데이터: 세션 간 공유/CDN 캐시 금지.
    response.headers["Cache-Control"] = "private, no-store"
    # W2(O1/FE) — full rebuild 중에는 half-projected footprint를 "0/0"로 오노출하지 않고
    # status=rebuilding으로 보류 표시(per-request :KgMeta 1회 lookup, 드문 윈도우).
    if kg_rebuild_in_progress():
        return OwnerFootprint(status="rebuilding")
    return owner_footprint(current.user_id)


@router.get("/pages/{slug:path}/graph", response_model=WikiPageGraphResponse)
def get_wiki_page_graph(
    slug: str,
    response: Response,
    depth: int = Query(default=1, ge=1, le=2),
    include: str | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> WikiPageGraphResponse:
    """K4 — wiki page 주변 1-2 hop 관계 그래프 (read-only, kg-model §4).

    응답은 slug/title/메타만 담는다 — 본문은 기존 page GET으로 되돌아간다
    (불변식 5). KG off(personal node 포함)는 404보다 먼저 `supported:false`로
    평가한다(P4 federated unsupported 패턴). **fail-open 범위는 KG/Neo4j
    가용성이다** — Neo4j down/timeout/그래프 처리 오류는 `kg_unavailable`로
    수렴하고(세부 사유는 `kg_query_runs`), `run_kg_template`은 예외를 던지지
    않는다. 반면 PG(SoR) 장애는 fail-open 대상이 아니다 — page GET 등 다른
    route와 동일하게 5xx로 드러난다(404 존재 확인이 PG에 의존한다).
    인증은 page GET과 동일 dependency다 — 그래프 메타는 page 본문 이하의
    민감도라 operator 가드를 걸지 않는다(구현 명세 §11-5).

    404 시작-노드 존재 판정은 게이트와 **동일한 scope**를 본다(코드리뷰): owner-scope
    ON이면 caller 자기 personal + company를 함께 보는 owner-inclusive resolve라 owner가
    자기 personal 페이지 그래프를 받을 수 있고, OFF이면 company-only(v1 불변). 이 판정이
    게이트의 resolve와 갈라지면(예: company-only 고정) owner read 경로가 통째로 가려진다."""
    # T4 — owner-scope ON이면 응답에 caller 본인 personal 노드가 섞이므로 캐시 금지
    # (세션 간 공유/CDN 캐시 차단). company-only(flag-off)여도 무해.
    response.headers["Cache-Control"] = "private, no-store"
    if not get_settings().kg_enabled:
        return WikiPageGraphResponse(slug=slug, supported=False, reason="kg_disabled")
    # W2(FE) — full rebuild 중에는 half-projected(`scope=None+owner=None→company`) 노드/엣지
    # 오노출을 막기 위해 supported:false로 보류(P4 unsupported-state 패턴, fail-open과 동형).
    if kg_rebuild_in_progress():
        return WikiPageGraphResponse(slug=slug, supported=False, reason="kg_unavailable")
    slug = _require_wiki_slug(slug)
    if not _graph_start_node_exists(slug, current.user_id):
        raise HTTPException(status_code=404, detail=f"wiki page not found: {slug}")
    result = run_kg_template(
        user_id=current.user_id,
        template_name="neighbors",
        params={"slug": slug, "depth": depth},
    )
    if result.status is not KgQueryStatus.OK:
        return WikiPageGraphResponse(slug=slug, supported=False, reason="kg_unavailable")
    nodes = list(result.nodes)
    edges = list(result.edges)
    truncated = result.truncated
    # K8.2 (B2-a) — 이 페이지 claim들의 모순(CONFLICTS_WITH, placeholder counterpart 포함)을
    # 같은 패널 ring/list에 합류한다. neighbors와 별개 게이트 호출(page_conflicts)을 돌려
    # 서버에서 nodes/edges를 병합한다(cross-call truncated primitive 부재 — 병합 truncated의
    # 단일 소유자 = 이 endpoint). additive·best-effort: page_conflicts가 실패해도 neighbors
    # 패널은 그대로 렌더한다(모순만 빠짐). owner-scope는 게이트가 내부 처리(B7-union — counterpart
    # fetch가 gate+predicate+tripwire 경유, raw Cypher 우회 없음).
    #
    # 효율(코드리뷰 #7) — 모순은 claim↔counterpart 엣지이고, 페이지의 claim은 neighbors에
    # BACKLINK로 잡힌다. neighbors에 WikiClaim이 하나도 없고 truncate도 안 됐으면(= 페이지에
    # claim 없음 확정) 모순이 있을 수 없으므로 두 번째 게이트 호출/Neo4j 왕복/`kg_query_runs`
    # row를 생략한다. truncate된 경우엔 claim이 가려졌을 수 있어 보수적으로 그대로 조회한다.
    has_claim_neighbor = any(n.label == "WikiClaim" for n in nodes)
    if has_claim_neighbor or result.truncated or result.boundary_tripwire_drops > 0:
        conflicts = run_kg_template(
            user_id=current.user_id,
            template_name="page_conflicts",
            params={"slug": slug},
        )
        if conflicts.status is KgQueryStatus.OK:
            cap = get_settings().kg_query_limit
            nodes, edges, union_overflow = _merge_graph(
                nodes, edges, conflicts.nodes, conflicts.edges, cap=cap
            )
            truncated = truncated or conflicts.truncated or union_overflow

    # K9.2 (C-C/U2/D-TZ B) — entity 다리 합류. **presence-gated**: 엔티티가 전역에 존재할 때만
    # entity_neighbors를 1회 호출한다(dormant=`kg_entities=0`이면 entities_present()=False → 2차
    # 왕복/row 0). 결과로 펼침-전 티저(page 수 + 비-person 엔티티명, person 억제 D-TZ B)를 유도하고,
    # `include=entity`일 때만 entity 노드/엣지를 패널 ring에 합류한다(기본 패널 무변 — U2 펼침 전/후).
    # additive·best-effort: entity_neighbors 실패해도 neighbors/conflicts 패널은 그대로(다리만 빠짐).
    entity_teaser: EntityTeaser | None = None
    if entities_present():
        ent = run_kg_template(
            user_id=current.user_id,
            template_name="entity_neighbors",
            params={"slug": slug},
        )
        if ent.status is KgQueryStatus.OK and ent.nodes:
            entity_teaser = _build_entity_teaser(slug, ent.nodes)
            if include == "entity":
                cap = get_settings().kg_query_limit
                nodes, edges, union_overflow = _merge_graph(
                    nodes, edges, ent.nodes, ent.edges, cap=cap
                )
                truncated = truncated or ent.truncated or union_overflow
    return WikiPageGraphResponse(
        slug=slug,
        supported=True,
        truncated=truncated,
        nodes=nodes,
        edges=edges,
        entity_teaser=entity_teaser,
        # K7.3 honesty(D2) — owner-scope ON이면 personal note의 entity 기반 연결이 아직
        # 없음을 알려 FE state 3 카피를 정직하게. flag-off는 False.
        personal_entity_search_deferred=kg_owner_scope_enabled(),
    )


_EXPAND_ENTITY_LABEL = "Entity"


@router.get("/graph/expand", response_model=WikiPageGraphResponse)
def get_wiki_graph_expand(
    response: Response,
    label: str = Query(..., min_length=1),
    id: str = Query(..., min_length=1),
    depth: int = Query(default=1, ge=1, le=1),  # E1b는 1-hop 고정(누적은 FE)
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> WikiPageGraphResponse:
    """E1b — 임의 (label,id) 노드 1-hop 확장(그래프 탐색기 더블클릭). read-only, 불변식 5.

    가드 순서는 get_wiki_page_graph와 동일: kg_enabled → rebuild 보류 → (precheck는 게이트) →
    kg_available. Entity면 namespaced id에서 접두 제거해 expand_entity, 그 외 expand_node.
    존재하지 않는/foreign 노드는 fail-open 대상이 아니라 404(K4 계약 — fail-open은 KG 가용성 한정).
    owner-scope ON 응답은 caller personal 노드가 섞일 수 있어 캐시 금지."""
    response.headers["Cache-Control"] = "private, no-store"
    if not get_settings().kg_enabled:
        return WikiPageGraphResponse(slug=id, supported=False, reason="kg_disabled")
    if kg_rebuild_in_progress():
        return WikiPageGraphResponse(slug=id, supported=False, reason="kg_unavailable")
    if label == _EXPAND_ENTITY_LABEL:
        result = run_kg_template(
            user_id=current.user_id,
            template_name="expand_entity",
            params={
                "entity_key": entity_key_from_node_id(id)
            },  # unprefixed → gate가 namespaced 재구성
            limit=get_settings().kg_expand_limit,
        )
    else:
        result = run_kg_template(
            user_id=current.user_id,
            template_name="expand_node",
            params={"label": label, "id": id},
            limit=get_settings().kg_expand_limit,
        )
    if result.status is not KgQueryStatus.OK:
        # 미존재/foreign 노드 + invalid_params(미허용 label 포함) → 404(fail-open은 KG 가용성 한정).
        rr = result.reject_reason or ""
        if rr == "entity_or_node_not_found" or rr.startswith("invalid_params"):
            raise HTTPException(status_code=404, detail=f"node not found: {label}/{id}")
        return WikiPageGraphResponse(slug=id, supported=False, reason="kg_unavailable")
    return WikiPageGraphResponse(
        slug=id,
        supported=True,
        truncated=result.truncated,
        nodes=result.nodes,
        edges=result.edges,
        personal_entity_search_deferred=kg_owner_scope_enabled(),
    )


class KgQueryRequest(BaseModel):
    """`POST /wiki/kg/query` 입력. relation 의도 + 그에 맞는 식별자(slug/slug_b/name).
    depth/max_hops는 템플릿 상한에 맞게 게이트 직전 clamp된다(orthus/kg/relations.py)."""

    relation: Literal["neighbors", "conflicts", "related", "path", "mentions"]
    slug: str | None = None
    slug_b: str | None = None
    name: str | None = None
    depth: int = 1
    max_hops: int = 4


@router.post("/kg/query", response_model=KgRelationResponse)
def post_kg_query(
    body: KgQueryRequest,
    response: Response,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> KgRelationResponse:
    """K-series 관계 read — orthus-mcp `kg_relations`/`entity_relations` 툴 + cli /ask +
    위임 에이전트가 친다. graph 패널 endpoint(`/pages/{slug}/graph`)와 동일한 read-only
    게이트(`run_kg_template`)를 쓰되 단일 페이지에 묶이지 않은 일반 관계 질의를 받는다.

    인증은 graph endpoint와 동일 dual-auth라 collector knowledge 토큰의 owner로 바인딩되고,
    게이트가 owner-inclusive resolve + tripwire로 경계를 보장한다(타 owner personal 미노출).
    KG off/rebuild/미가용은 200 + `supported=false`(fail-open)다. personal 데이터가 섞일 수
    있어 캐시 금지."""
    response.headers["Cache-Control"] = "private, no-store"
    if not get_settings().kg_enabled:
        return KgRelationResponse(supported=False, relation=body.relation, reason="kg_disabled")
    if kg_rebuild_in_progress():
        return KgRelationResponse(supported=False, relation=body.relation, reason="kg_unavailable")
    result = run_kg_relation(
        user_id=current.user_id,
        relation=body.relation,
        slug=body.slug,
        slug_b=body.slug_b,
        name=body.name,
        depth=body.depth,
        max_hops=body.max_hops,
    )
    return KgRelationResponse(
        supported=supported_from(result),
        relation=body.relation,
        reason=None if result.status is KgQueryStatus.OK else result.reject_reason,
        truncated=result.truncated,
        nodes=result.nodes,
        edges=result.edges,
    )


@router.get("/pages/{slug:path}", response_model=WikiPage)
def get_wiki_page(
    slug: str,
    scope: str | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> WikiPage:
    user_id = current.user_id
    if scope is not None:
        resolved_scope, owner_id = _wiki_page_scope(user_id, scope)
        page = store.load_page(slug, scope=resolved_scope, owner_id=owner_id)
        if page is None:
            raise HTTPException(status_code=404, detail=f"wiki page not found: {slug}")
        return page

    resolved_scope, owner_id = _wiki_scope(user_id)
    page = store.load_page(slug, scope=resolved_scope, owner_id=owner_id)
    if page is None and resolved_scope == "personal":
        try:
            page = central_client.get_company_wiki_page(slug)
        except central_client.CentralReadUnavailable:
            page = store.load_page(slug, scope="company", owner_id=None)
    if page is None and resolved_scope == "company" and get_settings().owner_scope_enabled:
        # P8.4 merged read plane: a company miss falls back to the caller's own
        # personal page (owner_id == current user) before 404. Other users'
        # personal pages stay invisible because owner_id never matches them.
        page = store.load_page(slug, scope="personal", owner_id=user_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"wiki page not found: {slug}")
    return page


@router.get("/tasks", response_model=list[WikiTask])
def list_wiki_tasks(
    resolved: bool | None = Query(default=None),
    kind: str | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> list[WikiTask]:
    scope, owner_id = _wiki_scope(user_id)
    tasks: list[WikiTask] = []
    for slug in store.list_slugs("task", scope=scope, owner_id=owner_id):
        task = store.load_task(slug, scope=scope, owner_id=owner_id)
        if task is None:
            continue
        if resolved is not None and task.resolved is not resolved:
            continue
        if kind is not None and task.kind != kind:
            continue
        tasks.append(task)
    return sorted(tasks, key=lambda t: (t.resolved, t.created_at), reverse=False)


@router.patch("/tasks/{slug}", response_model=WikiTask)
def patch_wiki_task(
    slug: str,
    body: TaskPatchIn,
    user_id: UUID = Depends(get_user_id),
) -> WikiTask:
    scope, owner_id = _wiki_scope(user_id)
    task = store.load_task(slug, scope=scope, owner_id=owner_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"wiki task not found: {slug}")
    updated = task.model_copy(update={"resolved": body.resolved})
    # Project is not the privacy boundary; personal scope is carried by owner_id.
    project = "company"
    store.write_task(updated, user_id=user_id, scope=scope, owner_id=owner_id, project=project)
    # WTR.2: reopen(resolved True->False)만 별도 감사. plain dismiss(False->True)는
    # 기존 동작 유지 — role gate/claim mutation 없는 하위호환 경로.
    if task.resolved and not body.resolved:
        with audit("wiki.task.reopen") as span:
            span.add_meta(
                slug=slug,
                kind=task.kind,
                node_id=get_settings().node_id,
                user_id=str(user_id),
            )
    return updated


@router.post("/tasks/{slug}/resolve", response_model=WikiTask)
def resolve_wiki_task(
    slug: str,
    body: TaskResolveIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> WikiTask:
    """WTR.2: kind-aware interactive resolve + claim write-back/compile.

    `PATCH /wiki/tasks/{slug}`(reopen + 하위호환 dismiss)와 달리, conflict는
    실제 claim 텍스트를 갱신하고 open_question은 `author_from_qa` compile 경로로
    새 claim을 만든다. 내용 변경 decision(use_incoming/merge/answered)은
    owner/admin(`require_node_operator`)만 가능하고, keep_existing/cleanup/dismissed는
    현행 권한으로 닫는다. write-back은 task와 같은 scope/owner로 한다(`_wiki_scope`)."""
    scope, owner_id = _wiki_scope(current.user_id)
    task = store.load_task(slug, scope=scope, owner_id=owner_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"wiki task not found: {slug}")

    decision = body.decision
    # decision <-> kind 정합성 (spec §4).
    if decision in _CONFLICT_DECISIONS and task.kind != "conflict":
        raise HTTPException(status_code=422, detail=f"decision={decision} requires kind=conflict")
    if decision == "answered" and task.kind != "open_question":
        raise HTTPException(status_code=422, detail="decision=answered requires kind=open_question")
    if decision == "cleanup" and task.kind not in _CLEANUP_KINDS:
        raise HTTPException(status_code=422, detail="decision=cleanup requires a cleanup-kind task")
    # conflict write-back 사전 입력 검증 — audit span 밖에서 처리해 검증 실패가
    # errored `wiki.task.resolve` span으로 잡히지 않게 한다(span은 실제 쓰기만 감싼다).
    if decision in {"use_incoming", "merge"} and not task.related:
        raise HTTPException(status_code=422, detail="conflict task has no related claim")
    if decision == "use_incoming" and task.incoming_claim is None:
        raise HTTPException(status_code=422, detail="no incoming text recorded")

    # 내용 변경(claim write/compile)은 owner/admin 전용 (demo/jwt 통과).
    if decision in {"use_incoming", "merge", "answered"}:
        require_node_operator(current)

    produced_claim_slugs: list[str] = []
    claim_slug_meta: str | None = None

    with audit("wiki.task.resolve") as span:
        if decision in {"use_incoming", "merge"}:
            existing_slug = task.related[0]
            existing = store.load_claim(existing_slug, scope=scope, owner_id=owner_id)
            if existing is None:
                raise HTTPException(
                    status_code=404, detail=f"wiki claim not found: {existing_slug}"
                )
            new_text = task.incoming_claim if decision == "use_incoming" else body.merged_text
            store.write_claim(
                existing.model_copy(update={"claim": new_text, "last_reviewed": date.today()}),
                user_id=current.user_id,
                scope=scope,
                owner_id=owner_id,
                project="company",
            )
            produced_claim_slugs = [existing_slug]
            claim_slug_meta = existing_slug
        elif decision == "answered":
            # open_question -> author_from_qa compile. Generated tasks keep
            # source slug first and wiki page hints after it; only actual wiki
            # pages should become QA grounding refs.
            source_refs: list[WikiSourceRef] = []
            for related in task.related:
                if not _wiki_page_exists(related, scope=scope, owner_id=owner_id):
                    continue
                source_refs.append(
                    WikiSourceRef(
                        page_slug=related,
                        title=related,
                        kind="page",
                        excerpt="",
                        score=0.0,
                        source_scope="personal" if scope == "personal" else "company",
                    )
                )
            author_from_qa(
                current.user_id,
                question=task.description,
                answer=body.answer or "",
                source_refs=source_refs,
                scope=scope,
                owner_id=owner_id,
            )
            produced_slug = qa_claim_slug(task.description)
            produced_claim_slugs = [produced_slug]
            claim_slug_meta = produced_slug
        # keep_existing / cleanup / dismissed: write-back 없음.

        resolution = WikiTaskResolution(
            decision=decision,
            note=body.note,
            resolved_by=current.user_id,
            resolved_at=datetime.now(UTC),
            produced_claim_slugs=produced_claim_slugs,
        )
        updated = task.model_copy(update={"resolved": True, "resolution": resolution})
        store.write_task(
            updated,
            user_id=current.user_id,
            scope=scope,
            owner_id=owner_id,
            project="company",
        )
        span.add_meta(
            slug=slug,
            kind=task.kind,
            decision=decision,
            claim_slug=claim_slug_meta,
            node_id=get_settings().node_id,
            user_id=str(current.user_id),
        )
    return updated


def _wiki_scope(user_id: UUID) -> tuple[str, UUID | None]:
    settings = get_settings()
    if settings.node_kind == "personal":
        return "personal", user_id
    return "company", None


def _wiki_page_scope(user_id: UUID, scope: str | None) -> tuple[str, UUID | None]:
    if scope is None:
        return _wiki_scope(user_id)
    value = scope.strip()
    if value == "personal":
        return "personal", user_id
    if value == "company":
        return "company", None
    raise HTTPException(status_code=422, detail="scope must be company or personal")


def _require_wiki_slug(slug: str) -> str:
    clean = clean_wiki_slug(slug)
    if clean is None:
        raise HTTPException(status_code=422, detail="invalid wiki slug")
    return clean


def _merge_graph(nodes_a, edges_a, nodes_b, edges_b, *, cap: int):
    """K8.2 (B2-a) — neighbors 결과에 page_conflicts 결과를 합류한다. 노드/엣지 dedup 키는
    게이트 `_map_records`와 **같은 함수**(`kg_node_dedup_key`/`kg_edge_dedup_key`)를 공유한다
    — 두 곳의 규약이 드리프트하지 않게(코드리뷰 #4/#5). 반환 `overflow`는 병합 노드 수가 cap에
    도달했는지 — 호출자가 truncated에 OR한다. 하드 truncate는 하지 않는다(FE가 표시 cap을
    가지며, 끊긴 엣지를 만들지 않게)."""
    seen_nodes = {kg_node_dedup_key(n.label, n.scope, n.id) for n in nodes_a}
    merged_nodes = list(nodes_a)
    for n in nodes_b:
        key = kg_node_dedup_key(n.label, n.scope, n.id)
        if key not in seen_nodes:
            seen_nodes.add(key)
            merged_nodes.append(n)
    seen_edges = {kg_edge_dedup_key(e.src, e.rel, e.dst, e.scope) for e in edges_a}
    merged_edges = list(edges_a)
    for e in edges_b:
        key = kg_edge_dedup_key(e.src, e.rel, e.dst, e.scope)
        if key not in seen_edges:
            seen_edges.add(key)
            merged_edges.append(e)
    return merged_nodes, merged_edges, len(merged_nodes) > cap


def _build_entity_teaser(start_slug: str, nodes: list[KgGraphNode]) -> EntityTeaser | None:
    """K9.2 (D-TZ B) — entity_neighbors 결과(시작 page p + 매개 :Entity + 이웃 WikiPage q)에서
    펼침-전 티저를 유도한다. 순수 함수(테스트 대상).

    - page_count = start와 다른 materialized WikiPage 수(다리로 엮인 다른 페이지).
    - entity_names = **비-person** 엔티티 display name(person 억제, §0 비목표/D-TZ B), 희소 우선
      (mention_count 오름차순) 상위 3개. person-only 페이지면 names=[] + person_only=True(옵션 A).
    - 이웃 페이지가 0이면 None(이 페이지엔 다리 없음 — calm 빈 상태 U3, 티저 미노출)."""
    neighbor_page_ids = {
        n.id
        for n in nodes
        if n.label == "WikiPage" and n.materialized and n.slug and n.slug != start_slug
    }
    if not neighbor_page_ids:
        return None
    entities = [n for n in nodes if n.label == "Entity"]
    non_person = sorted(
        (n for n in entities if n.entity_kind != "person"),
        key=lambda n: n.mention_count if n.mention_count is not None else 1_000_000,
    )
    names = [n.title for n in non_person if n.title][:3]
    return EntityTeaser(
        page_count=len(neighbor_page_ids),
        entity_names=names,
        person_only=bool(entities) and not non_person,
    )


def _graph_start_node_exists(slug: str, caller_id: UUID) -> bool:
    """K4/K7.2 graph endpoint의 404 판정 — **게이트와 동일한 scope 규칙**을 본다(코드리뷰).

    owner-scope ON: 게이트의 `resolve_slug`와 같은 owner-inclusive resolve(caller 자기
    personal + company, personal-first)로 본다. company-only 체크를 그대로 두면 owner의
    자기 personal page가 항상 404가 돼 K7.2 owner read 경로 전체가 prod에서 가려진다.
    OFF: v1 company-only(`_company_wiki_page_exists`, 불변)."""
    if kg_owner_scope_enabled():
        return resolve_slug(slug, caller_id, "WikiPage") is not None
    return _company_wiki_page_exists(slug)


def _company_wiki_page_exists(slug: str) -> bool:
    """K4 graph endpoint의 404 판정(flag-OFF) — `wiki_pages(slug, scope='company')` PG lookup.

    kind 불문이다(게이트 4단계와 동일 — 구현 명세 §6.2). v1 그래프는 company
    scope만 투영하므로 owner의 personal page slug는 merged view에 보여도 여기서
    404다(kg-model §4 — K7 이전의 의도된 fail-closed 경계)."""
    with db_session() as s:
        row = s.execute(
            select(wiki_pages_table.c.page_id)
            .where(wiki_pages_table.c.slug == slug, wiki_pages_table.c.scope == "company")
            .limit(1)
        ).first()
    return row is not None


def _wiki_page_exists(slug: str, *, scope: str, owner_id: UUID | None) -> bool:
    # 존재 판정 전용 — load_page(전체 markdown read+parse) 대신 같은 _path 규칙의
    # 파일 stat(store.exists)만 본다. 404/empty 시맨틱은 동일하다.
    clean = clean_wiki_slug(slug)
    if clean is None:
        return False
    return store.exists("page", clean, scope=scope, owner_id=owner_id)


def _start_of_day(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.min, tzinfo=UTC)


def _exclusive_next_day(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value + timedelta(days=1), time.min, tzinfo=UTC)
