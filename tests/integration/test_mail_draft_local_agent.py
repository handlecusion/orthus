"""On-demand local-agent mail draft (the "AI 초안" button -> self-assigned agent_task).

DB-backed. No LLM is involved at build time (the builder self-assigns
deterministically; the draft text is produced later by the owner's daemon,
simulated here via the collector command queue). Mirrors test_mail_reply_agent.py
but exercises the compose_draft completion branch (writes payload.mail_draft onto
the SAME work item, never an email_send item).
"""

from __future__ import annotations

import json as _json
import uuid

from sqlalchemy import func, insert, select

from orthus.agentwork import resolve_agent_task_work_item_result
from orthus.agentwork.service import get_agent_work_item
from orthus.collector.commands import claim_command, complete_command, list_pending_commands
from orthus.db import session
from orthus.mail.draft_delegation import build_compose_draft_agent_task
from orthus.schemas.canonical import MailDraftDispatchRequest
from orthus.settings import get_settings
from orthus.tables import (
    agent_work_items,
    auth_identities,
    collector_commands,
    collector_tokens,
    users,
)

COMPANY_NODE = "company"


def _enable(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "mail_agent_draft_enabled", True)
    monkeypatch.setattr(settings, "agent_task_enabled", True)


def _make_owner(email: str = "2fe@acme.example") -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Owner"))
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=uid,
                provider="test",
                provider_subject=f"sub-{uid}",
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
                scopes=["ingest", "agent_task"],
            )
        )
        s.commit()


def _req(**overrides) -> MailDraftDispatchRequest:
    base = dict(
        mode="reply",
        instruction="정중하게 미팅 일정을 제안",
        to="client@partner.com",
        subject="견적 문의",
        context="안녕하세요, 견적 부탁드립니다. 연락처는 010-1234-5678 입니다.",
    )
    base.update(overrides)
    return MailDraftDispatchRequest(**base)


def _complete_with(owner, item, *, status="done", result_payload):
    command_id = uuid.UUID(item.payload["auto_execution"]["command_id"])
    claim_command(owner, command_id)
    command = complete_command(owner, command_id, status=status, result_payload=result_payload)
    return resolve_agent_task_work_item_result(
        command.payload, command_status=command.status, result=command.result
    )


# --- build / dispatch -------------------------------------------------------


def test_compose_draft_auto_dispatches(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_compose_draft_agent_task(
        _req(), owner, actor_role="admin", settings=get_settings()
    )

    assert item is not None
    assert item.action_family == "agent_task"
    assert item.state == "auto_execute"
    # self-assigned: the requester is the assignee, dispatched to their own daemon.
    assert item.payload["assignee_user_id"] == str(owner)
    assert item.payload["assignee_node_id"] == COMPANY_NODE
    assert item.payload["runner"] == "claude"
    assert item.payload["mode"] == "knowledge"
    origin = item.payload["mail_origin"]
    assert origin["kind"] == "compose_draft"
    assert origin["draft_mode"] == "reply"
    # the agent_task command was dispatched to the owner's enrolled daemon, with the
    # full system+user prompt folded into the single instruction.
    pending = list_pending_commands(owner)
    assert pending.count == 1
    command = pending.items[0]
    assert command.kind == "agent_task"
    assert command.payload["work_item_id"] == str(item.work_id)
    assert "JSON" in command.payload["instruction"]


def test_compose_draft_runner_override_hermes(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_compose_draft_agent_task(
        _req(runner="hermes"), owner, actor_role="owner", settings=get_settings()
    )
    assert item is not None
    assert item.payload["runner"] == "hermes"


def test_compose_draft_flag_off_noop(clean, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(get_settings(), "mail_agent_draft_enabled", False)
    owner = _make_owner()
    _enroll_daemon(owner)

    assert (
        build_compose_draft_agent_task(_req(), owner, actor_role="admin", settings=get_settings())
        is None
    )
    with session() as s:
        work_count = s.execute(select(func.count()).select_from(agent_work_items)).scalar_one()
        cmd_count = s.execute(select(func.count()).select_from(collector_commands)).scalar_one()
    assert work_count == 0
    assert cmd_count == 0


def test_compose_draft_not_company_noop(clean, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(get_settings(), "node_kind", "personal")
    owner = _make_owner()
    _enroll_daemon(owner)

    assert (
        build_compose_draft_agent_task(_req(), owner, actor_role="admin", settings=get_settings())
        is None
    )


def test_compose_draft_unenrolled_returns_none_no_stray_row(clean, monkeypatch):
    """No enrolled daemon -> None (caller falls back); never leaves a stray queue row."""
    _enable(monkeypatch)
    owner = _make_owner()  # owner exists but has NO collector daemon enrolled.

    assert (
        build_compose_draft_agent_task(_req(), owner, actor_role="admin", settings=get_settings())
        is None
    )
    with session() as s:
        work_count = s.execute(select(func.count()).select_from(agent_work_items)).scalar_one()
        cmd_count = s.execute(select(func.count()).select_from(collector_commands)).scalar_one()
    assert work_count == 0
    assert cmd_count == 0


def test_compose_draft_non_operator_returns_none(clean, monkeypatch):
    """A non owner/admin actor produces no queue row (matches assistant_command)."""
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    assert (
        build_compose_draft_agent_task(_req(), owner, actor_role="member", settings=get_settings())
        is None
    )


def test_compose_draft_instruction_not_redacted(clean, monkeypatch):
    """§5-B carve-out: the folded mail context (phone/email) reaches the local agent
    UNredacted, so the draft is faithful — matching the central sync /mail/draft."""
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_compose_draft_agent_task(_req(), owner, actor_role="admin", settings=get_settings())
    assert item is not None
    instruction = item.payload["instruction"]
    assert "010-1234-5678" in instruction  # phone kept verbatim, not [REDACTED]


# --- completion (compose_draft branch) --------------------------------------


def test_compose_draft_completion_writes_mail_draft_no_email(clean, monkeypatch):
    """A done compose_draft parses {subject,body} onto payload.mail_draft and creates
    NO email_send item (the user sends manually through /mail/send)."""
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_compose_draft_agent_task(_req(), owner, actor_role="admin", settings=get_settings())
    assert item is not None and item.state == "auto_execute"

    draft = {"subject": "Re: 견적 문의", "body": "안녕하세요.\n\n다음 주 화요일 오후 가능합니다.\n\n감사합니다."}
    envelope = _json.dumps({"type": "result", "is_error": False, "result": _json.dumps(draft)})
    resolved = _complete_with(
        owner, item, result_payload={"exit_code": 0, "stdout": envelope, "runner": "claude"}
    )

    assert resolved is not None and resolved.state == "resolved"
    assert resolved.payload["mail_draft"]["subject"] == "Re: 견적 문의"
    assert "다음 주 화요일" in resolved.payload["mail_draft"]["body"]
    # NO second email_send item.
    with session() as s:
        email_count = s.execute(
            select(func.count())
            .select_from(agent_work_items)
            .where(agent_work_items.c.action_family == "email_send")
        ).scalar_one()
    assert email_count == 0
    # owner-scoped read returns the same draft.
    fetched = get_agent_work_item(owner, resolved.work_id)
    assert fetched is not None and fetched.payload["mail_draft"]["body"]


def test_compose_draft_completion_plain_text_fallback(clean, monkeypatch):
    """hermes-style plain text (no JSON) becomes the body; subject falls back."""
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_compose_draft_agent_task(
        _req(runner="hermes"), owner, actor_role="admin", settings=get_settings()
    )
    assert item is not None and item.state == "auto_execute"

    plain = "안녕하세요. 제안 주신 일정 좋습니다. 감사합니다."
    resolved = _complete_with(
        owner, item, result_payload={"exit_code": 0, "stdout": plain, "runner": "hermes"}
    )
    assert resolved is not None and resolved.state == "resolved"
    assert resolved.payload["mail_draft"]["body"] == plain
    # reply mode keeps the original subject as the fallback.
    assert resolved.payload["mail_draft"]["subject"] == "견적 문의"


def test_compose_draft_empty_body_requests_more_data(clean, monkeypatch):
    """An empty agent answer downgrades to request_more_data so the FE shows retry."""
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_compose_draft_agent_task(_req(), owner, actor_role="admin", settings=get_settings())
    assert item is not None and item.state == "auto_execute"

    resolved = _complete_with(
        owner, item, result_payload={"exit_code": 0, "stdout": "", "runner": "claude"}
    )
    assert resolved is not None and resolved.state == "request_more_data"
    assert resolved.payload["mail_draft"]["body"] == ""


def test_compose_draft_completion_failed_status_request_more_data(clean, monkeypatch):
    """A failed daemon run leaves the compose draft as request_more_data, no email."""
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_compose_draft_agent_task(_req(), owner, actor_role="admin", settings=get_settings())
    assert item is not None and item.state == "auto_execute"

    resolved = _complete_with(
        owner,
        item,
        status="failed",
        result_payload={"exit_code": 1, "stderr": "boom", "runner": "claude"},
    )
    assert resolved is not None and resolved.state == "request_more_data"
    with session() as s:
        email_count = s.execute(
            select(func.count())
            .select_from(agent_work_items)
            .where(agent_work_items.c.action_family == "email_send")
        ).scalar_one()
    assert email_count == 0


# --- endpoint tiers ---------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    from orthus.api.main import app

    return TestClient(app)


def test_dispatch_endpoint_flag_off_404(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_agent_draft_enabled", False)
    owner = _make_owner()
    res = _client().post(
        "/mail/draft/dispatch",
        json={"mode": "compose", "instruction": "테스트"},
        headers={"X-User-Id": str(owner)},
    )
    assert res.status_code == 404


def test_dispatch_endpoint_unavailable_without_daemon(clean, monkeypatch):
    """Flag on, no enrolled daemon, no central fallback -> mode=unavailable."""
    _enable(monkeypatch)
    monkeypatch.setattr(get_settings(), "mail_draft_central_fallback", False)
    owner = _make_owner()  # no daemon enrolled
    res = _client().post(
        "/mail/draft/dispatch",
        json={"mode": "compose", "instruction": "테스트"},
        headers={"X-User-Id": str(owner)},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "unavailable"
    assert body["work_id"] is None
    assert body["message"]


def test_dispatch_endpoint_central_fallback(clean, monkeypatch):
    """Flag on, no daemon, central fallback on -> synchronous central draft inline."""
    _enable(monkeypatch)
    monkeypatch.setattr(get_settings(), "mail_draft_central_fallback", True)
    owner = _make_owner()  # no daemon enrolled
    res = _client().post(
        "/mail/draft/dispatch",
        json={"mode": "compose", "instruction": "테스트", "subject": "제목"},
        headers={"X-User-Id": str(owner)},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "central"
    assert body["draft"] is not None
    assert "body" in body["draft"]


def test_compose_draft_question_body_does_not_trigger_hitl(clean, monkeypatch):
    """A draft body containing '?' must NOT be misread as a request for operator input."""
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_compose_draft_agent_task(_req(), owner, actor_role="admin", settings=get_settings())
    assert item is not None

    draft = {"subject": "Re: 견적 문의", "body": "언제가 편하신가요? 시간 알려주시면 맞추겠습니다."}
    envelope = _json.dumps({"type": "result", "is_error": False, "result": _json.dumps(draft)})
    resolved = _complete_with(
        owner, item, result_payload={"exit_code": 0, "stdout": envelope, "runner": "claude"}
    )
    assert resolved is not None and resolved.state == "resolved"
    assert resolved.payload.get("pending_user_input") is None
