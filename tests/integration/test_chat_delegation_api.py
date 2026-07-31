"""S1/S4: structured agent_task delegate endpoint + owner-scoped agent-folders.

DB-backed tests for the friendly Agent-chat delegation surface:

- POST /agent-work/delegate builds an agent_task work item through the same
  deterministic policy gate as the /ask natural-language path (no agent process
  is ever spawned; the gate decides the outcome).
- GET /agent-work/agent-folders returns ONLY the caller's own daemon-reported
  run folders (owner-scope fail-closed) and is gated by agent_task_enabled.
- POST /collector/status stores the daemon-reported workspaces; the folder
  endpoint reflects them.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert, update

from orthus.api.main import app
from orthus.agentwork import get_agent_work_item
from orthus.collector.auth import hash_collector_token
from orthus.agentwork.service import resolve_agent_task_work_item_result
from orthus.collector.commands import list_pending_commands
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import auth_identities, collector_tokens, users

client = TestClient(app)

COMPANY_NODE = "company"


def _make_user(email: str | None = None) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        if email is not None:
            s.execute(
                insert(auth_identities).values(
                    identity_id=uuid.uuid4(),
                    user_id=uid,
                    provider="google",
                    provider_subject=uuid.uuid4().hex,
                    email=email,
                    email_verified=True,
                )
            )
        s.commit()
    return uid


def _enroll_daemon(user_id: uuid.UUID) -> uuid.UUID:
    token_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=token_id,
                user_id=user_id,
                node_id=COMPANY_NODE,
                name="daemon",
                token_hash=uuid.uuid4().hex,
                scopes=["commands"],
            )
        )
        s.commit()
    return token_id


def _enable_agent_task() -> None:
    get_settings().agent_task_enabled = True


def _headers(user_id: uuid.UUID) -> dict:
    return {"X-User-Id": str(user_id)}


# --- S1: POST /agent-work/delegate --------------------------------------------


def test_delegate_returns_agent_work_item_with_defaults(clean):
    _enable_agent_task()
    actor = _make_user()
    _enroll_daemon(actor)  # self-assignee daemon so the gate can auto-execute

    resp = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={"instruction": "summarize the latest releases"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action_family"] == "agent_task"
    # assignee defaults to the caller
    assert body["payload"]["assignee_user_id"] == str(actor)
    # runner/mode default to codex/knowledge
    assert body["payload"]["runner"] == "codex"
    assert body["payload"]["mode"] == "knowledge"


def test_delegate_honors_explicit_runner_mode_assignee_cwd(clean):
    _enable_agent_task()
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)

    resp = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={
            "instruction": "fix the failing test",
            "assignee": "dev@example.com",
            "runner": "claude",
            "mode": "code",
            "cwd": "/repo",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["payload"]["runner"] == "claude"
    assert body["payload"]["mode"] == "code"
    assert body["payload"]["assignee_user_id"] == str(assignee)
    assert body["payload"]["cwd"] == "/repo"


def test_delegate_session_thread_resumes_after_runner_session_ready(clean):
    _enable_agent_task()
    actor = _make_user()
    _enroll_daemon(actor)
    created = client.post("/agent-work/chats", json={}, headers=_headers(actor))
    assert created.status_code == 200
    session_id = created.json()["session_id"]

    first = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={
            "instruction": "첫 작업",
            "runner": "claude",
            "mode": "knowledge",
            "session_id": session_id,
        },
    )

    assert first.status_code == 200, first.text
    first_body = first.json()
    payload = first_body["payload"]
    assert payload["chat_session_id"] == session_id
    assert payload["thread_id"]
    assert payload["runner_session_id"] == payload["thread_id"]
    assert payload["runner_session_resume"] is False
    command = list_pending_commands(actor).items[-1]
    assert command.payload["runner_session_id"] == payload["thread_id"]
    assert command.payload["runner_session_resume"] is False

    resolved = resolve_agent_task_work_item_result(
        command.payload,
        command_status="done",
        result={"runner_session_id": payload["thread_id"], "stdout": "ok"},
    )
    assert resolved is not None

    second = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={
            "instruction": "이어서 작업",
            "runner": "claude",
            "mode": "knowledge",
            "session_id": session_id,
        },
    )

    assert second.status_code == 200, second.text
    second_payload = second.json()["payload"]
    assert second_payload["thread_id"] == payload["thread_id"]
    assert second_payload["runner_session_id"] == payload["thread_id"]
    assert second_payload["runner_session_resume"] is True
    second_command = list_pending_commands(actor).items[-1]
    assert second_command.payload["runner_session_resume"] is True


def test_continue_agent_task_answers_latest_input_request(clean):
    _enable_agent_task()
    actor = _make_user()
    _enroll_daemon(actor)
    created = client.post("/agent-work/chats", json={}, headers=_headers(actor))
    assert created.status_code == 200
    session_id = created.json()["session_id"]

    first = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={
            "instruction": "작업해줘",
            "runner": "claude",
            "mode": "knowledge",
            "session_id": session_id,
        },
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    payload = first_body["payload"]
    command = list_pending_commands(actor).items[-1]
    resolved = resolve_agent_task_work_item_result(
        command.payload,
        command_status="done",
        result={
            "runner_session_id": payload["thread_id"],
            "stdout": "진행 방식 골라 주세요.\n1. 빠르게 처리\n2. 테스트 먼저",
        },
    )
    assert resolved is not None and resolved.state == "request_more_data"

    result_message = client.post(
        f"/agent-work/chats/{session_id}/work-result",
        headers=_headers(actor),
        json={"work_id": first_body["work_id"]},
    )
    assert result_message.status_code == 200, result_message.text
    assert result_message.json()["text"].startswith("[입력 필요]")

    user_message = client.post(
        f"/agent-work/chats/{session_id}/messages",
        headers=_headers(actor),
        json={"role": "user", "text": "2번"},
    )
    assert user_message.status_code == 200, user_message.text

    continued = client.post(
        f"/agent-work/chats/{session_id}/continue-agent-task",
        headers=_headers(actor),
        json={"text": "2번"},
    )

    assert continued.status_code == 200, continued.text
    second_payload = continued.json()["payload"]
    assert second_payload["thread_id"] == payload["thread_id"]
    assert second_payload["runner_session_resume"] is True
    assert "사용자 답변" in second_payload["instruction"]
    assert "2번" in second_payload["instruction"]
    second_command = list_pending_commands(actor).items[-1]
    assert second_command.payload["thread_id"] == payload["thread_id"]
    assert second_command.payload["runner_session_resume"] is True
    original = get_agent_work_item(actor, uuid.UUID(first_body["work_id"]))
    assert original is not None
    assert original.state == "resolved"
    assert original.payload["pending_user_input"]["status"] == "answered"
    assert original.payload["pending_user_input"]["answer_work_id"] == continued.json()["work_id"]


def test_delegate_rejects_cross_owner_session(clean):
    _enable_agent_task()
    owner = _make_user()
    actor = _make_user()
    _enroll_daemon(actor)
    created = client.post("/agent-work/chats", json={}, headers=_headers(owner))
    assert created.status_code == 200

    resp = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={"instruction": "침범", "session_id": created.json()["session_id"]},
    )

    assert resp.status_code == 404


def test_delegate_rejects_bad_runner(clean):
    _enable_agent_task()
    actor = _make_user()

    resp = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={"instruction": "do thing", "runner": "gemini"},
    )

    assert resp.status_code == 422
    assert "runner" in resp.json()["detail"]


def test_delegate_rejects_bad_mode(clean):
    _enable_agent_task()
    actor = _make_user()

    resp = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={"instruction": "do thing", "mode": "write"},
    )

    assert resp.status_code == 422
    assert "mode" in resp.json()["detail"]


def test_delegate_empty_instruction_is_422(clean):
    _enable_agent_task()
    actor = _make_user()

    resp = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={"instruction": "   "},
    )

    assert resp.status_code == 422


# --- S4: GET /agent-work/agent-folders ----------------------------------------


def _set_workspaces(token_id: uuid.UUID, workspaces: dict, *, last_status_at) -> None:
    with session() as s:
        s.execute(
            update(collector_tokens)
            .where(collector_tokens.c.token_id == token_id)
            .values(agent_workspaces=workspaces, last_status_at=last_status_at)
        )
        s.commit()


def test_agent_folders_unsupported_when_disabled(clean):
    get_settings().agent_task_enabled = False
    actor = _make_user()

    resp = client.get("/agent-work/agent-folders", headers=_headers(actor))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "supported": False,
        "default": None,
        "recent": [],
        "reported_at": None,
    }


def test_agent_folders_empty_when_no_report(clean):
    _enable_agent_task()
    actor = _make_user()
    _enroll_daemon(actor)  # token with NULL agent_workspaces

    resp = client.get("/agent-work/agent-folders", headers=_headers(actor))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["supported"] is True
    assert body["default"] is None
    assert body["recent"] == []
    assert body["reported_at"] is None


def test_agent_folders_returns_own_folders(clean):
    from datetime import UTC, datetime

    _enable_agent_task()
    actor = _make_user()
    token_id = _enroll_daemon(actor)
    _set_workspaces(
        token_id,
        {"default": "/work/default", "recent": ["/repo/a", "/repo/b"]},
        last_status_at=datetime(2026, 6, 20, tzinfo=UTC),
    )

    resp = client.get("/agent-work/agent-folders", headers=_headers(actor))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["supported"] is True
    assert body["default"] == "/work/default"
    assert body["recent"] == ["/repo/a", "/repo/b"]
    assert body["reported_at"] is not None


def test_agent_folders_are_owner_scoped_no_cross_user_leak(clean):
    from datetime import UTC, datetime

    _enable_agent_task()
    actor = _make_user()
    other = _make_user()
    actor_token = _enroll_daemon(actor)
    other_token = _enroll_daemon(other)
    _set_workspaces(
        actor_token,
        {"default": "/me/work", "recent": ["/me/repo"]},
        last_status_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    _set_workspaces(
        other_token,
        {"default": "/secret/work", "recent": ["/secret/repo"]},
        last_status_at=datetime(2026, 6, 21, tzinfo=UTC),
    )

    resp = client.get("/agent-work/agent-folders", headers=_headers(actor))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Only the caller's own folders are visible; the other user's are never leaked.
    assert body["default"] == "/me/work"
    assert body["recent"] == ["/me/repo"]
    assert "/secret/repo" not in body["recent"]
    assert "/secret/work" != body["default"]


def test_agent_folders_dedupe_and_newest_default_across_tokens(clean):
    from datetime import UTC, datetime

    _enable_agent_task()
    actor = _make_user()
    older = _enroll_daemon(actor)
    newer = _enroll_daemon(actor)
    _set_workspaces(
        older,
        {"default": "/old/default", "recent": ["/repo/shared", "/repo/old"]},
        last_status_at=datetime(2026, 6, 18, tzinfo=UTC),
    )
    _set_workspaces(
        newer,
        {"default": "/new/default", "recent": ["/repo/new", "/repo/shared"]},
        last_status_at=datetime(2026, 6, 21, tzinfo=UTC),
    )

    resp = client.get("/agent-work/agent-folders", headers=_headers(actor))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # default comes from the most-recently-reported token.
    assert body["default"] == "/new/default"
    # recent is the deduped union, most-recent token first; "/repo/shared" once.
    assert body["recent"] == ["/repo/new", "/repo/shared", "/repo/old"]


def test_agent_folders_ignores_revoked_token(clean):
    from datetime import UTC, datetime

    _enable_agent_task()
    actor = _make_user()
    revoked = _enroll_daemon(actor)
    _set_workspaces(
        revoked,
        {"default": "/revoked/work", "recent": ["/revoked/repo"]},
        last_status_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    with session() as s:
        s.execute(
            update(collector_tokens)
            .where(collector_tokens.c.token_id == revoked)
            .values(revoked_at=datetime(2026, 6, 21, tzinfo=UTC))
        )
        s.commit()

    resp = client.get("/agent-work/agent-folders", headers=_headers(actor))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recent"] == []
    assert body["default"] is None


# --- S3->E: status report stores workspaces; folder endpoint reflects it -------


def test_status_report_stores_workspaces_reflected_by_folder_endpoint(clean, monkeypatch):
    _enable_agent_task()
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    actor = _make_user()

    plaintext = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=actor,
                node_id=COMPANY_NODE,
                name="daemon",
                token_hash=hash_collector_token(plaintext),
                scopes=["commands"],
            )
        )
        s.commit()

    status = client.post(
        "/collector/status",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={"workspaces": {"default": "/work/default", "recent": ["/repo/x", "/repo/y"]}},
    )
    assert status.status_code == 200, status.text

    folders = client.get("/agent-work/agent-folders", headers=_headers(actor))
    assert folders.status_code == 200, folders.text
    body = folders.json()
    assert body["default"] == "/work/default"
    assert body["recent"] == ["/repo/x", "/repo/y"]
    assert body["reported_at"] is not None


# --- device targeting on /agent-work/delegate ---------------------------------


def _enroll_device(user_id: uuid.UUID, device_id: str) -> None:
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=COMPANY_NODE,
                name=f"daemon-{device_id}",
                token_hash=uuid.uuid4().hex,
                scopes=["commands"],
                device_id=device_id,
            )
        )
        s.commit()


def test_delegate_pins_requested_device(clean):
    _enable_agent_task()
    actor = _make_user()
    _enroll_device(actor, "device-a")
    _enroll_device(actor, "device-b")

    resp = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={"instruction": "run on a", "device_id": "device-a"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["payload"]["assignee_device_id"] == "device-a"
    command = list_pending_commands(actor, device_id="device-a").items[-1]
    assert command.payload["work_item_id"] == body["work_id"]
    # device-b daemon must not see the pinned command.
    assert list_pending_commands(actor, device_id="device-b").count == 0


def test_delegate_unowned_device_is_422(clean):
    _enable_agent_task()
    actor = _make_user()
    _enroll_device(actor, "device-a")

    resp = client.post(
        "/agent-work/delegate",
        headers=_headers(actor),
        json={"instruction": "run somewhere", "device_id": "device-zzz"},
    )

    assert resp.status_code == 422, resp.text


# --- GET /agent-work/delegate-targets -----------------------------------------


def test_delegate_targets_unsupported_when_disabled(clean):
    get_settings().agent_task_enabled = False
    actor = _make_user()
    resp = client.get("/agent-work/delegate-targets", headers=_headers(actor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["supported"] is False
    assert body["self_user_id"] == str(actor)
    assert body["targets"] == []


def test_delegate_targets_owner_includes_teammate(clean):
    from datetime import UTC, datetime

    _enable_agent_task()
    actor = _make_user(email="owner@example.com")
    _enroll_daemon(actor)
    teammate = _make_user(email="mate@example.com")
    teammate_token = _enroll_daemon(teammate)
    # Recent poll -> the teammate node is online.
    with session() as s:
        s.execute(
            update(collector_tokens)
            .where(collector_tokens.c.token_id == teammate_token)
            .values(last_polled_at=datetime.now(UTC))
        )
        s.commit()

    resp = client.get("/agent-work/delegate-targets", headers=_headers(actor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["supported"] is True
    ids = [t["user_id"] for t in body["targets"]]
    assert str(actor) in ids and str(teammate) in ids
    # self is first.
    assert body["targets"][0]["user_id"] == str(actor)
    assert body["targets"][0]["is_self"] is True
    mate = next(t for t in body["targets"] if t["user_id"] == str(teammate))
    assert mate["online"] is True
    assert mate["nodes"][0]["status"] == "online"
    assert mate["email"] == "mate@example.com"


def test_delegate_targets_node_offline_when_poll_is_old(clean):
    from datetime import UTC, datetime, timedelta

    _enable_agent_task()
    actor = _make_user(email="owner2@example.com")
    token = _enroll_daemon(actor)
    with session() as s:
        s.execute(
            update(collector_tokens)
            .where(collector_tokens.c.token_id == token)
            .values(last_polled_at=datetime.now(UTC) - timedelta(hours=5))
        )
        s.commit()

    resp = client.get("/agent-work/delegate-targets", headers=_headers(actor))
    assert resp.status_code == 200, resp.text
    target = next(t for t in resp.json()["targets"] if t["user_id"] == str(actor))
    assert target["online"] is False
    assert target["nodes"][0]["status"] == "offline"
    # device_id is null for a legacy deviceless token.
    assert target["nodes"][0]["device_id"] is None


def test_delegate_candidate_member_sees_only_self(clean):
    # require_node_operator permits demo/owner only; the member branch of the
    # candidate resolver is exercised directly (a session member is 403'd before
    # reaching this code). With role="member" only self is returned even when a
    # teammate has an enrolled daemon.
    from orthus.api.routes.agent_work import _delegate_candidate_user_ids

    actor = _make_user()
    teammate = _make_user(email="mate2@example.com")
    _enroll_daemon(teammate)

    ids = _delegate_candidate_user_ids(actor, role="member", node_id=COMPANY_NODE)
    assert ids == [actor]


# --- delegate-targets node hygiene (sprawl + deviceless collapse + empty drop) -


def _enroll_device_token(user_id: uuid.UUID, device_id: str) -> uuid.UUID:
    token_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=token_id,
                user_id=user_id,
                node_id=COMPANY_NODE,
                name=f"daemon-{device_id}",
                token_hash=uuid.uuid4().hex,
                scopes=["commands"],
                device_id=device_id,
            )
        )
        s.commit()
    return token_id


def _set_last_polled(token_id: uuid.UUID, when) -> None:
    with session() as s:
        s.execute(
            update(collector_tokens)
            .where(collector_tokens.c.token_id == token_id)
            .values(last_polled_at=when)
        )
        s.commit()


def test_delegate_targets_excludes_never_polled_keeps_offline(clean):
    # A never-polled run-scope token is a CLI registration artifact and must NOT
    # appear as a node; an offline (polled long ago) token is still pickable.
    from datetime import UTC, datetime, timedelta

    _enable_agent_task()
    actor = _make_user(email="hygiene@example.com")
    seen_token = _enroll_device_token(actor, "device-seen")
    _enroll_device_token(actor, "device-never")  # never polled -> dropped
    _set_last_polled(seen_token, datetime.now(UTC) - timedelta(hours=5))

    resp = client.get("/agent-work/delegate-targets", headers=_headers(actor))
    assert resp.status_code == 200, resp.text
    target = next(t for t in resp.json()["targets"] if t["user_id"] == str(actor))
    device_ids = [n["device_id"] for n in target["nodes"]]
    assert device_ids == ["device-seen"]
    assert target["nodes"][0]["status"] == "offline"


def test_delegate_targets_collapse_deviceless_to_newest(clean):
    # Multiple legacy deviceless tokens for one user collapse to a SINGLE node,
    # keeping the most recently polled one.
    from datetime import UTC, datetime, timedelta

    _enable_agent_task()
    actor = _make_user(email="deviceless@example.com")
    older = _enroll_daemon(actor)
    newer = _enroll_daemon(actor)
    _set_last_polled(older, datetime.now(UTC) - timedelta(hours=3))
    _set_last_polled(newer, datetime.now(UTC) - timedelta(minutes=10))

    resp = client.get("/agent-work/delegate-targets", headers=_headers(actor))
    assert resp.status_code == 200, resp.text
    target = next(t for t in resp.json()["targets"] if t["user_id"] == str(actor))
    assert len(target["nodes"]) == 1
    node = target["nodes"][0]
    assert node["device_id"] is None  # deviceless -> null
    # The most-recently-polled deviceless token wins -> online (<=30min).
    assert node["status"] == "online"


def test_delegate_targets_drops_empty_teammate_keeps_self(clean):
    # A teammate whose only tokens were never polled has zero runnable nodes and
    # is excluded from targets entirely; self with zero nodes is still present.
    _enable_agent_task()
    actor = _make_user(email="selfowner@example.com")  # never-polled -> no nodes
    _enroll_daemon(actor)
    teammate = _make_user(email="ghost@example.com")  # never-polled -> no nodes
    _enroll_daemon(teammate)

    resp = client.get("/agent-work/delegate-targets", headers=_headers(actor))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = [t["user_id"] for t in body["targets"]]
    assert str(actor) in ids  # self always present
    assert str(teammate) not in ids  # empty teammate dropped
    self_target = next(t for t in body["targets"] if t["user_id"] == str(actor))
    assert self_target["is_self"] is True
    assert self_target["nodes"] == []
