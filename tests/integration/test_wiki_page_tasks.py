"""P4.5b wiki page WikiTask link surface."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.api.deps import get_current_user
from orthus.api.main import app
from orthus.auth import AuthenticatedUser
from orthus.db import session
from orthus.schemas.canonical import WikiPage, WikiTask
from orthus.settings import get_settings
from orthus.tables import users
from orthus.wiki import store as wiki_store

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated_wiki_store(clean, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)


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


def _write_task(
    user_id: uuid.UUID,
    slug: str,
    *,
    related: list[str],
    created_at: datetime,
    scope: str,
    owner_id: uuid.UUID | None = None,
    resolved: bool = False,
) -> None:
    wiki_store.write_task(
        WikiTask(
            slug=slug,
            kind="open_question",
            description=f"Need source for {slug}",
            related=related,
            created_at=created_at,
            resolved=resolved,
        ),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
        project="company",
    )


def test_page_tasks_returns_open_tasks_for_company_page(clean, user_id):
    base = datetime(2026, 6, 7, tzinfo=UTC)
    _write_page(user_id, "company-policy", scope="company")
    _write_page(user_id, "other-page", scope="company")
    _write_task(
        user_id,
        "task-linked",
        related=["company-policy", "claim-only", "other page"],
        created_at=base,
        scope="company",
    )
    _write_task(
        user_id,
        "task-resolved",
        related=["company-policy"],
        created_at=base + timedelta(minutes=1),
        scope="company",
        resolved=True,
    )
    _write_task(
        user_id,
        "task-other-page",
        related=["other-page"],
        created_at=base + timedelta(minutes=2),
        scope="company",
    )

    response = client.get(
        "/wiki/pages/company-policy/tasks",
        headers={"X-User-Id": str(user_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["slug"] == "task-linked"
    assert body["items"][0]["related"] == ["company-policy"]
    assert body["items"][0]["resolved"] is False


def test_page_tasks_caps_and_orders_by_created_desc(clean, user_id):
    base = datetime(2026, 6, 7, tzinfo=UTC)
    _write_page(user_id, "company-policy", scope="company")
    for index in range(22):
        _write_task(
            user_id,
            f"task-{index:02d}",
            related=["company-policy"],
            created_at=base + timedelta(minutes=index),
            scope="company",
        )

    response = client.get(
        "/wiki/pages/company-policy/tasks",
        headers={"X-User-Id": str(user_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 22
    assert len(body["items"]) == 20
    assert body["items"][0]["slug"] == "task-21"
    assert body["items"][-1]["slug"] == "task-02"


def test_page_tasks_clamps_personal_owner(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    other_user = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_user, display_name="Other User"))
        s.commit()
    base = datetime(2026, 6, 7, tzinfo=UTC)
    _write_page(user_id, "personal-page", scope="personal", owner_id=user_id)
    _write_page(other_user, "personal-page", scope="personal", owner_id=other_user)
    _write_task(
        user_id,
        "task-own",
        related=["personal-page"],
        created_at=base,
        scope="personal",
        owner_id=user_id,
    )
    _write_task(
        other_user,
        "task-other-owner",
        related=["personal-page"],
        created_at=base + timedelta(minutes=1),
        scope="personal",
        owner_id=other_user,
    )

    response = client.get(
        "/wiki/pages/personal-page/tasks",
        headers={"X-User-Id": str(user_id)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["slug"] == "task-own"


def test_page_tasks_returns_empty_for_federated_personal_page(clean, user_id):
    settings = get_settings()
    settings.node_kind = "personal"
    settings.node_id = "personal-a"
    _write_page(user_id, "company-policy", scope="company")
    _write_task(
        user_id,
        "task-company",
        related=["company-policy"],
        created_at=datetime(2026, 6, 7, tzinfo=UTC),
        scope="company",
    )

    response = client.get(
        "/wiki/pages/company-policy/tasks",
        headers={"X-User-Id": str(user_id)},
    )

    assert response.status_code == 200
    assert response.json() == {"count": 0, "items": []}


def test_page_tasks_rejects_malformed_slug(clean, user_id):
    response = client.get(
        "/wiki/pages/bad%20slug/tasks",
        headers={"X-User-Id": str(user_id)},
    )

    assert response.status_code == 422


def test_page_tasks_requires_node_operator_for_session_member(clean, user_id):
    def member_user():
        return AuthenticatedUser(
            user_id=user_id,
            auth_mode="session",
            display_name="Member",
            email="member@example.com",
            role="member",
            node_id="company",
        )

    app.dependency_overrides[get_current_user] = member_user
    try:
        response = client.get("/wiki/pages/company-policy/tasks")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 403
