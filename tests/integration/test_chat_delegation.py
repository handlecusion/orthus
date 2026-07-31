"""Delegation slice 6: chat (/ask) natural-language delegation to an agent.

DB-backed. The chat delegation path is deterministic: NO LLM is mocked or called
on the /ask request path — intake is by an explicit delegation prefix only
(``parse_chat_delegation``). The daemon side is simulated by inspecting the queued
collector command directly (no real claude/codex/hermes process runs).

Covers:
- detect: explicit delegation prefix -> "agent_task"; keyword command families
  keep working for non-prefixed text; info queries / plain commands -> None.
- candidate/intake: self-default when no teammate is named; explicit `@email`
  teammate; enrolled -> auto_execute + command enqueue; unenrolled ->
  request_more_data.
- /ask e2e: an operator delegation question returns an agent_work ack and creates
  the work item.
"""

from __future__ import annotations

import uuid

from orthus.agentwork import detect_assistant_command_action
from orthus.agentwork.service import create_assistant_command_work_item
from orthus.api.main import app
from orthus.collector.commands import list_pending_commands
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import agent_work_items, auth_identities, collector_tokens, users
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

client = TestClient(app)

COMPANY_NODE = "company"


# --- helpers ------------------------------------------------------------------


def _enable_agent_task(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "agent_task_enabled", True)


def _make_user(email: str | None = None) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        if email is not None:
            s.execute(
                insert(auth_identities).values(
                    identity_id=uuid.uuid4(),
                    user_id=uid,
                    provider="test",
                    provider_subject=uuid.uuid4().hex,
                    email=email,
                    email_verified=True,
                )
            )
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


# --- detect -------------------------------------------------------------------


def test_detect_explicit_delegation_prefix_returns_agent_task(clean):
    assert detect_assistant_command_action("위임: 리포 테스트 고쳐줘") == "agent_task"
    assert detect_assistant_command_action("에이전트한테 리포 분석시켜줘") == "agent_task"
    assert detect_assistant_command_action("내 에이전트에게 문서 정리시켜줘") == "agent_task"
    assert detect_assistant_command_action("/위임 코드 수정해줘") == "agent_task"


def test_detect_existing_families_unchanged(clean):
    # Keyword command families keep working for non-prefixed text.
    assert detect_assistant_command_action("gmail 동기화 해줘") == "connector_sync"
    assert detect_assistant_command_action("john에게 이메일 초안 만들어줘") == "email_send"
    assert detect_assistant_command_action("회의록 문서 작성해줘") == "document_draft"
    assert detect_assistant_command_action("보드 정리해줘") == "personal_board_cleanup"
    assert detect_assistant_command_action("위키 반영해줘") == "central_wiki_task_cleanup"


def test_detect_delegation_prefix_takes_priority_over_family(clean):
    # An explicit delegation prefix wins over a keyword family that would
    # otherwise match the rest of the text.
    assert detect_assistant_command_action("위임: gmail 동기화 해줘") == "agent_task"


def test_detect_info_queries_and_plain_commands_return_none(clean):
    assert detect_assistant_command_action("휴가 정책이 뭐야?") is None
    assert detect_assistant_command_action("오늘 일정 알려줘") is None
    assert detect_assistant_command_action("프로젝트 현황 어떻게 돼?") is None
    # A bare command verb with no delegation prefix is not a delegation.
    assert detect_assistant_command_action("할일 만들어줘") is None
    # Delegation-flavoured wording WITHOUT an explicit prefix no longer fires.
    assert detect_assistant_command_action("이 작업 dev에게 시켜줘") is None


def test_detect_wiki_summary_read_is_not_a_command(clean):
    # A "summarize/explain what the wiki says" ASK is a knowledge question and must
    # fall through to the normal /ask answer, NOT be queued as central_wiki_task_cleanup.
    # Regression: "정리된" (adjective) once matched the "정리" command verb and "위키"
    # matched the wiki family, queuing the read as agent_work ("agent_work 결과 없음").
    assert detect_assistant_command_action("아틀라스 제품 관련해서 회사 위키에 정리된 내용 요약해줘") is None
    assert detect_assistant_command_action("회사 위키에 정리된 내용 설명해줘") is None
    # The genuine cleanup command still fires (no read-intent verb).
    assert detect_assistant_command_action("위키 반영해줘") == "central_wiki_task_cleanup"


# --- candidate / intake -------------------------------------------------------


def test_chat_delegation_self_default_auto_dispatches(clean, monkeypatch):
    # No teammate named -> the requester delegates to themselves.
    _enable_agent_task(monkeypatch)
    actor = _make_user()
    _enroll_daemon(actor)

    item = create_assistant_command_work_item(
        actor,
        "위임: 리포 테스트를 수정해줘",
        actor_role="owner",
    )

    assert item is not None
    assert item.action_family == "agent_task"
    assert item.state == "auto_execute"
    assert item.payload["assignee_user_id"] == str(actor)
    assert item.payload["assignee_node_id"] == COMPANY_NODE
    assert item.payload["runner"] == "codex"
    # "수정" implies a code-editing task -> code mode.
    assert item.payload["mode"] == "code"
    pending = list_pending_commands(actor)
    assert pending.count == 1
    command = pending.items[0]
    assert command.kind == "agent_task"
    assert command.payload["work_item_id"] == str(item.work_id)
    assert command.payload["instruction"] == "리포 테스트를 수정해줘"


def test_chat_delegation_named_teammate_dispatches_to_them(clean, monkeypatch):
    _enable_agent_task(monkeypatch)
    actor = _make_user()
    assignee = _make_user(email="dev@example.com")
    _enroll_daemon(assignee)

    item = create_assistant_command_work_item(
        actor,
        "@dev@example.com 위임: 리포 테스트를 고쳐줘",
        actor_role="owner",
    )

    assert item is not None
    assert item.state == "auto_execute"
    assert item.payload["assignee_user_id"] == str(assignee)
    # the command landed in the teammate's queue, not the requester's.
    assert list_pending_commands(assignee).count == 1
    assert list_pending_commands(actor).count == 0


def test_chat_delegation_unenrolled_requests_more_data(clean, monkeypatch):
    _enable_agent_task(monkeypatch)
    actor = _make_user()
    # assignee resolves but has NO collector daemon enrolled.
    _make_user(email="dev@example.com")

    item = create_assistant_command_work_item(
        actor,
        "@dev@example.com 위임: 작업을 처리해줘",
        actor_role="owner",
    )

    assert item is not None
    assert item.state == "request_more_data"
    assert item.payload.get("auto_execution") is None
    assert list_pending_commands(actor).count == 0


def test_chat_delegation_flag_off_does_not_dispatch(clean, monkeypatch):
    # agent_task disabled -> gate rejects, no command is enqueued.
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "agent_task_enabled", False)
    actor = _make_user()
    _enroll_daemon(actor)

    item = create_assistant_command_work_item(
        actor,
        "위임: 리포 테스트를 고쳐줘",
        actor_role="owner",
    )

    assert item is not None
    assert item.state == "rejected"
    assert list_pending_commands(actor).count == 0


def test_chat_non_delegation_falls_through(clean, monkeypatch):
    # A plain knowledge question is not an explicit delegation -> no candidate
    # (so /ask answers normally), and no queue row is created.
    _enable_agent_task(monkeypatch)
    actor = _make_user()

    item = create_assistant_command_work_item(
        actor,
        "휴가 정책 뭐야",
        actor_role="owner",
    )

    assert item is None
    with session() as s:
        count = s.execute(select(func.count()).select_from(agent_work_items)).scalar_one()
    assert count == 0


# --- /ask e2e -----------------------------------------------------------------


def test_orchestrate_delegation_returns_agent_work_ack(clean, monkeypatch):
    _enable_agent_task(monkeypatch)
    actor = _make_user()
    _enroll_daemon(actor)
    hdr = {"X-User-Id": str(actor)}

    # Delegation intake moved off /ask to the agent-work chat orchestrator.
    created = client.post("/agent-work/chats", json={}, headers=hdr)
    assert created.status_code == 200, created.text
    sid = created.json()["session_id"]
    asked = client.post(
        f"/agent-work/chats/{sid}/orchestrate",
        headers=hdr,
        json={"text": "위임: 리포 테스트를 고쳐줘"},
    )

    assert asked.status_code == 200
    body = asked.json()
    assert body["mode"] == "agent_work"
    assert body["agent_work"]["action_family"] == "agent_task"
    assert body["agent_work"]["state"] == "auto_execute"

    item = client.get(
        f"/agent-work/{body['agent_work']['work_id']}",
        headers={"X-User-Id": str(actor)},
    )
    assert item.status_code == 200
    payload = item.json()["payload"]
    assert payload["assignee_user_id"] == str(actor)
    assert list_pending_commands(actor).count == 1
