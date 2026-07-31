"""HITL (Human-in-the-Loop) for the agent-work chat orchestrator — real DB.

Seals the turn-ending question → resume loop end to end against orthus_test:

- agent_chat_messages.kind/payload roundtrip + legacy default (migration 0101).
- orchestrate flag ON: an email command with no recipient parks as
  request_more_data and surfaces a user_input_request (chat row + work-item
  pending marker).
- orchestrate flag OFF: byte-identical — no user_input_request, no question row.
- POST /chats/{id}/hitl-response: server-appends the answer, marks the question
  answered, resolves the blocked item, and re-runs the SAME gate (recipient now
  present → draft_for_review).
- guards: 409 (second answer / non-latest), 404 (flag off / non-owner), 422
  (empty answer), and the plain message POST cannot forge a structured kind.

The deterministic policy gate (state.py) is never touched; HITL only projects its
output and resume re-enters it.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.agentwork import create_assistant_command_work_item
from orthus.agentwork.chat import append_message, get_session as get_chat_session
from orthus.api.deps import get_current_user
from orthus.api.main import app
from orthus.api.routes import agent_work as agent_work_routes
from orthus.auth import AuthenticatedUser
from orthus.db import session
from orthus.schemas.canonical import RoutedAnswer, SubAnswer
from orthus.settings import get_settings
from orthus.tables import agent_chat_messages, agent_work_items, users

client = TestClient(app)

_EMAIL_CMD = "메일 초안 만들어줘"  # email_send, no recipient → request_more_data


@pytest.fixture
def hitl_env():
    """Personal owner node with HITL on; restores flags/node identity after."""
    settings = get_settings()
    original = (
        settings.agent_hitl_enabled,
        settings.ask_decompose_enabled,
        settings.ask_command_split_enabled,
        settings.node_kind,
        settings.node_id,
    )
    settings.agent_hitl_enabled = True
    settings.ask_decompose_enabled = False
    settings.ask_command_split_enabled = False
    settings.node_kind = "personal"
    settings.node_id = "personal-test"
    yield settings
    (
        settings.agent_hitl_enabled,
        settings.ask_decompose_enabled,
        settings.ask_command_split_enabled,
        settings.node_kind,
        settings.node_id,
    ) = original


def _owner_override(user_id: uuid.UUID):
    def _owner():
        return AuthenticatedUser(
            user_id=user_id,
            auth_mode="session",
            display_name="Owner",
            email="owner@example.com",
            role="owner",
            node_id=get_settings().node_id,
        )

    return _owner


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="HITL User"))
        s.commit()
    return uid


def _question_rows(session_id: str) -> list:
    with session() as s:
        return s.execute(
            select(agent_chat_messages).where(
                agent_chat_messages.c.session_id == uuid.UUID(session_id),
                agent_chat_messages.c.kind == "user_input_request",
            )
        ).all()


# ── storage: kind/payload roundtrip + legacy default ──


def test_append_kind_payload_roundtrip_and_legacy_default(clean, user_id, hitl_env):
    node_id = hitl_env.node_id
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        created = client.post("/agent-work/chats", json={})
        sid = uuid.UUID(created.json()["session_id"])
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    plain = append_message(user_id, node_id, sid, "user", "안녕")
    assert plain is not None
    assert plain.kind == "text"
    assert plain.payload is None

    structured = append_message(
        user_id,
        node_id,
        sid,
        "agent",
        "누구에게 보낼까요?",
        kind="user_input_request",
        payload={"origin": "policy_gate", "status": "waiting"},
    )
    assert structured.kind == "user_input_request"
    assert structured.payload["status"] == "waiting"

    detail = get_chat_session(user_id, node_id, sid)
    kinds = {m.message_id: (m.kind, m.payload) for m in detail.messages}
    assert kinds[plain.message_id] == ("text", None)
    assert kinds[structured.message_id][0] == "user_input_request"


# ── orchestrate flag ON → question surfaces ──


def test_orchestrate_flag_on_email_missing_recipient_asks(clean, user_id, hitl_env):
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        asked = client.post(f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD})
        assert asked.status_code == 200, asked.text
        body = asked.json()
        uir = body["user_input_request"]
        assert uir is not None
        assert uir["origin"] == "policy_gate"
        assert uir["status"] == "waiting"
        assert uir["message_id"]
        # 후속 B: the email recipient gate question carries a form field, and the
        # persisted chat-row payload carries the same declaration for the FE.
        assert uir["fields"] == [
            {
                "name": "recipient",
                "label": "받는 사람",
                "placeholder": "이름 또는 이메일",
                "required": True,
            }
        ]
        work_id = uir["work_id"]
        assert work_id

        # Question persisted as a chat row (fields included in the stored payload).
        rows = _question_rows(sid)
        assert len(rows) == 1
        assert rows[0]._mapping["payload"]["fields"][0]["name"] == "recipient"

        # Work item stamped as waiting on the chat owner.
        with session() as s:
            row = s.execute(
                select(agent_work_items).where(agent_work_items.c.work_id == uuid.UUID(work_id))
            ).first()
        assert row._mapping["state"] == "request_more_data"
        assert row._mapping["payload"]["pending_user_input"]["status"] == "waiting"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── orchestrate flag OFF → byte-identical ──


def test_orchestrate_flag_off_no_question(clean, user_id, hitl_env):
    hitl_env.agent_hitl_enabled = False
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        asked = client.post(f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD})
        assert asked.status_code == 200, asked.text
        assert asked.json().get("user_input_request") is None
        assert _question_rows(sid) == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── hitl-response happy path ──


def test_hitl_response_resolves_and_reruns_gate(clean, user_id, hitl_env):
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        asked = client.post(
            f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD}
        ).json()
        uir = asked["user_input_request"]
        blocked_id = uir["work_id"]

        resp = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={"message_id": uir["message_id"], "answer": "김민수"},
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()

        # Server-appended user answer row.
        assert result["response_message"]["role"] == "user"
        assert result["response_message"]["kind"] == "user_input_response"

        # Question flipped to answered.
        detail = get_chat_session(user_id, hitl_env.node_id, uuid.UUID(sid))
        q = next(m for m in detail.messages if m.message_id == uir["message_id"])
        assert q.payload["status"] == "answered"

        # Original blocked item resolved.
        with session() as s:
            blocked = s.execute(
                select(agent_work_items).where(agent_work_items.c.work_id == uuid.UUID(blocked_id))
            ).first()
        assert blocked._mapping["state"] == "resolved"

        # Resume re-ran the SAME gate; recipient now present → draft_for_review.
        assert result["routed"]["mode"] == "agent_work"
        successor = result["routed"]["agent_work"]
        assert successor["work_id"] != blocked_id
        assert successor["state"] == "draft_for_review"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── 후속 B: fill-in-the-blank values submission ──


def test_hitl_response_values_happy_path(clean, user_id, hitl_env):
    """Mirrors test_hitl_response_resolves_and_reruns_gate but submits the form
    `values` instead of a composer `answer`; the merged single-field value must
    flow through the same resume path byte-identically."""
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        asked = client.post(
            f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD}
        ).json()
        uir = asked["user_input_request"]
        blocked_id = uir["work_id"]

        resp = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={"message_id": uir["message_id"], "values": {"recipient": "김민수"}},
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()

        # Server-appended user answer row: merged bare value + values echo.
        assert result["response_message"]["role"] == "user"
        assert result["response_message"]["kind"] == "user_input_response"
        assert result["response_message"]["text"] == "김민수"
        assert result["response_message"]["payload"]["values"] == {"recipient": "김민수"}

        # Question flipped to answered.
        detail = get_chat_session(user_id, hitl_env.node_id, uuid.UUID(sid))
        q = next(m for m in detail.messages if m.message_id == uir["message_id"])
        assert q.payload["status"] == "answered"

        # Original blocked item resolved.
        with session() as s:
            blocked = s.execute(
                select(agent_work_items).where(agent_work_items.c.work_id == uuid.UUID(blocked_id))
            ).first()
        assert blocked._mapping["state"] == "resolved"

        # Resume re-ran the SAME gate; recipient now present → draft_for_review.
        assert result["routed"]["mode"] == "agent_work"
        successor = result["routed"]["agent_work"]
        assert successor["work_id"] != blocked_id
        assert successor["state"] == "draft_for_review"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_hitl_response_values_and_answer_422(clean, user_id, hitl_env):
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        uir = client.post(f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD}).json()[
            "user_input_request"
        ]
        resp = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={
                "message_id": uir["message_id"],
                "answer": "김민수",
                "values": {"recipient": "김민수"},
            },
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_hitl_response_values_undeclared_key_422(clean, user_id, hitl_env):
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        uir = client.post(f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD}).json()[
            "user_input_request"
        ]
        resp = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={"message_id": uir["message_id"], "values": {"foo": "bar"}},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_hitl_response_values_blank_required_422(clean, user_id, hitl_env):
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        uir = client.post(f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD}).json()[
            "user_input_request"
        ]
        resp = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={"message_id": uir["message_id"], "values": {"recipient": "  "}},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_hitl_response_values_on_fieldless_question_422(clean, user_id, hitl_env):
    """A legacy waiting text question with no declared fields cannot take a form
    submission (values ⇒ 422; the composer `answer` path stays the only way in)."""
    node_id = hitl_env.node_id
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        planted = append_message(
            user_id,
            node_id,
            uuid.UUID(sid),
            "agent",
            "추가 정보가 필요합니다",
            kind="user_input_request",
            payload={
                "origin": "agentic",
                "status": "waiting",
                "input_type": "text",
                "question": "추가 정보가 필요합니다",
                "resume": {"original_question": "정리해줘"},
            },
        )
        resp = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={"message_id": planted.message_id, "values": {"recipient": "김민수"}},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── guards ──


def test_hitl_response_second_answer_409(clean, user_id, hitl_env):
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        uir = client.post(f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD}).json()[
            "user_input_request"
        ]
        first = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={"message_id": uir["message_id"], "answer": "김민수"},
        )
        assert first.status_code == 200, first.text
        second = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={"message_id": uir["message_id"], "answer": "또"},
        )
        assert second.status_code == 409
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_hitl_response_empty_answer_422(clean, user_id, hitl_env):
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        uir = client.post(f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD}).json()[
            "user_input_request"
        ]
        resp = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={"message_id": uir["message_id"], "answer": "   "},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_hitl_response_flag_off_404(clean, user_id, hitl_env):
    # Ask with the flag on, then answer with it off → fail-closed 404.
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        uir = client.post(f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD}).json()[
            "user_input_request"
        ]
        hitl_env.agent_hitl_enabled = False
        resp = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={"message_id": uir["message_id"], "answer": "김민수"},
        )
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_hitl_response_non_owner_404(clean, user_id, hitl_env):
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        uir = client.post(f"/agent-work/chats/{sid}/orchestrate", json={"text": _EMAIL_CMD}).json()[
            "user_input_request"
        ]
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    other = _make_user()
    app.dependency_overrides[get_current_user] = _owner_override(other)
    try:
        resp = client.post(
            f"/agent-work/chats/{sid}/hitl-response",
            json={"message_id": uir["message_id"], "answer": "김민수"},
        )
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_message_post_cannot_forge_structured_kind(clean, user_id, hitl_env):
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        # The message create body has no kind/payload field; extras are ignored, so
        # the row must land as a plain text message.
        client.post(
            f"/agent-work/chats/{sid}/messages",
            json={"role": "user", "text": "x", "kind": "user_input_request"},
        )
        assert _question_rows(sid) == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# ── PR3: decomposed blocked-leaf → question mapping (route hook, deterministic) ──


def test_decomposed_blocked_email_leaf_maps_to_question(clean, user_id, hitl_env, monkeypatch):
    """A compound turn whose command leaf is parked by the gate (email_send +
    required_data) surfaces a question. The decompose split itself is LLM-driven and
    flaky, so we drive the route hook directly: a real request_more_data item + a
    synthetic decomposed RoutedAnswer pointing at it, with answer() monkeypatched."""
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]

        # Real blocked email leaf (unique text avoids idempotent de-dup with prior tests).
        item = create_assistant_command_work_item(
            user_id, "분기실적 메일 초안 만들어줘", actor_role="owner"
        )
        assert item is not None and item.state == "request_more_data"

        synthetic = RoutedAnswer(
            question="분기실적 요약하고 메일 보내줘",
            mode="decomposed",
            parts=[
                SubAnswer(sub_question_id=uuid.uuid4(), grounded=True),  # the read leaf
                SubAnswer(
                    sub_question_id=uuid.uuid4(),
                    agent_work_id=item.work_id,
                    state="request_more_data",
                ),
            ],
        )
        monkeypatch.setattr(agent_work_routes, "answer", lambda *a, **k: synthetic)

        asked = client.post(
            f"/agent-work/chats/{sid}/orchestrate",
            json={"text": "분기실적 요약하고 메일 보내줘", "scope": "company"},
        )
        assert asked.status_code == 200, asked.text
        uir = asked.json()["user_input_request"]
        assert uir is not None
        assert uir["origin"] == "policy_gate"
        assert uir["work_id"] == str(item.work_id)
        assert uir["message_id"]
        rows = _question_rows(sid)
        assert len(rows) == 1
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_decomposed_mapping_off_when_flag_off(clean, user_id, hitl_env, monkeypatch):
    hitl_env.agent_hitl_enabled = False
    app.dependency_overrides[get_current_user] = _owner_override(user_id)
    try:
        sid = client.post("/agent-work/chats", json={}).json()["session_id"]
        item = create_assistant_command_work_item(
            user_id, "월별매출 메일 초안 만들어줘", actor_role="owner"
        )
        assert item is not None and item.state == "request_more_data"
        synthetic = RoutedAnswer(
            question="q",
            mode="decomposed",
            parts=[SubAnswer(sub_question_id=uuid.uuid4(), agent_work_id=item.work_id)],
        )
        monkeypatch.setattr(agent_work_routes, "answer", lambda *a, **k: synthetic)
        asked = client.post(
            f"/agent-work/chats/{sid}/orchestrate", json={"text": "q", "scope": "company"}
        )
        assert asked.status_code == 200
        assert asked.json().get("user_input_request") is None
        assert _question_rows(sid) == []
    finally:
        app.dependency_overrides.pop(get_current_user, None)
