"""KG claim 노드 title = display_title 투영 (Neo4j/PG 불필요, build() 순수함수).

claim 노드는 저장된 display_title(헤드라인)을 title 속성으로 복사하고, page/source
노드의 정상 title은 오염하지 않는다. display_title이 NULL이면 slug(=title) 폴백.
투영은 저장값 복사만 한다(LLM 0회).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from orthus.kg.project import PageRow, SourceRows, build
from orthus.kg.schema import Label

_T = datetime(2026, 6, 1, tzinfo=UTC)
_CLAIM_HL = UUID("00000000-0000-4000-8000-0000000001a1")
_CLAIM_NULL = UUID("00000000-0000-4000-8000-0000000001a2")
_PAGE = UUID("00000000-0000-4000-8000-0000000001a3")
_SOURCE = UUID("00000000-0000-4000-8000-0000000001a4")


def _rows() -> SourceRows:
    return SourceRows(
        pages=[
            PageRow(
                page_id=_CLAIM_HL,
                slug="qa-ae4e5c20-claim",
                kind="claim",
                title="qa-ae4e5c20-claim",  # title=slug(SoR 불변)
                confidence="low",
                display_title="주간 회고 문서에 계획·회고 항목이 모두 미입력이다",
                content_hash="h1",
                project="atlas",
                updated_at=_T,
            ),
            PageRow(
                page_id=_CLAIM_NULL,
                slug="qa-deadbeef-claim",
                kind="claim",
                title="qa-deadbeef-claim",
                confidence="low",
                display_title=None,  # 헤드라인 없음 → slug 폴백
                content_hash="h2",
                project="atlas",
                updated_at=_T,
            ),
            PageRow(
                page_id=_PAGE,
                slug="vacation-policy",
                kind="page",
                title="휴가 정책",  # 정상 title
                confidence=None,
                display_title=None,
                content_hash="h3",
                project="atlas",
                updated_at=_T,
            ),
            PageRow(
                page_id=_SOURCE,
                slug="src-x",
                kind="source",
                title="원본 문서",
                confidence=None,
                display_title=None,
                content_hash="h4",
                project="atlas",
                updated_at=_T,
            ),
        ],
    )


def _node_props(plan, label, page_id):
    return next(n.props for n in plan.nodes if n.label is label and n.merge_value == str(page_id))


def test_claim_node_title_uses_display_title():
    plan = build(_rows())
    props = _node_props(plan, Label.WIKI_CLAIM, _CLAIM_HL)
    assert props["title"] == "주간 회고 문서에 계획·회고 항목이 모두 미입력이다"
    # slug 속성은 여전히 내부 식별자로 보존된다.
    assert props["slug"] == "qa-ae4e5c20-claim"


def test_claim_node_title_falls_back_to_slug_when_null():
    plan = build(_rows())
    props = _node_props(plan, Label.WIKI_CLAIM, _CLAIM_NULL)
    # display_title NULL → 기존 title(=slug) 폴백.
    assert props["title"] == "qa-deadbeef-claim"


def test_page_and_source_titles_not_overwritten():
    plan = build(_rows())
    page = _node_props(plan, Label.WIKI_PAGE, _PAGE)
    source = _node_props(plan, Label.WIKI_SOURCE, _SOURCE)
    assert page["title"] == "휴가 정책"
    assert source["title"] == "원본 문서"


def test_deterministic():
    assert build(_rows()).model_dump() == build(_rows()).model_dump()
