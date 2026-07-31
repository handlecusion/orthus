"""Chat history (orchestrator reference context) + Slice C (work-result writeback) tests.

Both surfaces are strictly owner-scoped and fail-closed. `load_chat_history` builds
non-authoritative reference turns for the agent-work chat orchestrator (excluding the
current question); `/ask` is search-only and never builds history. The work-result
endpoint is poll-on-view, server-shaped, PII-redacted, and idempotent.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.agentwork.chat import load_chat_history
from orthus.api.main import app
from orthus.api.routes import agent_work as agent_work_route
from orthus.api.routes import ask as ask_route
from orthus.db import session
from orthus.schemas.canonical import ChatTurn
from orthus.settings import get_settings
from orthus.tables import agent_chat_messages, agent_work_items, users

client = TestClient(app)


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Chat User"))
        s.commit()
    return uid


def _h(uid: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(uid)}


def _seed_messages(session_id: uuid.UUID, turns: list[tuple[str, str]]) -> None:
    import datetime

    base = datetime.datetime.now(datetime.UTC)
    with session() as s:
        for i, (role, text) in enumerate(turns):
            s.execute(
                insert(agent_chat_messages).values(
                    message_id=uuid.uuid4(),
                    session_id=session_id,
                    role=role,
                    text=text,
                    created_at=base + datetime.timedelta(seconds=i),
                )
            )
        s.commit()


def _create_session(uid: uuid.UUID) -> uuid.UUID:
    created = client.post("/agent-work/chats", json={}, headers=_h(uid))
    assert created.status_code == 200
    return uuid.UUID(created.json()["session_id"])


# --- load_chat_history (orchestrator reference context) -------------------------


def test_load_chat_history_excludes_current_question_and_caps(clean, user_id):
    session_id = _create_session(user_id)
    # 8 prior turns + the FE-appended current user message at the end.
    turns: list[tuple[str, str]] = []
    for i in range(4):
        turns.append(("user", f"질문{i}"))
        turns.append(("agent", f"답변{i}"))
    turns.append(("user", "현재 질문"))  # the FE appends this BEFORE calling orchestrate
    _seed_messages(session_id, turns)

    history = load_chat_history(user_id, get_settings().node_id, str(session_id), "현재 질문")
    # The trailing user message equal to the current question is dropped.
    assert all(not (t.role == "user" and t.text == "현재 질문") for t in history)
    # Capped to the last 6 turns.
    assert len(history) == 6
    assert isinstance(history[0], ChatTurn)
    # Order preserved (oldest of the kept window first).
    assert history[-1].text == "답변3"


def test_load_chat_history_truncates_text(clean, user_id):
    session_id = _create_session(user_id)
    long_text = "가" * 900
    _seed_messages(session_id, [("user", long_text), ("agent", "ok"), ("user", "현재")])
    history = load_chat_history(user_id, get_settings().node_id, str(session_id), "현재")
    user_turn = next(t for t in history if t.role == "user")
    assert len(user_turn.text) == 500


def test_load_chat_history_cross_owner_returns_empty(clean, user_id):
    other = _make_user()
    session_id = _create_session(user_id)
    _seed_messages(session_id, [("user", "비밀"), ("agent", "답"), ("user", "현재")])
    # Another user cannot read this session's history (fail-closed -> []).
    history = load_chat_history(other, get_settings().node_id, str(session_id), "현재")
    assert history == []


def test_load_chat_history_missing_session_returns_empty(clean, user_id):
    history = load_chat_history(user_id, get_settings().node_id, str(uuid.uuid4()), "현재")
    assert history == []


def test_ask_endpoint_is_search_only_passes_no_history(clean, user_id, monkeypatch):
    """`/ask` is search-only — it never builds session history (that moved to the
    agent-work chat orchestrator). An extra `session_id` field is simply ignored."""
    captured: dict[str, object] = {"called": False}

    def _fake_answer(uid, question, **kwargs):
        captured["called"] = True
        captured["history"] = kwargs.get("history")
        from orthus.schemas.canonical import RoutedAnswer, WikiAnswer

        return RoutedAnswer(
            question=question,
            mode="wiki",
            wiki=WikiAnswer(question=question, answer="ok", sources=[]),
        )

    monkeypatch.setattr(ask_route, "answer", _fake_answer)
    resp = client.post("/ask", json={"question": "뭐였더라", "session_id": str(uuid.uuid4())})

    assert resp.status_code == 200
    assert captured["called"] is True
    assert captured["history"] is None


def test_orchestrate_caller_builds_history(clean, user_id, monkeypatch):
    """The agent-work chat orchestrator builds non-authoritative reference history from
    the owner-scoped session (the surface where decompose/answer now run)."""
    captured: dict[str, object] = {}

    def _fake_answer(uid, question, **kwargs):
        captured["history"] = kwargs.get("history")
        from orthus.schemas.canonical import RoutedAnswer, WikiAnswer

        return RoutedAnswer(
            question=question,
            mode="wiki",
            wiki=WikiAnswer(question=question, answer="ok", sources=[]),
        )

    monkeypatch.setattr(agent_work_route, "answer", _fake_answer)
    session_id = _create_session(user_id)
    _seed_messages(
        session_id,
        [("user", "첫 질문"), ("agent", "첫 답변"), ("user", "두번째 질문")],
    )
    resp = client.post(
        f"/agent-work/chats/{session_id}/orchestrate",
        json={"text": "두번째 질문"},
        headers=_h(user_id),
    )
    assert resp.status_code == 200
    history = captured["history"]
    assert history is not None
    # Current question excluded; prior turns present.
    assert [t.text for t in history] == ["첫 질문", "첫 답변"]


# --- Slice C: work-result writeback ---------------------------------------------


def _seed_work_item(
    user_id: uuid.UUID,
    *,
    state: str,
    result: dict | None = None,
    reason_codes: list[str] | None = None,
) -> uuid.UUID:
    settings = get_settings()
    work_id = uuid.uuid4()
    payload: dict = {}
    if result is not None:
        payload["auto_execution"] = {"kind": "agent_task", "result": result}
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=settings.node_id,
                node_kind=settings.node_kind,
                owner_id=user_id if settings.node_kind == "personal" else None,
                source_kind="assistant_command",
                source_ref_id="agent-task-cmd",
                action_family="agent_task",
                title="Delegated task",
                payload=payload,
                state=state,
                policy_outcome="auto_execute",
                policy_reason="agent_task",
                reason_codes=reason_codes or [],
                evidence=[],
                created_by=user_id,
            )
        )
        s.commit()
    return work_id


def test_work_result_non_terminal_returns_null(clean, user_id):
    session_id = _create_session(user_id)
    work_id = _seed_work_item(user_id, state="auto_execute", result={"stdout": "x"})
    resp = client.post(
        f"/agent-work/chats/{session_id}/work-result",
        json={"work_id": str(work_id)},
        headers=_h(user_id),
    )
    assert resp.status_code == 200
    assert resp.json() is None
    # No message appended.
    with session() as s:
        count = s.execute(
            select(agent_chat_messages).where(agent_chat_messages.c.session_id == session_id)
        ).all()
    assert count == []


def test_work_result_terminal_resolved_appends_redacted_once(clean, user_id):
    session_id = _create_session(user_id)
    # Result with a PII email so we can confirm redaction ran.
    work_id = _seed_work_item(
        user_id,
        state="resolved",
        result={"stdout": "작업 완료. 연락처 alice@example.com 확인."},
    )
    first = client.post(
        f"/agent-work/chats/{session_id}/work-result",
        json={"work_id": str(work_id)},
        headers=_h(user_id),
    )
    assert first.status_code == 200
    body = first.json()
    assert body is not None
    assert body["role"] == "agent"
    assert body["work_id"] == str(work_id)
    assert body["text"].startswith("[완료]")
    # Redaction ran (raw email must not survive).
    assert "alice@example.com" not in body["text"]

    # Idempotent: a second poll returns the SAME message, no duplicate row.
    second = client.post(
        f"/agent-work/chats/{session_id}/work-result",
        json={"work_id": str(work_id)},
        headers=_h(user_id),
    )
    assert second.status_code == 200
    assert second.json()["message_id"] == body["message_id"]

    with session() as s:
        rows = s.execute(
            select(agent_chat_messages).where(
                agent_chat_messages.c.session_id == session_id,
                agent_chat_messages.c.work_id == work_id,
            )
        ).all()
    assert len(rows) == 1


def test_work_result_terminal_request_more_data_label(clean, user_id):
    # A genuine policy-gate "need more data" (no run, no auto_execute_failed) keeps
    # the "[정보 필요]" label — NOT the failure label.
    session_id = _create_session(user_id)
    work_id = _seed_work_item(user_id, state="request_more_data")
    resp = client.post(
        f"/agent-work/chats/{session_id}/work-result",
        json={"work_id": str(work_id)},
        headers=_h(user_id),
    )
    assert resp.status_code == 200
    assert resp.json()["text"].startswith("[정보 필요]")


def test_work_result_failed_agent_task_shows_failure_reason(clean, user_id):
    # A dispatched agent_task whose run FAILED lands in request_more_data + the
    # auto_execute_failed reason code; it must surface as "[실패]" with the
    # runner's pre-flight reason, not a bland "[정보 필요]".
    session_id = _create_session(user_id)
    work_id = _seed_work_item(
        user_id,
        state="request_more_data",
        result={"reason": "runner not installed: codex"},
        reason_codes=["auto_execute_failed"],
    )
    resp = client.post(
        f"/agent-work/chats/{session_id}/work-result",
        json={"work_id": str(work_id)},
        headers=_h(user_id),
    )
    assert resp.status_code == 200
    text = resp.json()["text"]
    assert text.startswith("[실패]")
    assert "runner not installed: codex" in text


def test_work_result_failed_agent_task_surfaces_stderr_and_redacts(clean, user_id):
    # A non-zero exit surfaces the stderr tail + exit_code, PII-redacted.
    session_id = _create_session(user_id)
    work_id = _seed_work_item(
        user_id,
        state="request_more_data",
        result={
            "exit_code": 2,
            "stdout": "",
            "stderr": "Traceback...\nNo such directory; mail bob@example.com",
        },
        reason_codes=["auto_execute_failed"],
    )
    resp = client.post(
        f"/agent-work/chats/{session_id}/work-result",
        json={"work_id": str(work_id)},
        headers=_h(user_id),
    )
    assert resp.status_code == 200
    text = resp.json()["text"]
    assert text.startswith("[실패]")
    assert "exit_code=2" in text
    assert "bob@example.com" not in text  # failure text is PII-redacted


def test_work_result_cross_owner_session_404(clean, user_id):
    other = _make_user()
    session_id = _create_session(user_id)  # owned by user_id
    work_id = _seed_work_item(user_id, state="resolved", result={"stdout": "done"})
    # `other` does not own the session -> 404 (fail-closed, before any work read).
    resp = client.post(
        f"/agent-work/chats/{session_id}/work-result",
        json={"work_id": str(work_id)},
        headers=_h(other),
    )
    assert resp.status_code == 404


def test_work_result_missing_work_item_404(clean, user_id):
    session_id = _create_session(user_id)
    resp = client.post(
        f"/agent-work/chats/{session_id}/work-result",
        json={"work_id": str(uuid.uuid4())},
        headers=_h(user_id),
    )
    assert resp.status_code == 404
