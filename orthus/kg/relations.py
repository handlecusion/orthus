"""K-series 관계 read 헬퍼 — `relation` enum을 기존 KG 템플릿으로 매핑해
`run_kg_template`을 1회 호출한다. orthus-mcp `kg_relations`/`entity_relations` 툴
(`POST /wiki/kg/query` 경유, cli /ask + 위임 에이전트)과 inline agentic /ask의
in-process Solar 툴이 **이 단일 진입점을 공유**해 매핑이 드리프트하지 않게 한다.

신규 Cypher/게이트 경로는 0이다 — 기존 typed 템플릿(neighbors / page_conflicts /
entity_neighbors / path_between / entity_mentions)을 그대로 쓴다. owner-scope 경계,
redaction, read-only, fail-open(예외 미전파) 불변식은 전부 `run_kg_template` 게이트가
보유하며 여기서 재구현하지 않는다(orthus/kg/gate.py).
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from orthus.kg.gate import KgQueryStatus, KgTemplateResult, run_kg_template

# 사람이 묻는 관계 의도 → typed 템플릿. slug 기반 4종 + name 기반 mentions 1종.
KgRelation = Literal["neighbors", "conflicts", "related", "path", "mentions"]

_SLUG_RELATION_TEMPLATE: dict[str, str] = {
    "neighbors": "neighbors",  # 주변 1-2 hop 지식 관계
    "conflicts": "page_conflicts",  # 이 페이지 claim들의 모순(CONFLICTS_WITH)
    "related": "entity_neighbors",  # 공유 엔티티로 엮인 다른 페이지
    "path": "path_between",  # 두 페이지 사이 최단 지식 경로
}

# supported=False(=KG 자체 미가용)로 수렴시킬 hard 사유. 입력/slug reject는 KG는 살아
# 있으므로 supported=True + reason(빈 결과)로 정직하게 돌려준다.
_HARD_UNAVAILABLE = {"kg_disabled", "kg_unavailable", "timeout", "internal_error"}


def _rejected(reason: str) -> KgTemplateResult:
    return KgTemplateResult(status=KgQueryStatus.REJECTED, reject_reason=reason)


def supported_from(result: KgTemplateResult) -> bool:
    """결과를 'KG 가용' 관점으로 해석한다. OK거나 입력/slug reject면 True(KG는 살아
    있고 결과만 비거나 입력이 나쁨), Neo4j down/timeout/driver 오류면 False."""
    if result.status is KgQueryStatus.OK:
        return True
    reason = result.reject_reason or ""
    if reason in _HARD_UNAVAILABLE or reason.startswith(("driver_error", "mapping_error")):
        return False
    return True


def run_kg_relation(
    *,
    user_id: UUID | None,
    relation: str,
    slug: str | None = None,
    slug_b: str | None = None,
    name: str | None = None,
    depth: int = 1,
    max_hops: int = 4,
) -> KgTemplateResult:
    """`relation`을 typed 템플릿으로 매핑해 게이트를 1회 호출한다.

    잘못된 입력(미지원 relation, slug/name 누락)은 게이트를 타지 않고 REJECTED로
    돌려준다 — 게이트와 같은 fail-open 계약(예외 미전파). depth/max_hops는 템플릿
    Literal 상한({1,2}/{2,3,4})에 맞게 clamp해 불필요한 invalid_params reject를 막는다.
    """
    if relation == "mentions":
        # name_norm 정규화는 단일 공개 진입점을 재사용(persist 경로와 동일 — 이중 정규화
        # 드리프트 방지). entity_mentions는 company-only data라 게이트가 owner 술어를 면제한다.
        from orthus.kg.entities import normalize_name_norm

        norm = normalize_name_norm(name or "")
        if not norm:
            return _rejected("missing_name")
        return run_kg_template(
            user_id=user_id, template_name="entity_mentions", params={"name_norm": norm}
        )

    template = _SLUG_RELATION_TEMPLATE.get(relation)
    if template is None:
        return _rejected("unknown_relation")
    if not slug:
        return _rejected("missing_slug")

    if relation == "path":
        if not slug_b:
            return _rejected("missing_slug_b")
        hops = 2 if max_hops <= 2 else 3 if max_hops == 3 else 4
        params: dict = {"slug_a": slug, "slug_b": slug_b, "max_hops": hops}
    elif relation == "neighbors":
        params = {"slug": slug, "depth": 2 if depth >= 2 else 1}
    else:  # conflicts, related — 단일 page slug
        params = {"slug": slug}
    return run_kg_template(user_id=user_id, template_name=template, params=params)
