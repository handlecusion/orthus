"""Personal-board notification read surface (PR-N1).

Owner-scope read-only mirrors of the mail notify-state precedent:
`GET /personal-board/notify-state` and `GET /personal-board/notifications`.
Calendar-materialized auto-rows (`source_kind='calendar'`) never fire
notifications; the caller sees only their own workspace rows; a caller without
a workspace gets the zero-state and no workspace is created on read.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from orthus.api.main import app
from orthus.db import session
from orthus.personal_board import (
    board_notify_state,
    ensure_workspace,
    list_board_notifications,
)
from orthus.settings import get_settings
from orthus.tables import personal_board_tasks, personal_board_workspaces, users

client = TestClient(app)

NODE_ID = "personal-a"


@pytest.fixture
def personal_node(clean, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", NODE_ID)


def _user(display_name: str) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name=display_name))
        s.commit()
    return uid


def _task(
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    title: str,
    created_at: datetime,
    source_kind: str = "manual",
    status: str = "open",
) -> uuid.UUID:
    task_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(personal_board_tasks).values(
                task_id=task_id,
                workspace_id=workspace_id,
                user_id=user_id,
                title=title,
                status=status,
                source_kind=source_kind,
                # CHECK: exactly one of scheduled_date / backlog_bucket_id.
                scheduled_date=created_at.date(),
                created_at=created_at,
            )
        )
        s.commit()
    return task_id


def _seed(owner: uuid.UUID, other: uuid.UUID) -> dict[str, uuid.UUID]:
    t = lambda d: datetime(2026, 1, d, tzinfo=UTC)  # noqa: E731
    ws_owner = ensure_workspace(owner, NODE_ID)["workspace_id"]
    ws_other = ensure_workspace(other, NODE_ID)["workspace_id"]
    ids = {}
    ids["a"] = _task(workspace_id=ws_owner, user_id=owner, title="티켓 A", created_at=t(1))
    ids["b"] = _task(workspace_id=ws_owner, user_id=owner, title="티켓 B", created_at=t(3))
    # Excluded: calendar-materialized auto-row (newest of the owner's rows).
    ids["cal"] = _task(
        workspace_id=ws_owner,
        user_id=owner,
        title="캘린더 자동 행",
        created_at=t(5),
        source_kind="calendar",
    )
    # NOT visible to owner: another user's ticket (newest overall).
    ids["c"] = _task(workspace_id=ws_other, user_id=other, title="남의 티켓 C", created_at=t(7))
    return ids


def test_notify_state_excludes_calendar_and_counts_own_rows_only(personal_node):
    owner, other = _user("owner"), _user("other")
    _seed(owner, other)

    state = board_notify_state(owner, NODE_ID)

    # A + B; excludes calendar auto-row and other's ticket C.
    assert state.total == 2
    assert state.latest_at == datetime(2026, 1, 3, tzinfo=UTC)
    assert state.latest_title == "티켓 B"


def test_notify_state_zero_state_creates_no_workspace(personal_node):
    stranger = _user("stranger")

    state = board_notify_state(stranger, NODE_ID)

    assert state.total == 0
    assert state.latest_at is None
    assert state.latest_title is None
    with session() as s:
        count = s.execute(
            select(func.count()).where(
                personal_board_workspaces.c.user_id == stranger,
                personal_board_workspaces.c.node_id == NODE_ID,
            )
        ).scalar_one()
    assert count == 0


def test_notify_state_api(personal_node):
    owner, other = _user("owner"), _user("other")
    _seed(owner, other)

    r = client.get("/personal-board/notify-state", headers={"X-User-Id": str(owner)})

    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert body["latest_title"] == "티켓 B"


def test_notifications_owner_isolation_and_calendar_exclusion(personal_node):
    owner, other = _user("owner"), _user("other")
    ids = _seed(owner, other)

    r = client.get("/personal-board/notifications", headers={"X-User-Id": str(owner)})

    assert r.status_code == 200
    body = r.json()
    returned = [item["task_id"] for item in body["items"]]
    # Newest first, calendar excluded, other's ticket never visible.
    assert returned == [str(ids["b"]), str(ids["a"])]
    assert str(ids["cal"]) not in returned
    assert str(ids["c"]) not in returned
    assert body["total"] == 2

    # The other user sees only their own ticket.
    r2 = client.get("/personal-board/notifications", headers={"X-User-Id": str(other)})
    assert r2.status_code == 200
    body2 = r2.json()
    assert [item["task_id"] for item in body2["items"]] == [str(ids["c"])]
    assert body2["total"] == 1


def test_notifications_item_fields(personal_node):
    owner = _user("owner")
    ws = ensure_workspace(owner, NODE_ID)["workspace_id"]
    _task(
        workspace_id=ws,
        user_id=owner,
        title="필드 확인",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        source_kind="github",
        status="open",
    )

    r = client.get("/personal-board/notifications", headers={"X-User-Id": str(owner)})

    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["title"] == "필드 확인"
    assert item["source_kind"] == "github"
    assert item["status"] == "open"
    assert item["source_label"] is None
    assert item["backlog_bucket_id"] is None
    assert item["scheduled_date"] == "2026-02-01"
    assert item["created_at"] is not None


def test_notifications_limit_and_ordering(personal_node):
    owner = _user("owner")
    ws = ensure_workspace(owner, NODE_ID)["workspace_id"]
    for day in (1, 2, 3):
        _task(
            workspace_id=ws,
            user_id=owner,
            title=f"티켓 {day}",
            created_at=datetime(2026, 3, day, tzinfo=UTC),
        )

    r = client.get("/personal-board/notifications?limit=2", headers={"X-User-Id": str(owner)})

    assert r.status_code == 200
    body = r.json()
    assert [item["title"] for item in body["items"]] == ["티켓 3", "티켓 2"]
    # total is the full matching count, not len(items).
    assert body["total"] == 3

    # Route validates the 1..50 bound (422, not a silent widen).
    assert (
        client.get(
            "/personal-board/notifications?limit=51", headers={"X-User-Id": str(owner)}
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/personal-board/notifications?limit=0", headers={"X-User-Id": str(owner)}
        ).status_code
        == 422
    )

    # Service-level clamp stays within 1..50 for non-route callers.
    assert len(list_board_notifications(owner, NODE_ID, limit=0).items) == 1
    assert len(list_board_notifications(owner, NODE_ID, limit=999).items) == 3


def test_notifications_zero_state_without_workspace(personal_node):
    stranger = _user("stranger")

    r = client.get("/personal-board/notifications", headers={"X-User-Id": str(stranger)})

    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0
