"""K2 — projection build() 결정론 unit 테스트 (Neo4j/PG 불필요).

`project.build()`는 순수 함수다: 같은 SourceRows 입력이면 같은 plan이 나온다
(구현 명세 §4.1 — batch/sync/outbox 세 경로가 같은 변환을 지나는 계약의 근거).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from orthus.kg.project import (
    DocRow,
    EntityRow,
    FactRow,
    LinkRow,
    MentionRow,
    PageRow,
    SourceFront,
    SourceRows,
    TaskMeta,
    build,
)
from orthus.kg.schema import Label, Rel

_T = datetime(2026, 6, 1, tzinfo=UTC)
_PAGE = UUID("00000000-0000-4000-8000-00000000000a")
_CLAIM_A = UUID("00000000-0000-4000-8000-00000000000b")
_CLAIM_B = UUID("00000000-0000-4000-8000-00000000000c")
_SOURCE = UUID("00000000-0000-4000-8000-00000000000d")
_DOC = UUID("00000000-0000-4000-8000-00000000000e")
_FACT = UUID("00000000-0000-4000-8000-00000000000f")


def _rows() -> SourceRows:
    return SourceRows(
        pages=[
            PageRow(
                page_id=_PAGE,
                slug="page-x",
                kind="page",
                title="Page X",
                confidence=None,
                content_hash="h1",
                project="atlas",
                updated_at=_T,
            ),
            PageRow(
                page_id=_CLAIM_A,
                slug="claim-a",
                kind="claim",
                title="claim-a",
                confidence="high",
                content_hash="h2",
                project="atlas",
                updated_at=_T,
            ),
            PageRow(
                page_id=_CLAIM_B,
                slug="claim-b",
                kind="claim",
                title="claim-b",
                confidence="low",
                content_hash="h3",
                project="atlas",
                updated_at=_T,
            ),
            PageRow(
                page_id=_SOURCE,
                slug="source-s",
                kind="source",
                title="Source S",
                confidence=None,
                content_hash="h4",
                project="atlas",
                updated_at=_T,
            ),
        ],
        docs=[
            DocRow(
                doc_id=_DOC,
                source="notion",
                project="atlas",
                title="Doc D",  # 노드 표시 텍스트(본문 아님)
                updated_at=_T,
            ),
        ],
        facts=[
            FactRow(
                row_id=_FACT,
                record_type="event",
                confidence="medium",
                source_doc_id=_DOC,
                record_key="evt-001",  # 노드 표시 텍스트(본문 아님)
                valid_until_raw="not-a-datetime",  # 파싱 실패 → null + warning(§4.9)
                updated_at=_T,
            ),
        ],
        links=[
            LinkRow(
                src_page_id=_PAGE,
                src_slug="page-x",
                src_kind="page",
                dst_slug="missing-page",  # dangling → placeholder(§4.4)
                rel="backlink",
            ),
            LinkRow(
                src_page_id=_CLAIM_A,
                src_slug="claim-a",
                src_kind="claim",
                dst_slug="claim-b",
                rel="conflicts",
            ),
            LinkRow(
                src_page_id=_CLAIM_A,
                src_slug="claim-a",
                src_kind="claim",
                dst_slug="source-s",
                rel="supports",
            ),
        ],
        source_front={"source-s": SourceFront(source_type="corpus_doc", source_ref=str(_DOC))},
        conflict_index={
            # K8.6 — 키는 (namespace, slug_a, slug_b). 이 fixture는 company-only.
            ("company", "claim-a", "claim-b"): TaskMeta(
                detected_at=_T, reason="모순", status="UNRESOLVED"
            )
        },
        # K7 v2: slug_map은 (namespace, slug) → (page_id, kind, scope, owner_id).
        # 전부 company 네임스페이스(이 fixture는 company-only).
        slug_map={
            ("company", "page-x"): (_PAGE, "page", "company", None),
            ("company", "claim-a"): (_CLAIM_A, "claim", "company", None),
            ("company", "claim-b"): (_CLAIM_B, "claim", "company", None),
            ("company", "source-s"): (_SOURCE, "source", "company", None),
        },
        doc_meta={_DOC: ("company", None)},
        full=True,
    )


def test_project_build_deterministic():
    plan1 = build(_rows())
    plan2 = build(_rows())
    assert plan1.model_dump() == plan2.model_dump()


def test_build_plan_contents():
    plan = build(_rows())

    # K7 v2: placeholder는 ns_slug로 키잉된다(company → "company:<slug>").
    placeholders = [
        n for n in plan.nodes if n.label is Label.WIKI_PAGE and n.merge_key == "ns_slug"
    ]
    assert [n.merge_value for n in placeholders] == ["company:missing-page"]
    assert placeholders[0].props == {
        "ns_slug": "company:missing-page",
        "slug": "missing-page",
        "materialized": False,
        "scope": "company",
        "owner_id": None,
    }

    conflict = next(e for e in plan.edges if e.rel is Rel.CONFLICTS_WITH)
    assert conflict.props["status"] == "UNRESOLVED"
    assert conflict.props["conflict_reason"] == "모순"
    assert conflict.props["detected_at"] == _T
    assert conflict.props["resolved_favoring_id"] is None

    provenance = [e for e in plan.edges if e.rel is Rel.EXTRACTED_FROM]
    assert {(e.src_label, e.dst_label) for e in provenance} == {
        (Label.WIKI_SOURCE, Label.DOCUMENT),
        (Label.STRUCTURED_FACT, Label.DOCUMENT),
    }

    fact = next(n for n in plan.nodes if n.label is Label.STRUCTURED_FACT)
    assert fact.props["valid_until"] is None
    assert plan.counts["warning_valid_until_parse_failed"] == 1

    # 표시 텍스트(본문 아님, 불변식 5) — read 경로가 UUID fallback 대신 실제 이름을 쓴다.
    doc = next(n for n in plan.nodes if n.label is Label.DOCUMENT)
    assert doc.props["title"] == "Doc D"
    assert fact.props["title"] == "evt-001"

    assert plan.expected_keys is not None
    assert plan.expected_keys.placeholder_slugs == {"company:missing-page"}
    # 라벨별 분리 — kind-flip 시 구 라벨 잔존을 prune이 지우기 위한 계약.
    assert str(_PAGE) in plan.expected_keys.page_ids_by_label[Label.WIKI_PAGE.value]
    assert str(_CLAIM_A) in plan.expected_keys.page_ids_by_label[Label.WIKI_CLAIM.value]
    assert str(_PAGE) not in plan.expected_keys.page_ids_by_label[Label.WIKI_CLAIM.value]
    # IN_PROJECT: page + document 2건이 edge_triples에 들어간다.
    assert (str(_PAGE), "IN_PROJECT", "atlas") in plan.expected_keys.edge_triples
    assert (str(_DOC), "IN_PROJECT", "atlas") in plan.expected_keys.edge_triples


def test_build_unmatched_conflict_pair_gets_null_props():
    rows = _rows()
    rows.conflict_index = {}
    plan = build(rows)
    conflict = next(e for e in plan.edges if e.rel is Rel.CONFLICTS_WITH)
    assert conflict.props["status"] == "UNRESOLVED"
    assert conflict.props["detected_at"] is None
    assert conflict.props["conflict_reason"] is None


def _entity(kind: str, name_norm: str) -> EntityRow:
    return EntityRow(
        entity_key=f"{kind}:{name_norm}",
        entity_kind=kind,
        name_norm=name_norm,
        display_name=name_norm.title(),
        first_seen=_T,
        last_seen=_T,
        updated_at=_T,
    )


def test_build_entity_mention_count_is_page_only():
    """K9 (B1/R3) — Entity 노드 `mention_count`는 **page-only** 집계다(claim 제외). 다리
    Cypher의 `q:WikiPage`(page-kind만, claim은 :WikiClaim) denominator와 일치시켜 threshold
    튜닝이 어긋나지 않게 한다. page+claim 전체를 세면 안 된다."""
    # person:kim → page 2개 + claim 1개 mention. mention_count는 page-only = 2.
    # org:acme → page 1개만. = 1.
    rows = SourceRows(
        full=True,
        entities=[_entity("person", "kim"), _entity("org", "acme")],
        mentions=[
            MentionRow(entity_key="person:kim", page_id=_PAGE, page_kind="page", slug="page-x"),
            MentionRow(entity_key="person:kim", page_id=_CLAIM_A, page_kind="page", slug="page-y"),
            MentionRow(
                entity_key="person:kim", page_id=_CLAIM_B, page_kind="claim", slug="claim-b"
            ),
            MentionRow(entity_key="org:acme", page_id=_PAGE, page_kind="page", slug="page-x"),
        ],
    )
    plan = build(rows)
    by_key = {n.merge_value: n.props for n in plan.nodes if n.label is Label.ENTITY}
    assert by_key["company:person:kim"]["mention_count"] == 2  # claim mention 제외
    assert by_key["company:org:acme"]["mention_count"] == 1
    # scope/owner는 emit_node가 일괄 SET(company-only) — wire predicate 매칭 보장.
    assert by_key["company:person:kim"]["scope"] == "company"
    assert by_key["company:person:kim"]["owner_id"] is None


def test_build_entity_mention_count_zero_when_no_page_mentions():
    """page mention이 전무한 엔티티(claim에만 언급)는 mention_count=0 — KeyError 아님."""
    rows = SourceRows(
        full=True,
        entities=[_entity("system", "neo4j")],
        mentions=[
            MentionRow(
                entity_key="system:neo4j", page_id=_CLAIM_A, page_kind="claim", slug="claim-a"
            )
        ],
    )
    plan = build(rows)
    ent = next(n for n in plan.nodes if n.label is Label.ENTITY)
    assert ent.props["mention_count"] == 0
