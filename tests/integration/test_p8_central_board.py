"""P8.5a acceptance: central (company node) personal board enablement.

docs/p8-central-consolidation.md §6 (P8.5 backend half).

The personal board route hard-404s on the company node unless
`ORTHUS_OWNER_SCOPE_ENABLED` is true. With the flag on, the central node serves
each session user their own `(user_id, node_id)` workspace, fail-closed-isolated
from other users by the workspace+user_id predicates the service already applies
on every read/write (§3 owner boundary). The board's daily-doc wiki sync side
effect runs unchanged on the company node: `sync_day_to_wiki` calls
`upsert_source_document(scope="personal")`, which stamps `documents.scope`
='personal' + `documents.user_id`=<owner> and authors owner-scoped wiki/corpus,
node-kind-agnostic. So company-node board sync produces personal-owned artifacts,
never company-scope artifacts from personal data — no side-effect gating needed.

Flag-off central behavior and personal-node behavior must stay byte-identical;
the personal-node board path is covered by tests/integration/test_personal_board.py.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from orthus.api.main import app
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import documents, users

client = TestClient(app)


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(users.insert().values(user_id=uid, display_name="U"))
        s.commit()
    return uid


# --- flag-off central node: every personal-board route 404s (regression) --------

# Valid request bodies so the 404 comes from `_node_id()`, not body validation
# (POST routes validate the body before the handler runs).
_FLAG_OFF_ROUTES = [
    ("get", "/personal-board/bootstrap?date=2026-06-04", None),
    ("get", "/personal-board/weekly-plan", None),
    ("get", "/personal-board/days?start=2026-06-04", None),
    ("post", "/personal-board/tasks", {"title": "t", "scheduled_date": "2026-06-04"}),
    ("post", "/personal-board/projects", {"name": "p"}),
    (
        "post",
        "/personal-board/notes",
        {"note_date": "2026-06-04", "kind": "note", "title": "t", "body": "b"},
    ),
]


@pytest.mark.parametrize("method,path,body", _FLAG_OFF_ROUTES)
def test_company_node_flag_off_board_routes_404(clean, user_id, method, path, body):
    settings = get_settings()
    assert settings.node_kind == "company"
    assert settings.owner_scope_enabled is False
    headers = {"X-User-Id": str(user_id)}
    if method == "get":
        r = client.get(path, headers=headers)
    else:
        r = client.post(path, headers=headers, json=body)
    assert r.status_code == 404


# --- flag-on central node: owner workspace + owner isolation --------------------


def test_company_node_flag_on_owner_bootstrap_and_task_crud(clean, user_id):
    settings = get_settings()
    settings.owner_scope_enabled = True
    headers = {"X-User-Id": str(user_id)}

    boot = client.get("/personal-board/bootstrap?date=2026-06-04", headers=headers)
    assert boot.status_code == 200
    assert boot.json()["node_id"] == "company"

    created = client.post(
        "/personal-board/tasks",
        json={"title": "중앙 노드 작업", "scheduled_date": "2026-06-04"},
        headers=headers,
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    patched = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"status": "done"},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["completed"] is True


def test_company_node_flag_on_other_user_cannot_see_or_touch_owner_task(clean, user_id):
    settings = get_settings()
    settings.owner_scope_enabled = True
    user_b = _make_user()

    created = client.post(
        "/personal-board/tasks",
        json={"title": "A의 비밀 작업", "scheduled_date": "2026-06-04"},
        headers={"X-User-Id": str(user_id)},
    )
    assert created.status_code == 201
    a_task_id = created.json()["task_id"]

    # B bootstraps their own (empty) workspace under the same company node_id.
    b_boot = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(user_b)},
    )
    assert b_boot.status_code == 200
    b_tasks = [t for day in b_boot.json()["days"] for t in day["tasks"]]
    assert b_tasks == [], "B's workspace is empty; A's task is invisible"

    # B cannot read/update/delete A's task by id.
    b_headers = {"X-User-Id": str(user_b)}
    assert (
        client.patch(
            f"/personal-board/tasks/{a_task_id}", json={"status": "done"}, headers=b_headers
        ).status_code
        == 404
    )
    assert client.delete(f"/personal-board/tasks/{a_task_id}", headers=b_headers).status_code == 404

    # A still owns an open task (B's denied attempt did not mutate it).
    a_boot = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(user_id)},
    )
    a_tasks = [t for day in a_boot.json()["days"] for t in day["tasks"]]
    assert [t["task_id"] for t in a_tasks] == [a_task_id]
    assert a_tasks[0]["completed"] is False


# --- wiki daily-doc sync side effect stays owner-scoped on the company node ------


def test_company_node_flag_on_board_sync_writes_personal_owner_scoped_document(clean, user_id):
    """The board's daily-doc sync runs on the company node and produces a
    `scope='personal'` document owned by the session user (never company scope)."""
    settings = get_settings()
    settings.owner_scope_enabled = True

    created = client.post(
        "/personal-board/notes",
        json={
            "note_date": "2026-06-04",
            "kind": "note",
            "title": "세금 자료",
            "body": "금요일까지 받기로 했다.",
        },
        headers={"X-User-Id": str(user_id)},
    )
    assert created.status_code == 201

    with session() as s:
        rows = s.execute(
            select(documents.c.scope, documents.c.user_id).where(
                documents.c.source == "personal_board"
            )
        ).all()
    assert rows, "board sync wrote a personal_board source document on the company node"
    assert all(r.scope == "personal" for r in rows), "company-node board docs stay personal scope"
    assert all(r.user_id == user_id for r in rows), "board docs carry the owner's user_id"


# --- /auth/config exposes the flag ----------------------------------------------


def test_auth_config_reflects_owner_scope_flag_off(clean):
    assert get_settings().owner_scope_enabled is False
    r = client.get("/auth/config")
    assert r.status_code == 200
    assert r.json()["owner_scope_enabled"] is False


def test_auth_config_reflects_owner_scope_flag_on(clean):
    get_settings().owner_scope_enabled = True
    r = client.get("/auth/config")
    assert r.status_code == 200
    assert r.json()["owner_scope_enabled"] is True
