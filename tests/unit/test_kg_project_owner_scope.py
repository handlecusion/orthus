"""K7.1 — owner-scope projection build() 경계 단위 테스트 (Neo4j/PG 불필요).

`build()`는 순수 함수라 owner 경계의 **투영 절반**(노드/엣지 scope·owner_id, ns_key
placeholder, cross-owner 격리)을 DB 없이 고정할 수 있다. 읽기 경로 술어(B1/B5/B6)는
K7.2 게이트 테스트에서 증명한다 — 여기서는 "투영이 애초에 cross-owner 노드를
만들지 않는다"(B3/B4)를 증명한다.
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
    _edge_scope_owner,
    _load_source_front,
    build,
)
from orthus.kg.schema import Label, Rel

_T = datetime(2026, 6, 1, tzinfo=UTC)
_OWNER_A = UUID("00000000-0000-4000-8000-00000000aaaa")
_OWNER_B = UUID("00000000-0000-4000-8000-00000000bbbb")
_CO_PAGE = UUID("00000000-0000-4000-8000-0000000000c0")
_A_PAGE = UUID("00000000-0000-4000-8000-0000000000a0")
_B_PAGE = UUID("00000000-0000-4000-8000-0000000000b0")
_C_DOC = UUID("00000000-0000-4000-8000-0000000000d0")
_B_DOC = UUID("00000000-0000-4000-8000-0000000000d1")
_FACT = UUID("00000000-0000-4000-8000-0000000000f0")


def test_edge_scope_owner_personal_wins():
    # B3 — 더 제한적인 끝점이 이긴다(personal). owner는 그 personal 끝점의 것.
    assert _edge_scope_owner("company", None, "company", None) == ("company", None)
    assert _edge_scope_owner("personal", _OWNER_A, "company", None) == ("personal", _OWNER_A)
    assert _edge_scope_owner("company", None, "personal", _OWNER_B) == ("personal", _OWNER_B)


def _node(plan, merge_value):
    return next(n for n in plan.nodes if n.merge_value == merge_value)


def test_personal_page_projects_scope_and_owner():
    rows = SourceRows(
        pages=[
            PageRow(
                page_id=_A_PAGE,
                slug="my-note",
                kind="page",
                title="My note",
                content_hash="h",
                project="atlas",
                updated_at=_T,
                scope="personal",
                owner_id=_OWNER_A,
            ),
        ],
        slug_map={
            ("00000000-0000-4000-8000-00000000aaaa", "my-note"): (
                _A_PAGE,
                "page",
                "personal",
                _OWNER_A,
            )
        },
        full=True,
    )
    plan = build(rows)
    node = _node(plan, str(_A_PAGE))
    assert node.props["scope"] == "personal"
    assert node.props["owner_id"] == str(_OWNER_A)  # 문자열(predicate $caller와 동형)
    assert node.props["ns_slug"] == f"{_OWNER_A}:my-note"
    # IN_PROJECT 엣지(personal page → company 허브)는 personal/owner-A(B3).
    in_proj = next(e for e in plan.edges if e.rel is Rel.IN_PROJECT)
    assert in_proj.props["scope"] == "personal"
    assert in_proj.props["owner_id"] == str(_OWNER_A)


def test_placeholder_ns_separation_cross_owner():
    """B4 — owner-A·owner-B·company가 같은 dangling slug를 가리키면 **세 개의 서로
    다른** ns_slug placeholder가 된다. owner-A의 링크는 owner-B 노드로 절대 해석되지
    않는다(resolve_dst는 company ∪ A만 본다)."""
    common = "shared-slug"
    rows = SourceRows(
        pages=[
            PageRow(
                page_id=_CO_PAGE,
                slug="co",
                kind="page",
                title="co",
                content_hash="h",
                project="atlas",
                updated_at=_T,
                scope="company",
                owner_id=None,
            ),
            PageRow(
                page_id=_A_PAGE,
                slug="a",
                kind="page",
                title="a",
                content_hash="h",
                project="atlas",
                updated_at=_T,
                scope="personal",
                owner_id=_OWNER_A,
            ),
            PageRow(
                page_id=_B_PAGE,
                slug="b",
                kind="page",
                title="b",
                content_hash="h",
                project="atlas",
                updated_at=_T,
                scope="personal",
                owner_id=_OWNER_B,
            ),
        ],
        links=[
            LinkRow(
                src_page_id=_CO_PAGE,
                src_slug="co",
                src_kind="page",
                dst_slug=common,
                rel="backlink",
                src_scope="company",
                src_owner_id=None,
            ),
            LinkRow(
                src_page_id=_A_PAGE,
                src_slug="a",
                src_kind="page",
                dst_slug=common,
                rel="backlink",
                src_scope="personal",
                src_owner_id=_OWNER_A,
            ),
            LinkRow(
                src_page_id=_B_PAGE,
                src_slug="b",
                src_kind="page",
                dst_slug=common,
                rel="backlink",
                src_scope="personal",
                src_owner_id=_OWNER_B,
            ),
        ],
        slug_map={
            ("company", "co"): (_CO_PAGE, "page", "company", None),
            (str(_OWNER_A), "a"): (_A_PAGE, "page", "personal", _OWNER_A),
            (str(_OWNER_B), "b"): (_B_PAGE, "page", "personal", _OWNER_B),
        },
        full=True,
    )
    plan = build(rows)
    ph = sorted(
        n.merge_value for n in plan.nodes if n.label is Label.WIKI_PAGE and n.merge_key == "ns_slug"
    )
    # 같은 slug지만 세 개의 분리된 placeholder 정체성(B4 cross-owner 존재 오라클 차단).
    assert ph == sorted([f"company:{common}", f"{_OWNER_A}:{common}", f"{_OWNER_B}:{common}"])
    assert plan.expected_keys is not None
    assert plan.expected_keys.placeholder_slugs == set(ph)
    # 각 placeholder 노드는 자기 네임스페이스의 scope/owner를 들고 있다.
    a_ph = _node(plan, f"{_OWNER_A}:{common}")
    assert a_ph.props["scope"] == "personal" and a_ph.props["owner_id"] == str(_OWNER_A)
    assert a_ph.props["slug"] == common  # 표시용 slug는 bare


def test_personal_link_resolves_to_company_not_other_owner():
    """personal-A 링크 dst가 company에 있으면 company 실체로 해석된다(공유 지식
    다리). owner-B에만 있으면 절대 해석되지 않고 A 네임스페이스 placeholder가 된다."""
    rows = SourceRows(
        pages=[
            PageRow(
                page_id=_CO_PAGE,
                slug="co-topic",
                kind="page",
                title="t",
                content_hash="h",
                project="atlas",
                updated_at=_T,
                scope="company",
                owner_id=None,
            ),
            PageRow(
                page_id=_B_PAGE,
                slug="b-secret",
                kind="page",
                title="t",
                content_hash="h",
                project="atlas",
                updated_at=_T,
                scope="personal",
                owner_id=_OWNER_B,
            ),
            PageRow(
                page_id=_A_PAGE,
                slug="a-note",
                kind="page",
                title="t",
                content_hash="h",
                project="atlas",
                updated_at=_T,
                scope="personal",
                owner_id=_OWNER_A,
            ),
        ],
        links=[
            # A가 company topic을 링크 → company 실체로 resolve(real, not placeholder).
            LinkRow(
                src_page_id=_A_PAGE,
                src_slug="a-note",
                src_kind="page",
                dst_slug="co-topic",
                rel="backlink",
                src_scope="personal",
                src_owner_id=_OWNER_A,
            ),
            # A가 B의 secret slug를 링크 → resolve 실패 → A 네임스페이스 placeholder.
            LinkRow(
                src_page_id=_A_PAGE,
                src_slug="a-note",
                src_kind="page",
                dst_slug="b-secret",
                rel="backlink",
                src_scope="personal",
                src_owner_id=_OWNER_A,
            ),
        ],
        slug_map={
            ("company", "co-topic"): (_CO_PAGE, "page", "company", None),
            (str(_OWNER_B), "b-secret"): (_B_PAGE, "page", "personal", _OWNER_B),
            (str(_OWNER_A), "a-note"): (_A_PAGE, "page", "personal", _OWNER_A),
        },
        full=True,
    )
    plan = build(rows)
    edges = {(e.rel, e.dst[0], e.dst[1]): e for e in plan.edges if e.rel is Rel.BACKLINK}
    # company topic으로 가는 엣지: real page_id dst, 엣지 scope=personal-A(min, B3).
    co_edge = edges[(Rel.BACKLINK, "page_id", str(_CO_PAGE))]
    assert co_edge.props["scope"] == "personal"
    assert co_edge.props["owner_id"] == str(_OWNER_A)
    # b-secret으로 가는 엣지: A 네임스페이스 placeholder — B 노드로 새지 않는다.
    b_ph_key = f"{_OWNER_A}:b-secret"
    assert (Rel.BACKLINK, "ns_slug", b_ph_key) in edges
    # B의 실체 page(_B_PAGE)로 가는 엣지는 존재하지 않는다(B4).
    assert (Rel.BACKLINK, "page_id", str(_B_PAGE)) not in edges
    ph_values = {n.merge_value for n in plan.nodes if n.merge_key == "ns_slug"}
    assert b_ph_key in ph_values
    assert "company:b-secret" not in ph_values  # company 네임스페이스로 새지 않음


def test_entity_nodes_and_edges_company_prefixed():
    """D2 entity-key 접두 슬롯 예약 — :Entity 노드 키와 MENTIONED_IN 엣지 끝점이
    'company:' 접두로 일치한다(불일치면 erasure가 노드를 놓치거나 엣지가 dangle)."""
    rows = SourceRows(
        pages=[
            PageRow(
                page_id=_CO_PAGE,
                slug="topic",
                kind="page",
                title="t",
                content_hash="h",
                project="atlas",
                updated_at=_T,
            ),
        ],
        entities=[
            EntityRow(
                entity_key="person:kim",
                entity_kind="person",
                name_norm="kim",
                display_name="Kim",
                first_seen=_T,
                last_seen=_T,
                updated_at=_T,
            ),
        ],
        mentions=[
            MentionRow(entity_key="person:kim", page_id=_CO_PAGE, page_kind="page", slug="topic")
        ],
        full=True,
    )
    plan = build(rows)
    ent = next(n for n in plan.nodes if n.label is Label.ENTITY)
    assert ent.merge_value == "company:person:kim"
    assert plan.expected_keys is not None
    assert plan.expected_keys.entity_keys == {"company:person:kim"}
    mention = next(e for e in plan.edges if e.rel is Rel.MENTIONED_IN)
    # 엣지 src 끝점도 접두 키 — 노드 merge_value와 일치.
    assert mention.src == ("entity_key", "company:person:kim")
    assert mention.props["scope"] == "company"


def _fact(scope, owner, doc_id) -> FactRow:
    return FactRow(
        row_id=_FACT,
        record_type="event",
        confidence="medium",
        source_doc_id=doc_id,
        scope=scope,
        owner_id=owner,
        updated_at=_T,
    )


def test_provenance_does_not_cross_owner():
    """B4 provenance 판 — personal-A fact는 personal-B doc로 EXTRACTED_FROM 엣지를
    만들 수 없다(doc_meta는 전 owner 평면 맵이라 가드 없으면 cross-owner 엣지 발생)."""
    rows = SourceRows(
        facts=[_fact("personal", _OWNER_A, _B_DOC)],
        doc_meta={_B_DOC: ("personal", _OWNER_B)},  # B 소유 doc
        full=True,
    )
    plan = build(rows)
    assert [e for e in plan.edges if e.rel is Rel.EXTRACTED_FROM] == []
    assert plan.counts.get("warning_provenance_cross_owner_skipped") == 1


def test_provenance_to_company_doc_allowed():
    # personal-A fact → company doc는 허용(공유 지식 다리). 엣지는 personal/A(B3 min).
    rows = SourceRows(
        facts=[_fact("personal", _OWNER_A, _C_DOC)],
        doc_meta={_C_DOC: ("company", None)},
        full=True,
    )
    plan = build(rows)
    e = next(e for e in plan.edges if e.rel is Rel.EXTRACTED_FROM)
    assert e.props["scope"] == "personal"
    assert e.props["owner_id"] == str(_OWNER_A)


def test_provenance_same_owner_allowed():
    # personal-A fact → personal-A doc는 허용(자기 네임스페이스).
    rows = SourceRows(
        facts=[_fact("personal", _OWNER_A, _B_DOC)],
        doc_meta={_B_DOC: ("personal", _OWNER_A)},  # A 소유 doc
        full=True,
    )
    plan = build(rows)
    assert any(e.rel is Rel.EXTRACTED_FROM for e in plan.edges)


def test_load_source_front_skips_personal_source():
    """personal source는 company store를 읽지 않고 보강을 생략한다(엉뚱한 provenance
    방지). load_source를 호출하지 않으므로 DB-free."""
    warnings: dict[str, int] = {}
    personal_src = PageRow(
        page_id=_A_PAGE,
        slug="s",
        kind="source",
        title="s",
        content_hash="h",
        project="atlas",
        updated_at=_T,
        scope="personal",
        owner_id=_OWNER_A,
    )
    front = _load_source_front([personal_src], warnings)
    assert front == {}
    assert warnings.get("personal_source_front_deferred") == 1


def test_personal_source_does_not_pick_up_colliding_company_front():
    """코드리뷰 #2 — source_front는 bare slug로 키잉된 company-only 맵이다. personal source
    페이지의 slug가 company source와 겹쳐도 build()는 company frontmatter를 집어선 안 된다
    (집으면 personal 노드에 company source_ref가 박히고 company doc로 가짜 provenance 엣지)."""
    slug = "shared-src-slug"
    rows = SourceRows(
        pages=[
            PageRow(
                page_id=_A_PAGE,
                slug=slug,
                kind="source",
                title="s",
                content_hash="h",
                project="atlas",
                updated_at=_T,
                scope="personal",
                owner_id=_OWNER_A,
            ),
        ],
        # 같은 slug의 company source frontmatter(=corpus_doc → company doc) — 충돌 미끼.
        source_front={slug: SourceFront(source_type="corpus_doc", source_ref=str(_C_DOC))},
        doc_meta={_C_DOC: ("company", None)},
        full=True,
    )
    plan = build(rows)
    node = _node(plan, str(_A_PAGE))
    # personal source 노드는 company frontmatter를 받지 않는다(deferral: None).
    assert node.props["source_type"] is None
    assert node.props["source_ref"] is None
    # 충돌 company doc로 가는 EXTRACTED_FROM 엣지도 없다.
    assert [e for e in plan.edges if e.rel is Rel.EXTRACTED_FROM] == []


def test_company_source_still_gets_its_front():
    """#2 가드가 company source의 정상 frontmatter/provenance는 그대로 둔다(회귀 방지)."""
    slug = "co-src"
    rows = SourceRows(
        pages=[
            PageRow(
                page_id=_CO_PAGE,
                slug=slug,
                kind="source",
                title="s",
                content_hash="h",
                project="atlas",
                updated_at=_T,
            ),
        ],
        source_front={slug: SourceFront(source_type="corpus_doc", source_ref=str(_C_DOC))},
        doc_meta={_C_DOC: ("company", None)},
        full=True,
    )
    plan = build(rows)
    node = _node(plan, str(_CO_PAGE))
    assert node.props["source_ref"] == str(_C_DOC)
    assert any(e.rel is Rel.EXTRACTED_FROM for e in plan.edges)


def test_every_node_carries_scope_and_owner_props():
    """코드리뷰 #4 — emit_node funnel 보장: build()가 내는 **모든** 노드가 scope/owner_id
    props를 든다(B1). 한 종류라도 누락하면 RLS 없는 그래프에서 술어 매칭이 깨진다."""
    rows = SourceRows(
        pages=[
            PageRow(
                page_id=_A_PAGE,
                slug="n",
                kind="page",
                title="n",
                content_hash="h",
                project="atlas",
                updated_at=_T,
                scope="personal",
                owner_id=_OWNER_A,
            ),
            PageRow(
                page_id=_CO_PAGE,
                slug="c",
                kind="source",
                title="c",
                content_hash="h",
                project="atlas",
                updated_at=_T,
            ),
        ],
        docs=[DocRow(doc_id=_C_DOC, source="notion", project="atlas", updated_at=_T)],
        facts=[_fact("personal", _OWNER_A, _C_DOC)],
        entities=[
            EntityRow(
                entity_key="person:kim",
                entity_kind="person",
                name_norm="kim",
                display_name="Kim",
                first_seen=_T,
                last_seen=_T,
                updated_at=_T,
            ),
        ],
        mentions=[
            MentionRow(entity_key="person:kim", page_id=_A_PAGE, page_kind="page", slug="n"),
        ],
        # owner-A 링크의 dangling dst → placeholder 노드 한 개 더(emit_node 경로).
        links=[
            LinkRow(
                src_page_id=_A_PAGE,
                src_slug="n",
                src_kind="page",
                dst_slug="ghost",
                rel="backlink",
                src_scope="personal",
                src_owner_id=_OWNER_A,
            ),
        ],
        slug_map={
            ("00000000-0000-4000-8000-00000000aaaa", "n"): (
                _A_PAGE,
                "page",
                "personal",
                _OWNER_A,
            )
        },
        doc_meta={_C_DOC: ("company", None)},
        full=True,
    )
    plan = build(rows)
    labels = {n.label for n in plan.nodes}
    # Project/WikiPage/WikiSource/Document/StructuredFact/Entity/placeholder 모두 포함.
    assert Label.PROJECT in labels and Label.ENTITY in labels
    assert any(n.props.get("materialized") is False for n in plan.nodes)  # placeholder
    for n in plan.nodes:
        assert "scope" in n.props, f"{n.label}/{n.merge_value} missing scope"
        assert "owner_id" in n.props, f"{n.label}/{n.merge_value} missing owner_id"
        assert n.props["scope"] in ("company", "personal")


def test_scope_filter_for_event_derives_from_event_scope():
    """코드리뷰 #5 — reload 필터는 이벤트의 저장된 scope에서 유도(live flag 미독)."""
    from orthus.kg.project import ScopeFilter, scope_filter_for_event

    assert scope_filter_for_event("company") is ScopeFilter.COMPANY
    assert scope_filter_for_event("personal") is ScopeFilter.COMPANY_PLUS_OWNER


def test_company_only_rows_emit_company_scope_everywhere():
    """flag-off 등가 — company row만 들어오면 모든 노드/엣지가 company/null이다
    (owner predicate가 company OR ...로 모두에게 보이는 v1 동작 보존)."""
    rows = SourceRows(
        pages=[
            PageRow(
                page_id=_CO_PAGE,
                slug="co",
                kind="page",
                title="co",
                content_hash="h",
                project="atlas",
                updated_at=_T,
            ),
        ],
        docs=[DocRow(doc_id=_A_PAGE, source="notion", project="atlas", updated_at=_T)],
        slug_map={("company", "co"): (_CO_PAGE, "page", "company", None)},
        doc_meta={_A_PAGE: ("company", None)},
        full=True,
    )
    plan = build(rows)
    for n in plan.nodes:
        assert n.props.get("scope", "company") == "company"
        assert n.props.get("owner_id") is None
    for e in plan.edges:
        assert e.props["scope"] == "company"
        assert e.props["owner_id"] is None
