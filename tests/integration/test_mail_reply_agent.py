"""Local-agent reply draft (owner-scope mail -> self-assigned agent_task).

DB-backed. No LLM is involved (the builder self-assigns deterministically — the
draft text is produced later by the owner's daemon, simulated here only via the
collector command queue). Mirrors test_mail_delegation.py.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, insert, select

from orthus.collector.commands import list_pending_commands
from orthus.db import session
from orthus.mail.ingest import ingest_mail
from orthus.mail.reply_delegation import build_reply_draft_agent_task
from orthus.schemas.canonical import MailIngestRequest
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
    monkeypatch.setattr(settings, "mail_reply_draft_agent_enabled", True)
    monkeypatch.setattr(settings, "agent_task_enabled", True)
    # Central P7.1 reply draft stays off so the flow only exercises the agent path.
    monkeypatch.setattr(settings, "mail_reply_draft_enabled", False)


def _make_owner(email: str = "owner01@acme.example") -> uuid.UUID:
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


def _inbound(**overrides) -> MailIngestRequest:
    base = dict(
        backend="acme",
        owner_addr="owner01@acme.example",
        external_id=f"ext-{uuid.uuid4()}",
        message_id=f"<{uuid.uuid4()}@example.com>",
        direction="inbound",
        from_addr="client@partner.com",
        to_addr=["owner01@acme.example"],
        subject="견적 문의",
        body_text="견적 부탁드립니다.",
    )
    base.update(overrides)
    return MailIngestRequest(**base)


def test_reply_draft_agent_task_auto_dispatches(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_reply_draft_agent_task(_inbound(), owner, get_settings())

    assert item is not None
    assert item.action_family == "agent_task"
    assert item.state == "auto_execute"
    # self-assigned: the mailbox owner is the assignee, dispatched to their daemon.
    assert item.payload["assignee_user_id"] == str(owner)
    assert item.payload["assignee_node_id"] == COMPANY_NODE
    assert item.payload["runner"] == "claude"
    assert item.payload["mode"] == "knowledge"
    # mail origin is recorded for slice-5 completion surfacing (reply to sender).
    origin = item.payload["mail_origin"]
    assert origin["sender_hint"] == "client@partner.com"
    assert origin["source_canonical_id"].startswith("mail:acme:")
    # the agent_task command was dispatched to the owner's enrolled daemon.
    pending = list_pending_commands(owner)
    assert pending.count == 1
    command = pending.items[0]
    assert command.kind == "agent_task"
    assert command.payload["work_item_id"] == str(item.work_id)
    assert "답장 초안" in command.payload["instruction"]


def test_reply_draft_agent_task_flag_off_noop(clean, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(get_settings(), "mail_reply_draft_agent_enabled", False)
    owner = _make_owner()
    _enroll_daemon(owner)

    assert build_reply_draft_agent_task(_inbound(), owner, get_settings()) is None
    with session() as s:
        work_count = s.execute(select(func.count()).select_from(agent_work_items)).scalar_one()
        cmd_count = s.execute(select(func.count()).select_from(collector_commands)).scalar_one()
    assert work_count == 0
    assert cmd_count == 0


def test_reply_draft_agent_task_not_company_noop(clean, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(get_settings(), "node_kind", "personal")
    owner = _make_owner()

    assert build_reply_draft_agent_task(_inbound(), owner, get_settings()) is None


def test_reply_draft_agent_task_outbound_noop(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    assert (
        build_reply_draft_agent_task(_inbound(direction="outbound"), owner, get_settings()) is None
    )


def test_reply_draft_agent_task_unenrolled_requests_more_data(clean, monkeypatch):
    _enable(monkeypatch)
    owner = _make_owner()  # owner exists but has NO collector daemon enrolled.

    item = build_reply_draft_agent_task(_inbound(), owner, get_settings())

    assert item is not None
    assert item.state == "request_more_data"
    assert item.payload.get("auto_execution") is None
    with session() as s:
        cmd_count = s.execute(select(func.count()).select_from(collector_commands)).scalar_one()
    assert cmd_count == 0


def test_ingest_personal_scope_uses_agent_not_central(clean, monkeypatch):
    """Owner-scope inbound mail with the flag on creates an agent_task, and NOT a
    central email_send reply candidate (mutual exclusion at the ingest call-site)."""
    _enable(monkeypatch)
    # Central reply draft is also "on" to prove the agent path wins for owner scope.
    monkeypatch.setattr(get_settings(), "mail_reply_draft_enabled", True)
    owner = _make_owner()
    _enroll_daemon(owner)

    result = ingest_mail(_inbound(message_id="<reply-agent-1@example.com>"), get_settings())

    assert result.ingested is True
    assert result.scope == "personal"
    with session() as s:
        agent_count = s.execute(
            select(func.count())
            .select_from(agent_work_items)
            .where(agent_work_items.c.action_family == "agent_task")
        ).scalar_one()
        email_count = s.execute(
            select(func.count())
            .select_from(agent_work_items)
            .where(agent_work_items.c.action_family == "email_send")
        ).scalar_one()
    assert agent_count == 1
    assert email_count == 0
    pending = list_pending_commands(owner)
    assert pending.count == 1


def test_completion_email_surfaces_clean_draft_not_json(clean, monkeypatch):
    """Completing a reply-draft agent_task surfaces the agent's draft verbatim as
    the email_send body: the claude `--output-format json` envelope is unwrapped
    to `.result`, with no generic 'task completed' wrapper."""
    import json as _json

    from orthus.agentwork import (
        list_agent_work_items,
        resolve_agent_task_work_item_result,
    )
    from orthus.collector.commands import claim_command, complete_command

    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_reply_draft_agent_task(_inbound(), owner, get_settings())
    assert item is not None and item.state == "auto_execute"

    draft_text = "안녕하세요.\n\n다음 주 화/수 오후 30분 미팅 가능합니다. 편하신 시간 알려주세요.\n\n감사합니다."
    envelope = _json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": draft_text,
            "total_cost_usd": 0.21,
        }
    )
    command_id = uuid.UUID(item.payload["auto_execution"]["command_id"])
    claim_command(owner, command_id)
    command = complete_command(
        owner,
        command_id,
        status="done",
        result_payload={"exit_code": 0, "stdout": envelope, "runner": "claude"},
    )
    resolved = resolve_agent_task_work_item_result(
        command.payload, command_status=command.status, result=command.result
    )
    assert resolved is not None and resolved.state == "resolved"

    completion = next(
        i
        for i in list_agent_work_items(owner, source_kind="agent_task")
        if i.action_family == "email_send"
    )
    body = completion.payload["email_draft"]["body_template"]
    assert body == draft_text  # the agent's draft, verbatim
    assert "total_cost_usd" not in body  # no JSON envelope leaked
    assert "요청하신 작업이 완료되었습니다" not in body  # no generic wrapper


def test_completion_reply_draft_is_sendable_on_approve(clean, monkeypatch):
    """A completed reply-draft agent_task yields an email_send item carrying the
    routing context the approval route needs to actually send: a top-level
    payload.reply_context (the exact send trigger decide_work_item checks) plus
    the same context nested in email_draft (read by send_approved_reply), with the
    real From (company address that received the mail), To (original sender), and a
    Re: subject. Closes the SEND GAP for the agent_task reply path."""
    import json as _json

    from orthus.agentwork import (
        list_agent_work_items,
        resolve_agent_task_work_item_result,
    )
    from orthus.collector.commands import claim_command, complete_command

    _enable(monkeypatch)
    owner = _make_owner()
    _enroll_daemon(owner)

    item = build_reply_draft_agent_task(_inbound(), owner, get_settings())
    assert item is not None and item.state == "auto_execute"

    draft_text = "안녕하세요. 견적서 첨부드립니다. 감사합니다."
    envelope = _json.dumps({"type": "result", "is_error": False, "result": draft_text})
    command_id = uuid.UUID(item.payload["auto_execution"]["command_id"])
    claim_command(owner, command_id)
    command = complete_command(
        owner,
        command_id,
        status="done",
        result_payload={"exit_code": 0, "stdout": envelope, "runner": "claude"},
    )
    resolve_agent_task_work_item_result(
        command.payload, command_status=command.status, result=command.result
    )

    completion = next(
        i
        for i in list_agent_work_items(owner, source_kind="agent_task")
        if i.action_family == "email_send"
    )
    # Top-level reply_context is exactly what decide_work_item checks before it
    # calls send_approved_reply on approve.
    ctx = completion.payload.get("reply_context")
    assert isinstance(ctx, dict)
    assert ctx["from_addr"] == "owner01@acme.example"  # company addr that received it
    assert ctx["to_addr"] == "client@partner.com"  # original sender = reply recipient
    assert ctx["backend"] == "acme"
    assert ctx["subject"].startswith("Re:")
    # send_approved_reply reads the nested copy + the agent's draft as the body.
    nested = completion.payload["email_draft"]["reply_context"]
    assert nested["to_addr"] == "client@partner.com"
    assert completion.payload["email_draft"]["body_template"] == draft_text
