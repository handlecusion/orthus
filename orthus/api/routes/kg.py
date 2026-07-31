"""전용 지식그래프(KG) 화면 엔드포인트.

기존 KG 읽기(`/wiki/kg/footprint`, `/wiki/pages/{slug}/graph`, `/wiki/graph/expand`,
`/wiki/kg/query`)는 wiki 라우터에 그대로 두고, 전용 `/dashboard/graph` 화면이 헤더에 쓰는
company-scope 집계만 여기 새 표면으로 노출한다. 검색→시드 탐색은 기존 `/wiki/search` +
`/wiki/pages/{slug}/graph` + `/wiki/graph/expand`를 FE에서 재사용하므로 추가 엔드포인트가
없다(그 경로들은 이미 게이트/tripwire/audit 의무를 진다).

read-only, K-series 정합 표면이다 — 새 write 경로/raw Cypher 입력이 없고, 집계도
`company_kg_overview`(gate.py)가 run_read + audit("kg.overview") + kg_query_runs 규율을
공유한다. 인증은 다른 KG 읽기와 동일한 dual-auth이며, 응답은 count만 담아 page 본문 이하의
민감도라 operator 가드를 걸지 않는다."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from orthus.api.knowledge_token import get_session_user_or_knowledge_token
from orthus.auth import AuthenticatedUser
from orthus.kg.gate import (
    KgQueryStatus,
    company_kg_overview,
    kg_rebuild_in_progress,
    run_kg_template,
)
from orthus.schemas.canonical import KgOverview, KgRelationResponse
from orthus.settings import get_settings

router = APIRouter(prefix="/kg", tags=["kg"])


@router.get("/overview", response_model=KgOverview)
def get_kg_overview(
    response: Response,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> KgOverview:
    """company-scope KG 집계(노드/엣지/라벨별/엔티티/모순). 전용 지식그래프 화면 헤더용.

    fail-open: flag-off는 `disabled`, full rebuild 중은 `rebuilding`(half-projected 오노출
    보류), Neo4j 미가용/측정 실패는 `unavailable`(모두 200 — 절대 500 없음). `company_kg_overview`
    가 flag-off도 내부에서 `disabled`로 처리하므로 rebuild만 endpoint에서 선판정한다(footprint
    endpoint와 동형). 캐시 금지는 rebuild/ready 상태가 stale로 굳지 않게 + 기존 KG 엔드포인트와
    통일(company 집계라 personal 혼입은 없지만 일관성 우선)."""
    response.headers["Cache-Control"] = "private, no-store"
    if kg_rebuild_in_progress():
        return KgOverview(status="rebuilding")
    return company_kg_overview()


@router.get("/graph", response_model=KgRelationResponse)
def get_kg_entity_graph(
    response: Response,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> KgRelationResponse:
    """전용 지식그래프 화면 랜딩 그래프 — company 개체(Entity) co-mention 지도.

    검색 없이 진입하자마자 회사 지식의 대표 그래프(가장 많이 언급된 개체와 그 사이 연결)를
    바로 보여준다. FE 탐색기가 이걸 시드로 받아 렌더하고, 노드를 더블클릭하면 나머지로
    펼쳐진다(전체 14k+ 노드는 렌더 불가라 top-N 개체로 압축). 모든 그래프 읽기와 동일하게
    게이트(`run_kg_template`)를 통과해 kg_query_runs + audit("kg.retrieve")를 남긴다.

    fail-open: flag-off/rebuild/Neo4j 미가용은 `supported=false`(200) — 절대 500 없음.
    company_always 템플릿이라 owner-scope 무관하게 company 데이터만 본다."""
    response.headers["Cache-Control"] = "private, no-store"
    if not get_settings().kg_enabled:
        return KgRelationResponse(supported=False, reason="kg_disabled")
    if kg_rebuild_in_progress():
        return KgRelationResponse(supported=False, reason="kg_unavailable")
    result = run_kg_template(
        user_id=current.user_id,
        template_name="entity_overview",
        params={},
    )
    if result.status is not KgQueryStatus.OK:
        return KgRelationResponse(supported=False, reason="kg_unavailable")
    return KgRelationResponse(
        supported=True,
        relation="entity_overview",
        truncated=result.truncated,
        nodes=list(result.nodes),
        edges=list(result.edges),
    )
