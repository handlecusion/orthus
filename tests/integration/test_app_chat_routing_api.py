"""P10.7 Part B: app-chat orchestrate routes a personal turn to the caller's own
enrolled resident daemon (spec §5).

DB-backed: with the routing flag + owner-scope + agent_task enabled, an operator
with an enrolled daemon who chats in personal scope gets a self-`agent_task` work
item dispatched to their daemon (reusing the deterministic delegation gate +
command queue). With the flag off the orchestrator is byte-identical — the turn
never routes and no agent_task command is enqueued.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.api.main import app
from orthus.collector.commands import list_pending_commands
from orthus.db import session
from orthus.schemas.canonical import RoutedAnswer
from orthus.settings import get_settings
from orthus.tables import collector_tokens, users

client = TestClient(app)
COMPANY_NODE = "company"


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        s.commit()
    return uid


def _enroll_daemon(user_id: uuid.UUID) -> None:
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=COMPANY_NODE,
                name="daemon",
                token_hash=uuid.uuid4().hex,
                scopes=["commands"],
            )
        )
        s.commit()


def _enable_routing() -> None:
    s = get_settings()
    s.agent_task_enabled = True
    s.owner_scope_enabled = True  # so resolve_answer_scope("personal") is honored
    s.app_chat_gateway_routing_enabled = True


def _headers(user_id: uuid.UUID) -> dict:
    return {"X-User-Id": str(user_id)}


def _new_chat(actor: uuid.UUID) -> str:
    created = client.post("/agent-work/chats", json={}, headers=_headers(actor))
    assert created.status_code == 200, created.text
    return created.json()["session_id"]


def test_personal_scope_turn_routes_to_daemon(clean):
    _enable_routing()
    actor = _make_user()
    _enroll_daemon(actor)
    session_id = _new_chat(actor)

    resp = client.post(
        f"/agent-work/chats/{session_id}/orchestrate",
        headers=_headers(actor),
        json={
            "text": "오늘 올라온 공고 정리해줘",
            "scope": "personal",
            "stream_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "agent_work"
    assert "app_chat_routed_to_daemon" in body["warnings"]
    aw = body["agent_work"]
    assert aw["action_family"] == "agent_task"
    # dispatched to a real daemon → the accurate "running on your agent" copy (M3)
    assert aw["message"] == "개인 에이전트(상주 데몬)에서 실행 중입니다."

    # The work item is a self-delegation dispatched to the caller's daemon.
    item = client.get(f"/agent-work/{aw['work_id']}", headers=_headers(actor)).json()
    assert item["payload"]["assignee_user_id"] == str(actor)
    assert item["payload"]["runner"] == "codex"
    assert item["payload"]["mode"] == "knowledge"

    # A real agent_task command was enqueued to the caller's own daemon queue.
    pending = list_pending_commands(actor)
    assert any(c.kind == "agent_task" for c in pending.items)


def test_flag_off_does_not_route(clean, monkeypatch):
    """Fail-closed: with routing off the turn flows to the inline orchestrator and
    no agent_task command is enqueued (byte-identical to pre-P10.7)."""
    _enable_routing()
    get_settings().app_chat_gateway_routing_enabled = False  # off
    actor = _make_user()
    _enroll_daemon(actor)
    session_id = _new_chat(actor)

    # Stub the inline router so we don't depend on wiki/LLM machinery; we only
    # assert routing did NOT hijack this turn.
    def _stub_answer(*a, **k):
        return RoutedAnswer(question=k.get("question", ""), mode="wiki", warnings=[])

    monkeypatch.setattr("orthus.api.routes.agent_work.answer", _stub_answer)

    resp = client.post(
        f"/agent-work/chats/{session_id}/orchestrate",
        headers=_headers(actor),
        json={"text": "회사 소개 알려줘", "scope": "personal", "stream_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "app_chat_routed_to_daemon" not in body.get("warnings", [])
    assert body["mode"] != "agent_work"
    assert list_pending_commands(actor).items == []


def test_company_scope_turn_does_not_route(clean, monkeypatch):
    """Even with routing on, a company-scope turn stays on the inline path."""
    _enable_routing()
    actor = _make_user()
    _enroll_daemon(actor)
    session_id = _new_chat(actor)

    def _stub_answer(*a, **k):
        return RoutedAnswer(question=k.get("question", ""), mode="wiki", warnings=[])

    monkeypatch.setattr("orthus.api.routes.agent_work.answer", _stub_answer)

    resp = client.post(
        f"/agent-work/chats/{session_id}/orchestrate",
        headers=_headers(actor),
        json={"text": "회사 소개 알려줘", "scope": "company", "stream_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "app_chat_routed_to_daemon" not in body.get("warnings", [])
    assert list_pending_commands(actor).items == []
