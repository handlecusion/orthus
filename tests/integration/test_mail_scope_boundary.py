"""Mail ingest scope/owner boundary (docs/p6-mail-individual-scope.md).

An individual employee work mailbox (e.g. owner01@nova.example) must ingest as
scope=personal bound to that owner (owner-only readable); only an explicitly
configured shared mailbox stays scope=company. An individual mailbox whose owner
does not resolve to a real auth_identity is refused fail-closed.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, insert, select

from orthus.mail.ingest import (
    MailIngestConfigError,
    ingest_mail,
    ingest_scope_and_owner,
)
from orthus.schemas.canonical import MailIngestRequest
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import auth_identities, documents, users


def _register_owner(email: str) -> uuid.UUID:
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


def test_individual_mailbox_is_personal_owner_bound(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_nova_kind", "individual")
    owner = _register_owner("owner01@nova.example")

    scope, owner_user_id = ingest_scope_and_owner("nova", "owner01@nova.example", settings)

    assert scope == "personal"
    assert owner_user_id == owner


def test_shared_mailbox_is_company(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_nova_kind", "shared")
    # A resolvable owner exists, but a shared mailbox stays company-scope regardless.
    _register_owner("hello@nova.example")

    scope, _owner = ingest_scope_and_owner("nova", "hello@nova.example", settings)

    assert scope == "company"


def test_individual_mailbox_unresolvable_owner_raises(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_nova_kind", "individual")
    # No auth_identity for this address -> resolve_mail_owner_user_id falls back to
    # the service user, which is refused for an individual mailbox.

    with pytest.raises(MailIngestConfigError):
        ingest_scope_and_owner("nova", "unknown@nova.example", settings)


def test_gmail_unchanged_is_personal(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    owner = _register_owner("ys@gmail.com")

    scope, owner_user_id = ingest_scope_and_owner("gmail", "ys@gmail.com", settings)

    assert scope == "personal"
    assert owner_user_id == owner


def test_ingest_individual_mailbox_unresolvable_owner_writes_no_document(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_nova_kind", "individual")
    monkeypatch.setattr(settings, "mail_reply_draft_enabled", False)

    with pytest.raises(MailIngestConfigError):
        ingest_mail(_inbound(owner_addr="unknown@nova.example"), settings)

    with session() as s:
        count = s.execute(
            select(func.count()).select_from(documents).where(documents.c.source == "mail_nova")
        ).scalar_one()
    assert count == 0


def test_ingest_individual_mailbox_writes_personal_owner_document(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_nova_kind", "individual")
    monkeypatch.setattr(settings, "mail_reply_draft_enabled", False)
    owner = _register_owner("owner01@nova.example")

    result = ingest_mail(_inbound(), settings)

    assert result.ingested is True
    assert result.scope == "personal"
    with session() as s:
        row = s.execute(
            select(documents.c.user_id, documents.c.scope).where(
                documents.c.doc_id == result.doc_id
            )
        ).first()
    assert row is not None
    assert row.scope == "personal"
    assert row.user_id == owner
