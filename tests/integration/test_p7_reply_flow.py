"""P7.1 mail reply draft end-to-end flow + real-send guards.

DB-backed: ingest -> reply candidate -> lazy LLM draft on GET -> approve -> send.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

import orthus.agentwork.service as service
import orthus.mail.reply as reply_mod
from orthus.agentwork import ensure_reply_draft_for_work_item
from orthus.agentwork.service import (
    get_agent_work_item,
    persist_reply_candidate,
    update_email_draft,
)
from orthus.api.main import app
from orthus.api.routes import agent_work as agent_work_routes
from orthus.auth import AuthenticatedUser
from orthus.db import session
from orthus.mail.ingest import ingest_mail
from orthus.mail.reply import build_reply_candidate, send_approved_reply
from orthus.schemas.canonical import (
    AgentWorkItem,
    MailIngestRequest,
    MailSendResult,
)
from orthus.settings import get_settings
from orthus.tables import agent_work_items, auth_identities, email_send_log, users

client = TestClient(app)
DEMO_USER = "00000000-0000-4000-8000-000000000001"
DEMO_HEADERS = {"X-User-Id": DEMO_USER}


def _email_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _company_reply_enabled(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "mail_reply_draft_enabled", True)
    monkeypatch.setattr(settings, "mail_auto_send_enabled", False)


def _enable_send(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_send_enabled", True)
    monkeypatch.setattr(settings, "mail_nova_base_url", "https://nova.test/v0")
    monkeypatch.setattr(settings, "mail_nova_api_key", "nova-key")
    monkeypatch.setattr(settings, "mail_nova_owner", "owner01@nova.example")


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


def _owner_override():
    return lambda: AuthenticatedUser(
        user_id=uuid.UUID(DEMO_USER),
        auth_mode="session",
        display_name="Owner",
        role="owner",
        node_id="company",
    )


class _StubChat:
    model_id = "stub:chat"

    def __init__(self, response: str = "안녕하세요, 답장 본문입니다."):
        self.response = response

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        return self.response


class _RaisingChat:
    model_id = "raise:chat"

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        raise RuntimeError("llm unavailable")


# --- ensure_reply_draft_for_work_item -------------------------------------------


def test_ensure_reply_draft_llm_drafted(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    user = _make_user()
    candidate = build_reply_candidate(_inbound(), user, get_settings())
    item = persist_reply_candidate(user, candidate, settings=get_settings())
    assert item is not None
    assert item.state == "draft_for_review"

    monkeypatch.setattr(service, "get_chat_model_for", lambda _task: _StubChat("AI 본문"))
    drafted = ensure_reply_draft_for_work_item(user, item)

    draft = drafted.payload["email_draft"]
    assert draft["source"] == "agent_reply_draft"
    assert draft["llm_drafted"] is True
    assert draft["body_template"] == "AI 본문"
    assert draft["reply_context"]["backend"] == "nova"
    assert drafted.payload["auto_send_candidacy"]["eligible"] is False
    assert drafted.payload["auto_send_candidacy"]["used_for_outcome"] is False


def test_ensure_reply_draft_falls_back_on_llm_error(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    user = _make_user()
    candidate = build_reply_candidate(_inbound(), user, get_settings())
    item = persist_reply_candidate(user, candidate, settings=get_settings())

    monkeypatch.setattr(service, "get_chat_model_for", lambda _task: _RaisingChat())
    drafted = ensure_reply_draft_for_work_item(user, item)

    draft = drafted.payload["email_draft"]
    assert draft["llm_drafted"] is False
    assert "보내주신 메일" in draft["body_template"]


def test_ensure_reply_draft_idempotent(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    user = _make_user()
    candidate = build_reply_candidate(_inbound(), user, get_settings())
    item = persist_reply_candidate(user, candidate, settings=get_settings())

    monkeypatch.setattr(service, "get_chat_model_for", lambda _task: _StubChat("첫 본문"))
    first = ensure_reply_draft_for_work_item(user, item)
    # Second call must NOT regenerate (would call _RaisingChat and break if it ran).
    monkeypatch.setattr(service, "get_chat_model_for", lambda _task: _RaisingChat())
    second = ensure_reply_draft_for_work_item(user, first)

    assert second.payload["email_draft"]["body_template"] == "첫 본문"


def test_update_email_draft_edits_body_and_preserves_context(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    user = _make_user()
    candidate = build_reply_candidate(_inbound(), user, get_settings())
    item = persist_reply_candidate(user, candidate, settings=get_settings())
    monkeypatch.setattr(service, "get_chat_model_for", lambda _task: _StubChat("AI 본문"))
    drafted = ensure_reply_draft_for_work_item(user, item)
    assert drafted.payload["email_draft"]["body_template"] == "AI 본문"

    edited = update_email_draft(user, item.work_id, body_template="사람이 고친 본문")
    assert edited.state == "draft_for_review"
    assert edited.payload["email_draft"]["body_template"] == "사람이 고친 본문"
    # reply_context must survive the edit so approve still sends through the adapter
    assert edited.payload["email_draft"]["reply_context"]["backend"] == "nova"

    # a later GET/approve must NOT regenerate over the human edit
    monkeypatch.setattr(service, "get_chat_model_for", lambda _task: _RaisingChat())
    again = ensure_reply_draft_for_work_item(user, edited)
    assert again.payload["email_draft"]["body_template"] == "사람이 고친 본문"

    # empty body is rejected
    with pytest.raises(ValueError):
        update_email_draft(user, item.work_id, body_template="   ")


# --- send_approved_reply --------------------------------------------------------


def _drafted_reply_item(user: uuid.UUID, monkeypatch) -> AgentWorkItem:
    candidate = build_reply_candidate(_inbound(), user, get_settings())
    item = persist_reply_candidate(user, candidate, settings=get_settings())
    monkeypatch.setattr(service, "get_chat_model_for", lambda _task: _StubChat("본문"))
    return ensure_reply_draft_for_work_item(user, item)


def _stub_send(monkeypatch, result: MailSendResult) -> dict:
    seen: dict = {}

    async def fake_send_mail(request, settings, *, client=None):  # noqa: ANN001
        seen["request"] = request
        return result

    monkeypatch.setattr(reply_mod, "send_mail", fake_send_mail)
    return seen


def test_send_approved_reply_logs_hash_only(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    _enable_send(monkeypatch)
    user = _make_user()
    item = _drafted_reply_item(user, monkeypatch)
    _stub_send(
        monkeypatch,
        MailSendResult(backend="nova", status="sent", provider_message_id="re_xyz"),
    )

    outcome = asyncio.run(
        send_approved_reply(item, reviewer_id=user, reviewer_role="owner", settings=get_settings())
    )

    assert outcome["sent"] is True
    assert outcome["backend"] == "nova"
    assert outcome["provider_message_id_hash"] == _email_hash("re_xyz")
    with session() as s:
        row = s.execute(
            select(email_send_log).where(email_send_log.c.work_id == item.work_id)
        ).first()
    assert row is not None
    log = row._mapping
    assert log["origin"] == "agent_reply"
    assert log["status"] == "sent"
    assert log["recipient_hash"] == _email_hash("client@partner.com")
    assert "client@partner.com" not in str(dict(log))


def test_send_approved_reply_send_disabled(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    monkeypatch.setattr(get_settings(), "mail_send_enabled", False)
    user = _make_user()
    item = _drafted_reply_item(user, monkeypatch)

    outcome = asyncio.run(
        send_approved_reply(item, reviewer_id=user, reviewer_role="owner", settings=get_settings())
    )

    assert outcome == {"sent": False, "reason": "send_disabled"}
    with session() as s:
        count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert count == 0


def test_send_approved_reply_demo_role_forbidden(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    _enable_send(monkeypatch)
    user = _make_user()
    item = _drafted_reply_item(user, monkeypatch)
    _stub_send(monkeypatch, MailSendResult(backend="nova", status="sent"))

    outcome = asyncio.run(
        send_approved_reply(item, reviewer_id=user, reviewer_role="demo", settings=get_settings())
    )

    assert outcome == {"sent": False, "reason": "role_forbidden"}


def test_send_approved_reply_idempotent(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    _enable_send(monkeypatch)
    user = _make_user()
    item = _drafted_reply_item(user, monkeypatch)
    _stub_send(monkeypatch, MailSendResult(backend="nova", status="sent"))

    first = asyncio.run(
        send_approved_reply(item, reviewer_id=user, reviewer_role="owner", settings=get_settings())
    )
    second = asyncio.run(
        send_approved_reply(item, reviewer_id=user, reviewer_role="owner", settings=get_settings())
    )

    assert first["sent"] is True
    assert second == {"sent": False, "reason": "already_sent"}
    with session() as s:
        count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert count == 1


def test_send_approved_reply_rate_limited(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    _enable_send(monkeypatch)
    user = _make_user()
    # Pre-existing reply to the same recipient (different work) within the hour.
    prior = _drafted_reply_item(user, monkeypatch)
    _stub_send(monkeypatch, MailSendResult(backend="nova", status="sent"))
    asyncio.run(
        send_approved_reply(prior, reviewer_id=user, reviewer_role="owner", settings=get_settings())
    )

    second_item = _drafted_reply_item(user, monkeypatch)
    outcome = asyncio.run(
        send_approved_reply(
            second_item, reviewer_id=user, reviewer_role="owner", settings=get_settings()
        )
    )

    assert outcome == {"sent": False, "reason": "rate_limited"}
    with session() as s:
        count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert count == 1


# --- ingest flow ----------------------------------------------------------------


def _register_mailbox_owner(email: str = "owner01@nova.example") -> uuid.UUID:
    """Bind a mailbox address to a real auth_identity owner.

    The individual-mailbox boundary (docs/p6-mail-individual-scope.md) refuses to
    ingest under the service user, so the ingest flow needs a resolvable owner.
    """
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Mailbox Owner"))
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


def test_ingest_inbound_creates_reply_work_item(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    owner = _register_mailbox_owner()
    result = ingest_mail(_inbound(message_id="<flow-1@example.com>"), get_settings())

    assert result.ingested is True
    assert result.scope == "personal"
    assert result.work_id is not None
    # Individual-mailbox reply draft is owner-bound: visible to the owner ...
    item = get_agent_work_item(owner, result.work_id)
    assert item is not None
    assert item.owner_id == owner
    assert item.action_family == "email_send"
    assert item.state == "draft_for_review"
    assert item.source_kind == "mail_reply"
    assert isinstance(item.payload.get("reply_context"), dict)
    # ... and NOT to another company user.
    other = _make_user()
    assert get_agent_work_item(other, result.work_id) is None


def test_ingest_with_flag_off_creates_no_reply_item(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    monkeypatch.setattr(get_settings(), "mail_reply_draft_enabled", False)
    _register_mailbox_owner()
    result = ingest_mail(_inbound(message_id="<flow-2@example.com>"), get_settings())

    assert result.ingested is True
    assert result.work_id is None
    with session() as s:
        count = s.execute(
            select(func.count())
            .select_from(agent_work_items)
            .where(agent_work_items.c.source_kind == "mail_reply")
        ).scalar_one()
    assert count == 0


# --- decide path triggers send --------------------------------------------------


def test_decide_approve_reply_triggers_send(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    _enable_send(monkeypatch)
    owner = uuid.UUID(DEMO_USER)
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Owner"))
        s.commit()
    candidate = build_reply_candidate(_inbound(), owner, get_settings())
    item = persist_reply_candidate(owner, candidate, settings=get_settings())
    monkeypatch.setattr(service, "get_chat_model_for", lambda _task: _StubChat("승인 본문"))
    ensure_reply_draft_for_work_item(owner, item)
    seen = _stub_send(
        monkeypatch, MailSendResult(backend="nova", status="sent", provider_message_id="re_1")
    )

    app.dependency_overrides[agent_work_routes.get_current_user] = _owner_override()
    try:
        res = client.post(f"/agent-work/{item.work_id}/decision", json={"decision": "approve"})
    finally:
        app.dependency_overrides.pop(agent_work_routes.get_current_user, None)

    assert res.status_code == 200
    body = res.json()
    assert body["item"]["state"] == "resolved"
    assert body["item"]["payload"]["reply_send"]["sent"] is True
    assert "request" in seen
    with session() as s:
        row = s.execute(
            select(email_send_log).where(email_send_log.c.work_id == item.work_id)
        ).first()
    assert row is not None
    assert row._mapping["origin"] == "agent_reply"


def test_decide_approve_reply_without_prior_get_still_sends(clean, monkeypatch):
    """Approving before any GET (so the lazy draft never ran) must still
    generate the body while draft_for_review and send — not resolve empty."""
    _company_reply_enabled(monkeypatch)
    _enable_send(monkeypatch)
    owner = uuid.UUID(DEMO_USER)
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Owner"))
        s.commit()
    candidate = build_reply_candidate(_inbound(), owner, get_settings())
    item = persist_reply_candidate(owner, candidate, settings=get_settings())
    # NOTE: no ensure_reply_draft_for_work_item / GET here — the route must do it.
    monkeypatch.setattr(service, "get_chat_model_for", lambda _task: _StubChat("승인 본문"))
    seen = _stub_send(
        monkeypatch, MailSendResult(backend="nova", status="sent", provider_message_id="re_2")
    )

    app.dependency_overrides[agent_work_routes.get_current_user] = _owner_override()
    try:
        res = client.post(f"/agent-work/{item.work_id}/decision", json={"decision": "approve"})
    finally:
        app.dependency_overrides.pop(agent_work_routes.get_current_user, None)

    assert res.status_code == 200
    body = res.json()
    assert body["item"]["state"] == "resolved"
    assert body["item"]["payload"]["reply_send"]["sent"] is True
    assert "request" in seen
    assert seen["request"].text == "승인 본문"


def test_assistant_command_email_template_approve_does_not_send(clean, monkeypatch):
    # Regression guard: an assistant_command email-template item (no reply_context)
    # must NOT trigger a real send on approve.
    _company_reply_enabled(monkeypatch)
    _enable_send(monkeypatch)
    owner = uuid.UUID(DEMO_USER)
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Owner"))
        s.commit()
    work_id = uuid.uuid4()
    settings = get_settings()
    with session() as s:
        s.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=settings.node_id,
                node_kind=settings.node_kind,
                owner_id=None,
                source_kind="assistant_command",
                source_ref_id=f"email-command-{work_id}",
                action_family="email_send",
                title="Email draft",
                payload={
                    "recipient_hint": "client@partner.com",
                    "email_draft": {
                        "recipient_hint": "client@partner.com",
                        "subject_hint": "Re: hi",
                        "body_template": "본문",
                    },
                },
                state="draft_for_review",
                policy_outcome="draft_for_review",
                policy_reason="email send draft",
                reason_codes=["email_draft_first"],
                evidence=[],
                created_by=owner,
            )
        )
        s.commit()
    _stub_send(monkeypatch, MailSendResult(backend="nova", status="sent"))

    app.dependency_overrides[agent_work_routes.get_current_user] = _owner_override()
    try:
        res = client.post(f"/agent-work/{work_id}/decision", json={"decision": "approve"})
    finally:
        app.dependency_overrides.pop(agent_work_routes.get_current_user, None)

    assert res.status_code == 200
    assert "reply_send" not in res.json()["item"]["payload"]
    with session() as s:
        count = s.execute(select(func.count()).select_from(email_send_log)).scalar_one()
    assert count == 0
