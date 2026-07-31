"""P4.5a wiki page data-gap link surface."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.api.main import app
from orthus.db import session
from orthus.schemas.canonical import WikiPage
from orthus.settings import get_settings
from orthus.tables import users
from orthus.wiki import store as wiki_store
from orthus.wiki.gap import record_feedback, set_gap_status

client = TestClient(app)


def _write_page(
    user_id: uuid.UUID,
    slug: str,
    *,
    scope: str,
    owner_id: uuid.UUID | None = None,
) -> None:
    wiki_store.write_page(
        WikiPage(
            slug=slug,
            title=slug,
            definition=f"{slug} definition",
            overview=f"{slug} overview",
        ),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
        project="company",
    )


def test_page_data_gaps_returns_open_gaps_for_company_page(clean, user_id):
    _write_page(user_id, "company-policy", scope="company")
    open_gap = record_feedback(
        user_id,
        "회사 정책 근거 부족",
        context_wiki_slug="company-policy",
    )
    other_gap = record_feedback(
        user_id,
        "다른 페이지 근거 부족",
        context_wiki_slug="other-page",
    )
    closed_gap = record_feedback(
        user_id,
        "해결된 정책 근거 부족",
        context_wiki_slug="company-policy",
    )
    assert open_gap is not None
    assert other_gap is not None
    assert closed_gap is not None
    assert set_gap_status(user_id, closed_gap, "resolved") is not None

    response = client.get(
        "/wiki/pages/company-policy/data-gaps",
        headers={"X-User-Id": str(user_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["gap_id"] == str(open_gap)
    assert body["items"][0]["context_wiki_slug"] == "company-policy"
    assert body["items"][0]["status"] == "open"


def test_page_data_gaps_clamps_personal_owner(clean, user_id, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)
    other_user = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_user, display_name="Other User"))
        s.commit()
    _write_page(user_id, "personal-page", scope="personal", owner_id=user_id)
    _write_page(other_user, "personal-page", scope="personal", owner_id=other_user)
    own_gap = record_feedback(
        user_id,
        "내 개인 페이지 근거 부족",
        context_wiki_slug="personal-page",
    )
    other_gap = record_feedback(
        other_user,
        "다른 사람 페이지 근거 부족",
        context_wiki_slug="personal-page",
    )
    assert own_gap is not None
    assert other_gap is not None

    response = client.get(
        "/wiki/pages/personal-page/data-gaps",
        headers={"X-User-Id": str(user_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["gap_id"] == str(own_gap)
    assert body["items"][0]["question"] == "내 개인 페이지 근거 부족"


def test_page_data_gaps_returns_empty_for_federated_personal_page(clean, user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")

    response = client.get(
        "/wiki/pages/company-policy/data-gaps",
        headers={"X-User-Id": str(user_id)},
    )

    assert response.status_code == 200
    assert response.json() == {"count": 0, "items": []}


def test_page_data_gaps_rejects_malformed_slug(clean, user_id):
    response = client.get(
        "/wiki/pages/bad%20slug/data-gaps",
        headers={"X-User-Id": str(user_id)},
    )

    assert response.status_code == 422
