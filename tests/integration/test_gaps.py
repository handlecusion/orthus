"""Data-gap backlog persistence + feedback suggestion (orthus/wiki/gap.py).

Postgres must be up on localhost:5433 (run `make up && make migrate`)."""

from __future__ import annotations

import uuid

from sqlalchemy import insert
from fastapi.testclient import TestClient

from orthus.api.main import app
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import WikiSourceRef, derive_wiki_links
from orthus.tables import users
from orthus.wiki.gap import (
    generate_suggestion,
    list_gaps,
    record_feedback,
    set_gap_status,
)
from orthus.wiki.qa import answer_from_hits

client = TestClient(app)


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        s.commit()
    return uid


def _hit(score: float) -> WikiSourceRef:
    return WikiSourceRef(page_slug="p", title="P", kind="page", excerpt="…", score=score)


def test_wiki_links_projection_dedups_and_orders():
    sources = [
        WikiSourceRef(page_slug="beta", title="Beta", kind="page", excerpt="b", score=0.7),
        WikiSourceRef(page_slug="alpha", title="Alpha", kind="page", excerpt="a", score=0.9),
        WikiSourceRef(
            page_slug="alpha", title="Alpha duplicate", kind="claim", excerpt="a2", score=0.4
        ),
        WikiSourceRef(
            page_slug="personal-note",
            title="Personal note",
            kind="page",
            excerpt="p",
            score=0.9,
            source_scope="personal",
        ),
    ]

    links = derive_wiki_links(sources)

    assert [link.slug for link in links] == ["alpha", "personal-note", "beta"]
    assert links[0].title == "Alpha"
    assert links[1].scope == "personal"
    assert derive_wiki_links([]) == []


def test_no_hits_records_gap(clean):
    uid = _make_user()
    result = answer_from_hits(uid, "회사 소개", [], learn=False)
    assert result.gap is not None
    assert result.gap.reason == "no_data"
    gaps = list_gaps(uid)
    assert len(gaps) == 1
    assert gaps[0].question == "회사 소개"
    assert gaps[0].hit_count == 1
    assert gaps[0].context_wiki_slug is None
    assert gaps[0].suggestion_status == "pending"


def test_no_hits_records_explicit_context_wiki_slug_and_preserves_existing(clean):
    uid = _make_user()
    answer_from_hits(
        uid,
        "회사 소개 페이지 보강",
        [],
        learn=False,
        context_wiki_slug="company-policy",
    )
    answer_from_hits(
        uid,
        "회사 소개 페이지 보강",
        [],
        learn=False,
        context_wiki_slug="other-page",
    )

    gaps = list_gaps(uid)
    assert len(gaps) == 1
    assert gaps[0].hit_count == 2
    assert gaps[0].context_wiki_slug == "company-policy"


def test_weak_retrieval_derives_context_wiki_slug_from_top_hit(clean):
    uid = _make_user()
    hit = WikiSourceRef(
        page_slug="source-page",
        title="Source Page",
        kind="page",
        excerpt="thin source",
        score=0.1,
    )
    answer_from_hits(
        uid,
        "소스가 부족한 질문",
        [hit],
        chat_model=MockChat(default="관련 설명은 있지만 충분하지 않습니다."),
        learn=False,
    )

    gaps = list_gaps(uid)
    assert len(gaps) == 1
    assert gaps[0].reason == "weak_retrieval"
    assert gaps[0].context_wiki_slug == "source-page"


def test_invalid_context_wiki_slug_is_ignored(clean):
    uid = _make_user()
    answer_from_hits(
        uid,
        "잘못된 slug 질문",
        [],
        learn=False,
        context_wiki_slug="../bad slug",
    )

    assert list_gaps(uid)[0].context_wiki_slug is None


def test_insufficient_grounding_records_and_dedups(clean):
    uid = _make_user()
    chat = MockChat(default="제공된 컨텍스트에 정보 없음.")
    # Same question asked twice -> one deduped backlog row, hit_count incremented.
    answer_from_hits(uid, "노바티 제품 정의", [_hit(1.0)], chat_model=chat, learn=False)
    r2 = answer_from_hits(uid, "노바티 제품 정의", [_hit(1.0)], chat_model=chat, learn=False)
    assert r2.gap is not None
    assert r2.gap.reason == "insufficient_grounding"
    gaps = list_gaps(uid)
    assert len(gaps) == 1
    assert gaps[0].hit_count == 2


def test_good_answer_records_nothing(clean):
    uid = _make_user()
    chat = MockChat(default="노바티는 아틀라스 매칭 서비스입니다. 기능 A B C.")
    result = answer_from_hits(uid, "노바티 소개", [_hit(0.9)], chat_model=chat, learn=False)
    assert result.gap is None
    assert [link.slug for link in result.wiki_links] == ["p"]
    assert list_gaps(uid) == []


def test_post_ask_context_wiki_slug_records_gap(clean, monkeypatch):
    uid = _make_user()
    monkeypatch.setattr("orthus.router.classify", lambda *a, **k: "wiki")
    monkeypatch.setattr("orthus.wiki.qa.retrieve", lambda *a, **k: [])

    response = client.post(
        "/ask",
        headers={"X-User-Id": str(uid)},
        json={"question": "컨텍스트 페이지에서 없는 질문", "context_wiki_slug": "page-context"},
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "wiki"
    gaps = list_gaps(uid)
    assert len(gaps) == 1
    assert gaps[0].context_wiki_slug == "page-context"


def test_post_wiki_ask_context_wiki_slug_records_gap(clean, monkeypatch):
    uid = _make_user()
    monkeypatch.setattr("orthus.wiki.qa.retrieve", lambda *a, **k: [])

    response = client.post(
        "/wiki/ask",
        headers={"X-User-Id": str(uid)},
        json={
            "question": "위키 전용 페이지에서 없는 질문",
            "context_wiki_slug": "wiki-page-context",
        },
    )

    assert response.status_code == 200
    gaps = list_gaps(uid)
    assert len(gaps) == 1
    assert gaps[0].context_wiki_slug == "wiki-page-context"


def test_feedback_generates_suggestion(clean):
    uid = _make_user()
    suggestion_json = (
        '{"target": "회사 Notion \'회사 개요\' 페이지", "connector": "notion", '
        '"sections": [{"title": "회사 개요", "items": ["한 줄 정의", "사업 영역"]}]}'
    )
    gap_id = record_feedback(uid, "아크메 회사 소개")
    assert gap_id is not None
    result = generate_suggestion(uid, gap_id, chat_model=MockChat(default=suggestion_json))
    assert result is not None
    assert result.suggestion_status == "ready"
    assert result.suggested_connector == "notion"
    assert result.source == "feedback"
    assert len(result.suggested_fields) == 1
    assert result.suggested_fields[0].title == "회사 개요"
    assert "한 줄 정의" in result.suggested_fields[0].items


def test_set_status_resolve(clean):
    uid = _make_user()
    answer_from_hits(uid, "없는 주제", [], learn=False)
    gap_id = list_gaps(uid)[0].gap_id
    updated = set_gap_status(uid, gap_id, "resolved")
    assert updated is not None
    assert updated.status == "resolved"
    assert list_gaps(uid, status="open") == []
    assert len(list_gaps(uid, status="resolved")) == 1


def test_suggestion_fallback_on_bad_json(clean):
    uid = _make_user()
    gap_id = record_feedback(uid, "이상한 질문")
    assert gap_id is not None
    # Non-JSON LLM output must still leave the row actionable (ready, with a section).
    result = generate_suggestion(uid, gap_id, chat_model=MockChat(default="not json at all"))
    assert result is not None
    assert result.suggestion_status == "ready"
    assert len(result.suggested_fields) >= 1


# --- K7.4 L6 — cross-scope missing_link은 owner-scoped로 영속(privacy, merge-blocking) -----
# 이 가드들은 PG 평면만 본다(영속 row의 scope/owner_id는 Postgres 사실) — neo4j 불필요.
# record_gap이 write-site에서 scope-tag된 anchor로 scope/owner를 파생하므로 실제 그래프
# 발화 없이도 단정 가능하다.

import pytest  # noqa: E402

from sqlalchemy import select as _select  # noqa: E402

from orthus.tables import data_gaps  # noqa: E402
from orthus.wiki.gap import _build_message, record_gap  # noqa: E402
from orthus.schemas.canonical import WikiGap  # noqa: E402


def _missing_link_gap() -> WikiGap:
    return WikiGap(
        detected=True,
        reason="missing_link",
        missing_topic="신제품 출시 일정",
        top_score=0.5,
        message=_build_message("신제품 출시 일정", "missing_link"),
    )


def _co_hit(slug: str) -> WikiSourceRef:
    return WikiSourceRef(
        page_slug=slug, title="C", kind="page", excerpt="…", score=0.9, source_scope="company"
    )


def _pers_hit(slug: str) -> WikiSourceRef:
    return WikiSourceRef(
        page_slug=slug, title="P", kind="page", excerpt="…", score=0.8, source_scope="personal"
    )


def _row_scope_owner(gap_id):
    with session() as s:
        row = s.execute(
            _select(data_gaps.c.scope, data_gaps.c.owner_id).where(data_gaps.c.gap_id == gap_id)
        ).first()
    return row.scope, row.owner_id


@pytest.fixture
def _central_owner_scope_on(monkeypatch):
    monkeypatch.setattr("orthus.wiki.gap._is_personal", lambda: False)
    monkeypatch.setattr("orthus.kg.client.kg_owner_scope_enabled", lambda: True)


def test_cross_scope_missing_link_persists_owner_scoped(clean, _central_owner_scope_on):
    uid = _make_user()
    gid = record_gap(uid, _missing_link_gap(), source_hits=[_co_hit("co-a"), _pers_hit("my-note")])
    assert gid is not None
    scope, owner = _row_scope_owner(gid)
    assert scope == "personal"  # L6 — owner-scoped, NOT company
    assert owner == uid


def test_company_company_missing_link_stays_company_scoped(clean, _central_owner_scope_on):
    uid = _make_user()
    gid = record_gap(uid, _missing_link_gap(), source_hits=[_co_hit("co-a"), _co_hit("co-b")])
    scope, owner = _row_scope_owner(gid)
    assert scope == "company"
    assert owner is None


def test_cross_scope_gap_invisible_to_other_users(clean, _central_owner_scope_on):
    owner_uid = _make_user()
    other_uid = _make_user()
    record_gap(owner_uid, _missing_link_gap(), source_hits=[_co_hit("co-a"), _pers_hit("my-note")])
    # owner는 자기 백로그에서 본다(_owner_read_predicate: company + 본인 personal).
    assert any(g.scope == "personal" for g in list_gaps(owner_uid))
    # 타 user에게는 owner의 cross-scope(personal) gap이 보이지 않는다(K7 owner-only 경계).
    assert all(g.scope != "personal" for g in list_gaps(other_uid))


def test_cross_scope_and_company_company_are_two_bounded_rows(clean, _central_owner_scope_on):
    # 같은 question_norm이 cross-scope·company-company로 갈리면 dedup 키(scope, owner, norm)가
    # 달라 2개 distinct row가 된다(2-row bounded — 무한 증가 아님). 키를 norm 단독으로 완화하면
    # owner-personal 관측이 company row로 UPDATE되어 L6 경계가 붕괴되므로 금지(이 테스트가 핀).
    uid = _make_user()
    g_cross = record_gap(uid, _missing_link_gap(), source_hits=[_co_hit("co-a"), _pers_hit("mine")])
    g_co = record_gap(uid, _missing_link_gap(), source_hits=[_co_hit("co-a"), _co_hit("co-b")])
    assert g_cross != g_co  # 같은 질문이지만 별개 row(서로 다른 scope 버킷)
    assert _row_scope_owner(g_cross) == ("personal", uid)
    assert _row_scope_owner(g_co) == ("company", None)
    # 같은 cross-scope를 다시 관측하면 같은 owner-personal row로 dedup(증가 아님).
    g_cross2 = record_gap(
        uid, _missing_link_gap(), source_hits=[_co_hit("co-a"), _pers_hit("mine")]
    )
    assert g_cross2 == g_cross


def test_owner_scope_off_missing_link_stays_company(clean, monkeypatch):
    # flag-OFF에서는 cross-scope 신호가 있어도 company-scope 유지(owner-scope 기능 미활성).
    monkeypatch.setattr("orthus.wiki.gap._is_personal", lambda: False)
    monkeypatch.setattr("orthus.kg.client.kg_owner_scope_enabled", lambda: False)
    uid = _make_user()
    gid = record_gap(uid, _missing_link_gap(), source_hits=[_co_hit("co-a"), _pers_hit("my-note")])
    assert _row_scope_owner(gid) == ("company", None)
