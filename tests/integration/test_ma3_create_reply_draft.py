"""MA.3 — create_reply_draft tool (decompose reply-draft adapter).

DB-backed: the success path persists a draft_for_review reply candidate via the
P7.1 build_reply_candidate + persist_reply_candidate chain. The request_more_data
paths still open an audit span (which writes), so every test uses `clean`.

Scope is adapter-only (Option B, caller-supplied reply_context): the decompose
action-intake leaf is NOT wired to this adapter yet — that wiring + an /ask mail
context param are a later slice (docs/company-agent-orchestration.md §7 note).
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, insert, select

from orthus.db import session
from orthus.router.tools import create_reply_draft
from orthus.schemas.canonical import MailIngestRequest, MailReplyContext
from orthus.settings import get_settings
from orthus.tables import agent_work_items, audit_log, users


def _company_reply_enabled(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "mail_reply_draft_enabled", True)
    monkeypatch.setattr(settings, "mail_auto_send_enabled", False)
    monkeypatch.setattr(settings, "mail_send_enabled", False)


def _inbound(**overrides) -> MailIngestRequest:
    base = dict(
        backend="nova",
        owner_addr="owner01@nova.example",
        external_id=f"ext-{uuid.uuid4()}",
        message_id=f"<{uuid.uuid4()}@example.com>",
        direction="inbound",
        from_addr="client@partner.com",
        to_addr=["owner01@nova.example"],
        subject="견적 문의",
        body_text="견적 부탁드립니다.",
    )
    base.update(overrides)
    return MailIngestRequest(**base)


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Owner"))
        s.commit()
    return uid


def _reply_item_count() -> int:
    with session() as s:
        return s.execute(
            select(func.count())
            .select_from(agent_work_items)
            .where(agent_work_items.c.source_kind == "mail_reply")
        ).scalar_one()


# --- success path ---------------------------------------------------------------


def test_create_reply_draft_persists_draft_for_review(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    user = _make_user()

    result = create_reply_draft(user, _inbound(), owner_scoped=False, settings=get_settings())

    assert result.outcome == "draft_for_review"
    assert result.required_data == []
    item = result.item
    assert item is not None
    assert item.action_family == "email_send"
    assert item.state == "draft_for_review"
    assert item.source_kind == "mail_reply"
    # typed reply_context survives into the persisted payload (MA.3 typed payload)
    ctx = MailReplyContext.model_validate(item.payload["reply_context"])
    assert ctx.backend == "nova"
    assert ctx.from_addr == "owner01@nova.example"
    assert ctx.to_addr == "client@partner.com"
    assert ctx.subject.startswith("Re: ")
    # company-shared by default (owner_scoped=False)
    assert item.owner_id is None


def test_create_reply_draft_owner_scoped_binds_owner(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    user = _make_user()

    result = create_reply_draft(user, _inbound(), settings=get_settings(), owner_scoped=True)

    assert result.outcome == "draft_for_review"
    assert result.item is not None
    assert result.item.owner_id == user


# --- request_more_data paths ----------------------------------------------------


def test_create_reply_draft_no_context_request_more_data(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    user = _make_user()

    result = create_reply_draft(user, None, owner_scoped=False, settings=get_settings())

    assert result.outcome == "request_more_data"
    assert result.item is None
    assert "reply_context" in result.required_data
    assert result.reason == "no_reply_context"
    # no phantom work item is persisted when there is nothing to draft
    assert _reply_item_count() == 0


def test_create_reply_draft_non_routable_recipient_request_more_data(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    user = _make_user()
    payload = _inbound(to_addr=["someone@gmail.com"], owner_addr="someone@gmail.com")

    result = create_reply_draft(user, payload, owner_scoped=False, settings=get_settings())

    assert result.outcome == "request_more_data"
    assert result.item is None
    # the cause is an unroutable recipient, not missing caller input
    assert result.required_data == []
    assert result.reason == "no_routable_reply"
    assert _reply_item_count() == 0


def test_create_reply_draft_outbound_request_more_data(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    user = _make_user()

    result = create_reply_draft(
        user, _inbound(direction="outbound"), owner_scoped=False, settings=get_settings()
    )

    assert result.outcome == "request_more_data"
    assert result.item is None
    assert result.required_data == []


def test_create_reply_draft_flag_off_request_more_data(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    monkeypatch.setattr(get_settings(), "mail_reply_draft_enabled", False)
    user = _make_user()

    result = create_reply_draft(user, _inbound(), owner_scoped=False, settings=get_settings())

    assert result.outcome == "request_more_data"
    assert result.item is None
    assert _reply_item_count() == 0


# --- audit span -----------------------------------------------------------------


def test_create_reply_draft_emits_audit_span(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    user = _make_user()

    create_reply_draft(user, _inbound(), owner_scoped=False, settings=get_settings())

    with session() as s:
        phases = (
            s.execute(
                select(audit_log.c.phase)
                .where(audit_log.c.node == "router.create_reply_draft")
                .order_by(audit_log.c.audit_id)
            )
            .scalars()
            .all()
        )
    assert "enter" in phases
    assert "exit" in phases
