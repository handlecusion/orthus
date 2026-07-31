"""K2 — 그래프 스키마 계약: 라벨/관계 상수, 스키마 버전, projection row 모델.

projection·템플릿·테스트가 공유하는 단일 출처다 — Cypher 라벨/관계 문자열을
다른 모듈에 산재시키지 않는다(구현 명세 §2.5). projection 단계 간 통신은 plain
dict가 아니라 아래 Pydantic row 모델로만 한다(원칙 2 — canonical 슬롯 스키마).

그래프에는 본문을 저장하지 않는다 — slug/title/메타/hash/ref UUID만
(kg-model §1 PII 행). `KgNodeRow.props`에 markdown 본문/chunk가 들어가면
`test_no_body_text_in_graph` 회귀가 잡는다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

# 그래프 스키마 계약 버전. kg-model §2 스키마 변경 시 증가 → sync가 rebuild를
# 요구한다(구현 명세 §4.7 bump 정책: additive 변경은 bump하지 않고, 기존 키/속성
# 의미 변경만 bump + rebuild 강제).
#
# v2 (K7.1): placeholder MERGE 키가 slug → ns_key(scope,owner_id,slug)로 바뀌고
# (기존 키 의미 변경), 전 노드/엣지에 scope/owner_id 속성이 추가된다. placeholder
# 재키잉은 breaking이라 bump + rebuild 강제 대상이다(scope/owner_id 속성 추가
# 자체는 additive지만 같은 bump에 함께 탄다). 활성화 절차: 코드 머지(flag off,
# v2 포함) → sync는 version 불일치로 자동 거부 → kg-bootstrap → kg-rebuild
# (placeholder ns 재키잉 + scope props 소급) → flag on → 재기동(kg-k7-plan §6).
KG_SCHEMA_VERSION: int = 2

# (version, reason, breaking) — kg_monitor_summary가 현재 entry를 노출해 운영자가
# "graph at vN, code at vM (reason ...), rebuild required"를 빈 그래프 없이 본다
# (kg-k7-plan §1.3). 최신 entry가 KG_SCHEMA_VERSION과 일치해야 한다.
KG_SCHEMA_CHANGELOG: tuple[tuple[int, str, bool], ...] = (
    (1, "K2 initial company-scope projection", False),
    (2, "K7.1 owner-scope: placeholder ns_key rekey + scope/owner_id on all nodes/edges", True),
)


class Label(StrEnum):
    WIKI_PAGE = "WikiPage"
    WIKI_CLAIM = "WikiClaim"
    WIKI_SOURCE = "WikiSource"
    DOCUMENT = "Document"
    STRUCTURED_FACT = "StructuredFact"
    PROJECT = "Project"
    ENTITY = "Entity"  # K6
    KG_META = "KgMeta"
    OUTBOX_APPLIED = "OutboxApplied"


class Rel(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONFLICTS_WITH = "CONFLICTS_WITH"
    BACKLINK = "BACKLINK"
    DERIVED_FROM = "DERIVED_FROM"
    EXTRACTED_FROM = "EXTRACTED_FROM"
    IN_PROJECT = "IN_PROJECT"
    MENTIONED_IN = "MENTIONED_IN"  # K6
    RELATES_TO = "RELATES_TO"  # K6


WIKI_LINK_REL_MAP: dict[str, Rel] = {
    "supports": Rel.SUPPORTS,
    "conflicts": Rel.CONFLICTS_WITH,
    "backlink": Rel.BACKLINK,
    "derived_from": Rel.DERIVED_FROM,
}

# wiki_links 유래 rel 4종 — K3 `replace_page_link_edges`의 삭제 대상 경계이기도
# 하다(EXTRACTED_FROM/IN_PROJECT는 컬럼 유래라 제외, 구현 명세 §4.5).
WIKI_LINK_RELS: tuple[Rel, ...] = (
    Rel.SUPPORTS,
    Rel.CONFLICTS_WITH,
    Rel.BACKLINK,
    Rel.DERIVED_FROM,
)

# :Project 허브 노드 enum (kg-model §2) — SELECT 없음, 고정 4행.
PROJECTS: tuple[str, ...] = ("atlas", "nova", "orbit", "company")

# wiki_pages.kind → 노드 라벨 (task는 노드로 투영하지 않는다 — §4.2).
WIKI_KIND_LABEL_MAP: dict[str, Label] = {
    "page": Label.WIKI_PAGE,
    "claim": Label.WIKI_CLAIM,
    "source": Label.WIKI_SOURCE,
}


class KgNodeRow(BaseModel):
    """project.py 출력 노드 단위 — merge_key/merge_value가 MERGE 정체성이다."""

    label: Label
    merge_key: str  # "page_id" | "doc_id" | "row_id" | "name" | "slug"(placeholder)
    merge_value: str
    props: dict[str, Any] = Field(default_factory=dict)  # 본문 금지 — 메타만


class KgEdgeRow(BaseModel):
    """projection 출력 엣지 단위.

    src/dst는 (merge_key, merge_value)다. src_label/dst_label은 구현 명세 §2.5
    모델에 없던 K2 구현 추가 필드 — Neo4j 속성 인덱스가 라벨 단위라, MATCH에
    라벨이 없으면 엣지 batch가 전수 스캔이 된다(K2 PR 실측 교정, 계약 의미 불변).
    """

    rel: Rel
    src_label: Label
    src: tuple[str, str]
    dst_label: Label
    dst: tuple[str, str]
    props: dict[str, Any] = Field(default_factory=dict)
