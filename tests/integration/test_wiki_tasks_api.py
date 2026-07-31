"""Wiki task review API."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from orthus.api.main import app
from orthus.schemas.canonical import WikiTask
from orthus.settings import get_settings
from orthus.wiki import store

client = TestClient(app)


def test_wiki_tasks_list_and_resolve(user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    task = WikiTask(
        slug="conflict-nova-status",
        kind="conflict",
        description="Nova status claims disagree.",
        related=["claim-a", "claim-b"],
        created_at=datetime(2026, 5, 31, tzinfo=UTC),
        resolved=False,
    )
    store.write_task(task, user_id=user_id, scope="company", project="nova")

    listed = client.get("/wiki/tasks", headers={"X-User-Id": str(user_id)})

    assert listed.status_code == 200
    # 목록의 첫 항목이 아니라 자기 slug를 찾아 단언한다 — "저장소가 비어 있다"는
    # 가정에 기대지 않는다(정렬은 (resolved, created_at) 오름차순).
    items = {item["slug"]: item for item in listed.json()}
    assert "conflict-nova-status" in items
    assert items["conflict-nova-status"]["resolved"] is False

    resolved = client.patch(
        "/wiki/tasks/conflict-nova-status",
        json={"resolved": True},
        headers={"X-User-Id": str(user_id)},
    )

    assert resolved.status_code == 200
    assert resolved.json()["resolved"] is True
    loaded = store.load_task("conflict-nova-status", scope="company")
    assert loaded is not None
    assert loaded.resolved is True


def test_personal_wiki_task_resolve(user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    task = WikiTask(
        slug="personal-open-question",
        kind="open_question",
        description="Personal node task needs review.",
        related=["personal-page"],
        created_at=datetime(2026, 6, 7, tzinfo=UTC),
        resolved=False,
    )
    store.write_task(task, user_id=user_id, scope="personal", owner_id=user_id, project="company")

    listed = client.get("/wiki/tasks", headers={"X-User-Id": str(user_id)})

    assert listed.status_code == 200
    items = {item["slug"]: item for item in listed.json()}
    assert "personal-open-question" in items
    assert items["personal-open-question"]["resolved"] is False

    resolved = client.patch(
        "/wiki/tasks/personal-open-question",
        json={"resolved": True},
        headers={"X-User-Id": str(user_id)},
    )

    assert resolved.status_code == 200
    assert resolved.json()["resolved"] is True
    loaded = store.load_task("personal-open-question", scope="personal", owner_id=user_id)
    assert loaded is not None
    assert loaded.resolved is True


def test_wiki_tasks_filter(user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    store.write_task(
        WikiTask(
            slug="open-question",
            kind="open_question",
            description="Missing answer.",
            created_at=datetime(2026, 5, 30, tzinfo=UTC),
            resolved=False,
        ),
        user_id=user_id,
        scope="company",
    )
    store.write_task(
        WikiTask(
            slug="resolved-conflict",
            kind="conflict",
            description="Already handled.",
            created_at=datetime(2026, 5, 29, tzinfo=UTC),
            resolved=True,
        ),
        user_id=user_id,
        scope="company",
    )

    listed = client.get(
        "/wiki/tasks?resolved=false&kind=open_question",
        headers={"X-User-Id": str(user_id)},
    )

    assert listed.status_code == 200
    items = listed.json()
    assert "open-question" in [item["slug"] for item in items]
    assert all(item["kind"] == "open_question" for item in items)
    assert all(item["resolved"] is False for item in items)
