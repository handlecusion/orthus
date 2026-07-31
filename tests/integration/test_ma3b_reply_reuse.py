"""MA.3b — /ask context_mail_id + decompose reply-draft reuse (D path).

The decompose action-intake leaf, when the request carries a context_mail_id and a
sub-part is an email_send action, reuses the P7.1 reply draft already created for that
mail at ingest (find_reply_draft_for_mail) instead of queuing a fresh intake. Owner-scope
is fail-closed. When no draft exists it falls back to queue_agent_work.
"""

from __future__ import annotations

import uuid

from sqlalchemy import insert

from orthus.agentwork.service import find_reply_draft_for_mail, persist_reply_candidate
from orthus.db import session
from orthus.mail.reply import build_reply_candidate
from orthus.router.decompose import fan_out
from orthus.schemas.canonical import MailIngestRequest, SubQuestion
from orthus.settings import get_settings
from orthus.tables import auth_identities, users

REPLY_TEXT = "이 메일에 답장 보내줘"


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


def _make_user(email: str | None = None) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="User"))
        if email:
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


def _persist_reply_draft(owner: uuid.UUID, *, owner_scoped: bool = False, **mail):
    candidate = build_reply_candidate(_inbound(**mail), owner, get_settings())
    assert candidate is not None
    item = persist_reply_candidate(
        owner, candidate, settings=get_settings(), owner_scoped=owner_scoped
    )
    assert item is not None
    return item


def _action_leaf(user: uuid.UUID, context_mail_id: str | None):
    """Run a single email_send action sub-question through the decompose fan-out."""
    sq = SubQuestion(id=uuid.uuid4(), text=REPLY_TEXT, scope="company")
    answers = fan_out(
        [sq],
        user_id=user,
        scope="company",
        project=None,
        chat_model=None,
        context_wiki_slug=None,
        actor_role="owner",
        allow_agentic_in_leaf=False,
        stream_key=None,
        max_concurrency=1,
        leaf_timeout_sec=30,
        context_mail_id=context_mail_id,
    )
    assert len(answers) == 1
    return answers[0]


# --- find_reply_draft_for_mail --------------------------------------------------


def test_find_reply_draft_returns_existing(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    owner = _make_user()
    item = _persist_reply_draft(owner)

    found = find_reply_draft_for_mail(owner, item.source_ref_id)
    assert found is not None
    assert found.work_id == item.work_id
    assert found.source_kind == "mail_reply"


def test_find_reply_draft_none_when_absent(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    owner = _make_user()
    assert find_reply_draft_for_mail(owner, "mail:nova:<nope@example.com>") is None


def test_find_reply_draft_owner_scoped_hidden_from_other_user(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    owner = _make_user()
    item = _persist_reply_draft(owner, owner_scoped=True)

    # owner sees their own owner-bound draft ...
    assert find_reply_draft_for_mail(owner, item.source_ref_id) is not None
    # ... another company user must NOT (individual-mailbox boundary).
    other = _make_user()
    assert find_reply_draft_for_mail(other, item.source_ref_id) is None


# --- decompose leaf D path ------------------------------------------------------


def test_leaf_reuses_existing_reply_draft(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    owner = _make_user()
    item = _persist_reply_draft(owner)

    sa = _action_leaf(owner, item.source_ref_id)
    assert sa.error is None
    assert sa.grounded is False
    # D path: links the existing P7.1 draft rather than queuing a new intake.
    assert sa.agent_work_id == item.work_id


def test_leaf_falls_back_when_no_draft_for_mail(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    owner = _make_user()
    # A reply draft exists for mail A, but the request is about mail B (no draft).
    draft_a = _persist_reply_draft(owner, message_id="<mail-a@example.com>")

    sa = _action_leaf(owner, "mail:nova:<mail-b-no-draft@example.com>")
    assert sa.error is None
    # Did NOT reuse mail A's draft; fell back to queue_agent_work (a different item).
    assert sa.agent_work_id != draft_a.work_id


def test_leaf_without_context_mail_id_does_not_reuse(clean, monkeypatch):
    _company_reply_enabled(monkeypatch)
    owner = _make_user()
    item = _persist_reply_draft(owner)

    # No context_mail_id → the leaf cannot reuse, even though a draft exists.
    sa = _action_leaf(owner, None)
    assert sa.error is None
    assert sa.agent_work_id != item.work_id
