"""Agent-chat conversation sessions (Slice A, storage only).

Owner-scoped, restart-safe chat threads. Tests cover list/create/get/append
happy paths, cross-owner 404 isolation, bad-role 422, and first-user-message
title auto-set. Storage only — no policy gate, no LLM.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.api.main import app
from orthus.db import session
from orthus.tables import users

client = TestClient(app)


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Chat User"))
        s.commit()
    return uid


def _h(uid: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(uid)}


def test_create_session_appears_in_owner_list_only(clean, user_id):
    other = _make_user()

    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    assert created.status_code == 200
    body = created.json()
    assert body["session_id"]
    assert body["title"] == ""
    assert body["message_count"] == 0
    assert body["last_message_preview"] is None
    assert body["last_message_at"] is None

    listed = client.get("/agent-work/chats", headers=_h(user_id))
    assert listed.status_code == 200
    rows = listed.json()
    assert [r["session_id"] for r in rows] == [body["session_id"]]

    # The other user's list must not include it (owner isolation).
    other_listed = client.get("/agent-work/chats", headers=_h(other))
    assert other_listed.status_code == 200
    assert other_listed.json() == []


def test_append_messages_reflect_count_preview_and_order(clean, user_id):
    created = client.post("/agent-work/chats", json={"title": "Seed"}, headers=_h(user_id))
    session_id = created.json()["session_id"]

    m1 = client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "user", "text": "first question"},
        headers=_h(user_id),
    )
    assert m1.status_code == 200
    assert m1.json()["role"] == "user"
    assert m1.json()["text"] == "first question"
    assert m1.json()["work_id"] is None

    m2 = client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "agent", "text": "an answer that is reasonably long"},
        headers=_h(user_id),
    )
    assert m2.status_code == 200

    listed = client.get("/agent-work/chats", headers=_h(user_id)).json()
    assert len(listed) == 1
    summary = listed[0]
    assert summary["message_count"] == 2
    assert summary["last_message_preview"] == "an answer that is reasonably long"
    assert summary["last_message_at"] is not None

    detail = client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id))
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["session"]["message_count"] == 2
    texts = [m["text"] for m in detail_body["messages"]]
    assert texts == ["first question", "an answer that is reasonably long"]


def test_message_work_id_round_trips(clean, user_id):
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]
    work_id = str(uuid.uuid4())

    posted = client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "agent", "text": "linked", "work_id": work_id},
        headers=_h(user_id),
    )
    assert posted.status_code == 200
    assert posted.json()["work_id"] == work_id

    detail = client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id)).json()
    assert detail["messages"][0]["work_id"] == work_id


def test_preview_is_capped_at_120_chars(clean, user_id):
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]
    long_text = "x" * 300
    client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "user", "text": long_text},
        headers=_h(user_id),
    )
    summary = client.get("/agent-work/chats", headers=_h(user_id)).json()[0]
    assert summary["last_message_preview"] == "x" * 120


def test_get_another_owners_session_is_404(clean, user_id):
    other = _make_user()
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]

    resp = client.get(f"/agent-work/chats/{session_id}", headers=_h(other))
    assert resp.status_code == 404


def test_append_to_another_owners_session_is_404(clean, user_id):
    other = _make_user()
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]

    resp = client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "user", "text": "intrusion attempt"},
        headers=_h(other),
    )
    assert resp.status_code == 404

    # The real owner's session must remain untouched.
    detail = client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id)).json()
    assert detail["session"]["message_count"] == 0


def test_get_unknown_session_is_404(clean, user_id):
    resp = client.get(f"/agent-work/chats/{uuid.uuid4()}", headers=_h(user_id))
    assert resp.status_code == 404


def test_append_message_bad_role_is_422(clean, user_id):
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]

    resp = client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "system", "text": "nope"},
        headers=_h(user_id),
    )
    assert resp.status_code == 422


def test_title_auto_set_from_first_user_message_when_empty(clean, user_id):
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]
    assert created.json()["title"] == ""

    # Agent message first should NOT set the title.
    client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "agent", "text": "agent speaks first"},
        headers=_h(user_id),
    )
    after_agent = client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id)).json()
    assert after_agent["session"]["title"] == ""

    # First user message sets the title (first 60 chars).
    long_first = "회사 위키에서 회고 위치를 알려줘 " * 5
    client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "user", "text": long_first},
        headers=_h(user_id),
    )
    after_user = client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id)).json()
    assert after_user["session"]["title"] == long_first[:60]

    # A later user message must NOT overwrite the title.
    client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "user", "text": "second user message"},
        headers=_h(user_id),
    )
    final = client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id)).json()
    assert final["session"]["title"] == long_first[:60]


def test_title_provided_at_create_is_kept(clean, user_id):
    created = client.post("/agent-work/chats", json={"title": "Named thread"}, headers=_h(user_id))
    session_id = created.json()["session_id"]
    assert created.json()["title"] == "Named thread"

    client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "user", "text": "first user message"},
        headers=_h(user_id),
    )
    detail = client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id)).json()
    assert detail["session"]["title"] == "Named thread"


def test_rename_session_updates_title_and_keeps_messages(clean, user_id):
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]
    client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "user", "text": "keep me"},
        headers=_h(user_id),
    )

    renamed = client.patch(
        f"/agent-work/chats/{session_id}",
        json={"title": "  새 제목  "},
        headers=_h(user_id),
    )
    assert renamed.status_code == 200
    body = renamed.json()
    assert body["title"] == "새 제목"  # stripped
    assert body["message_count"] == 1  # aggregates preserved

    detail = client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id)).json()
    assert detail["session"]["title"] == "새 제목"
    assert [m["text"] for m in detail["messages"]] == ["keep me"]


def test_rename_title_is_capped_at_60_chars(clean, user_id):
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]
    renamed = client.patch(
        f"/agent-work/chats/{session_id}",
        json={"title": "z" * 100},
        headers=_h(user_id),
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "z" * 60


def test_rename_another_owners_session_is_404(clean, user_id):
    other = _make_user()
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]

    resp = client.patch(
        f"/agent-work/chats/{session_id}",
        json={"title": "hijacked"},
        headers=_h(other),
    )
    assert resp.status_code == 404

    # The real owner's title must remain untouched (default "").
    detail = client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id)).json()
    assert detail["session"]["title"] == ""


def test_delete_session_removes_it_and_its_messages(clean, user_id):
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]
    client.post(
        f"/agent-work/chats/{session_id}/messages",
        json={"role": "user", "text": "gone soon"},
        headers=_h(user_id),
    )

    deleted = client.delete(f"/agent-work/chats/{session_id}", headers=_h(user_id))
    assert deleted.status_code == 204

    # The session no longer lists, and fetching it is a 404.
    assert client.get("/agent-work/chats", headers=_h(user_id)).json() == []
    assert client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id)).status_code == 404


def test_delete_another_owners_session_is_404(clean, user_id):
    other = _make_user()
    created = client.post("/agent-work/chats", json={}, headers=_h(user_id))
    session_id = created.json()["session_id"]

    resp = client.delete(f"/agent-work/chats/{session_id}", headers=_h(other))
    assert resp.status_code == 404

    # The real owner's session must remain intact.
    assert client.get(f"/agent-work/chats/{session_id}", headers=_h(user_id)).status_code == 200


def test_delete_unknown_session_is_404(clean, user_id):
    resp = client.delete(f"/agent-work/chats/{uuid.uuid4()}", headers=_h(user_id))
    assert resp.status_code == 404


def test_list_orders_newest_updated_first(clean, user_id):
    first = client.post("/agent-work/chats", json={}, headers=_h(user_id)).json()["session_id"]
    second = client.post("/agent-work/chats", json={}, headers=_h(user_id)).json()["session_id"]

    # Touch the first session so its updated_at moves ahead of the second.
    client.post(
        f"/agent-work/chats/{first}/messages",
        json={"role": "user", "text": "bump first"},
        headers=_h(user_id),
    )

    rows = client.get("/agent-work/chats", headers=_h(user_id)).json()
    assert [r["session_id"] for r in rows][:2] == [first, second]


def test_chat_stream_available_without_engine_flags(clean, user_id):
    """Tier i: the chat orchestrate SSE is always available (no decompose/agentic
    flag gate) — the legacy knowledge ladder now publishes phase frames on every
    orchestrate turn, so a flags-off deployment must still be able to open the
    stream. Pre-seeded ring frames (incl. the terminal turn_complete) replay and
    close the stream immediately."""
    from collections import deque

    from orthus.agentwork import stream as agent_stream

    stream_id = "qa-phase-stream"
    key = f"{user_id}:{stream_id}"
    agent_stream._rings[key] = deque(
        [
            {"type": "phase", "stage": "searching"},
            {"type": "phase", "stage": "grounded", "evidence_count": 2},
            {"type": "phase", "stage": "answering"},
            {"type": "turn_complete"},
        ]
    )
    try:
        resp = client.get(f"/agent-work/chats/stream/{stream_id}", headers=_h(user_id))
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert '"stage": "searching"' in resp.text
        assert '"evidence_count": 2' in resp.text
        assert '"stage": "answering"' in resp.text
        assert '"turn_complete"' in resp.text
    finally:
        agent_stream._rings.pop(key, None)
