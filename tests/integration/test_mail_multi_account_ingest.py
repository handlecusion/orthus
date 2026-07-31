"""P6.7.3 per-account ingest scope routing.

`docs/p6-unified-mail.md` §12.2: a mail account row's `ingest_scope` decides the
ingest (scope, owner_id):
  - owner  -> scope=personal + owner_id=registrant (owner-only wiki; requires
    ORTHUS_OWNER_SCOPE_ENABLED).
  - company -> scope=company + owner_id=NULL (company-wide).
  - owner + ORTHUS_OWNER_SCOPE_ENABLED off -> fail-closed (skipped, NOT company).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import insert, select

from orthus.connectors.account_config import configure_connector_account
from orthus.db import session
from orthus.mail.backends import MailIngestScopeError
from orthus.mail.ingest import ingest_mail, resolve_ingest_route
from orthus.schemas.canonical import MailIngestRequest
from orthus.settings import get_settings
from orthus.tables import connector_accounts, documents, users, wiki_pages


def _register_mail_account(
    slug: str,
    owner_id: uuid.UUID,
    *,
    owner_addr: str,
    secret_key: str,
    ingest_scope: str,
) -> dict:
    settings = get_settings()
    # Registering a personal connector on the company node requires owner-scope on
    # (config gate). Restore the caller's flag so ingest-time assertions still see
    # the value the test set.
    prior = settings.owner_scope_enabled
    settings.owner_scope_enabled = True
    try:
        configure_connector_account(
            slug,
            owner_id,
            input_settings={
                "base_url": "https://mail.test",
                "owner_addr": owner_addr,
                "ingest_scope": ingest_scope,
            },
            input_secrets={secret_key: "row-secret"},
            account_kind="personal",
            owner_email=owner_addr,
        )
    finally:
        settings.owner_scope_enabled = prior
    with session() as s:
        row = s.execute(
            select(connector_accounts).where(
                connector_accounts.c.connector_slug == slug,
                connector_accounts.c.owner_id == owner_id,
            )
        ).first()
    return dict(row._mapping)


def _payload(backend: str, owner_addr: str, *, external_id: str) -> MailIngestRequest:
    return MailIngestRequest(
        backend=backend,
        owner_addr=owner_addr,
        external_id=external_id,
        message_id=f"<{external_id}@{owner_addr.split('@')[-1]}>",
        direction="inbound",
        from_addr="Lead <lead@example.com>",
        to_addr=[owner_addr],
        subject="P6.7.3 scope",
        body_text="Body with bob@example.com kept unredacted on the mail path.",
    )


def _other_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="Other"))
        s.commit()
    return uid


def test_owner_scope_writes_personal_with_owner_id(clean, user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_multi_account_enabled", True)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "mail_ingest_service_user_id", str(user_id))

    account = _register_mail_account(
        "mail_nova",
        user_id,
        owner_addr="me@nova.example",
        secret_key="api_key",
        ingest_scope="owner",
    )
    payload = _payload("nova", "me@nova.example", external_id="own-1")

    scope, owner = resolve_ingest_route(payload, settings, account=account)
    assert (scope, owner) == ("personal", user_id)

    result = ingest_mail(payload, settings, account=account)
    assert result.ingested is True
    assert result.scope == "personal"

    with session() as s:
        doc = s.execute(
            select(documents.c.scope, documents.c.user_id).where(documents.c.source == "mail_nova")
        ).first()
        pages = s.execute(
            select(wiki_pages.c.scope, wiki_pages.c.owner_id).where(
                wiki_pages.c.scope == "personal"
            )
        ).all()
        # Another user must not see any company-wide (owner_id NULL) row from this mail.
        company_pages = s.execute(
            select(wiki_pages.c.page_id).where(wiki_pages.c.owner_id.is_(None))
        ).all()

    assert doc.scope == "personal"
    assert doc.user_id == user_id
    assert pages, "owner-scope mail must author personal wiki pages"
    assert all(owner_id == user_id for _scope, owner_id in pages)
    assert not company_pages, "owner-scope mail must not write company-wide wiki pages"


def test_company_scope_writes_company_with_null_owner(clean, user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_multi_account_enabled", True)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "mail_ingest_service_user_id", str(user_id))

    account = _register_mail_account(
        "mail_acme",
        user_id,
        owner_addr="me@acme.example",
        secret_key="api_token",
        ingest_scope="company",
    )
    payload = _payload("acme", "me@acme.example", external_id="co-1")

    scope, owner = resolve_ingest_route(payload, settings, account=account)
    assert scope == "company"

    result = ingest_mail(payload, settings, account=account)
    assert result.ingested is True
    assert result.scope == "company"

    with session() as s:
        doc = s.execute(
            select(documents.c.scope).where(documents.c.source == "mail_acme")
        ).first()
        pages = s.execute(select(wiki_pages.c.scope, wiki_pages.c.owner_id)).all()

    assert doc.scope == "company"
    assert pages, "company-scope mail must author company wiki pages"
    assert all(scope == "company" and owner_id is None for scope, owner_id in pages)


def test_owner_scope_fails_closed_when_owner_scope_disabled(clean, user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_multi_account_enabled", True)
    # Owner scope requested by the row but the node flag is OFF -> must fail closed.
    monkeypatch.setattr(settings, "owner_scope_enabled", False)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "mail_ingest_service_user_id", str(user_id))

    account = _register_mail_account(
        "mail_nova",
        user_id,
        owner_addr="me@nova.example",
        secret_key="api_key",
        ingest_scope="owner",
    )
    payload = _payload("nova", "me@nova.example", external_id="fc-1")

    with pytest.raises(MailIngestScopeError):
        resolve_ingest_route(payload, settings, account=account)
    with pytest.raises(MailIngestScopeError):
        ingest_mail(payload, settings, account=account)

    # Nothing ingested at all (not silently downgraded to company-scope).
    with session() as s:
        docs = s.execute(select(documents.c.doc_id)).all()
    assert not docs


def test_account_owner_id_overrides_owner_addr_for_owner_scope(clean, user_id, monkeypatch):
    """owner-scope owner_id is the registrant (account.owner_id), not owner_addr lookup."""
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_multi_account_enabled", True)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    # Service user is someone else; owner-scope must still bind to the registrant.
    other = _other_user()
    monkeypatch.setattr(settings, "mail_ingest_service_user_id", str(other))

    account = _register_mail_account(
        "mail_nova",
        user_id,
        owner_addr="me@nova.example",
        secret_key="api_key",
        ingest_scope="owner",
    )
    payload = _payload("nova", "me@nova.example", external_id="own-2")

    scope, owner = resolve_ingest_route(payload, settings, account=account)
    assert (scope, owner) == ("personal", user_id)
