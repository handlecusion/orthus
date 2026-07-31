"""C7 personal email export connector."""

from __future__ import annotations

import mailbox
from email.message import EmailMessage
from uuid import UUID

import pytest
from sqlalchemy import func, select

from orthus.connectors.email_exports import EmailExportsConnector
from orthus.connectors.email_exports_account import ensure_email_exports_account
from orthus.connectors.registry import get_connector_provider, register_default_connector_providers
from orthus.connectors.runner import run_connector_account_sync
from orthus.connectors.state import get_sync_state
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.settings import get_settings
from orthus.tables import connector_accounts, connector_items, documents


def _email_message(
    *,
    subject: str = "Connector email",
    body: str = "Email body with user@example.com token=sk-abcdefghijklmnopqrstuvwxyz",
) -> EmailMessage:
    msg = EmailMessage()
    msg["Message-ID"] = "<email-1@example.com>"
    msg["Subject"] = subject
    msg["From"] = "User <user@example.com>"
    msg["To"] = "Assistant <assistant@example.com>"
    msg["Date"] = "Sun, 31 May 2026 09:00:00 +0900"
    msg.set_content(body)
    msg.add_attachment(
        b"attachment secret should not be ingested",
        maintype="text",
        subtype="plain",
        filename="secret.txt",
    )
    return msg


def test_email_exports_connector_reads_eml_and_filters_attachments(tmp_path):
    root = tmp_path / "email"
    root.mkdir()
    (root / "message.eml").write_bytes(_email_message().as_bytes())

    docs = list(EmailExportsConnector([root]).iter_documents(None))

    assert len(docs) == 1
    doc = docs[0]
    assert doc.title == "Email: Connector email"
    assert doc.source == "email_exports"
    assert doc.source_external_id is not None
    assert doc.source_external_id.startswith("email-export:")
    assert str(root) not in doc.markdown
    assert "Path: message.eml" in doc.markdown
    assert "Email body with" in doc.markdown
    assert "u***@example.com" in doc.markdown
    assert "[REDACTED_SECRET]" in doc.markdown
    assert "attachment secret" not in doc.markdown


def test_email_exports_connector_reads_mbox(tmp_path):
    root = tmp_path / "email"
    root.mkdir()
    mbox_path = root / "mailbox.mbox"
    box = mailbox.mbox(mbox_path)
    try:
        box.add(_email_message(subject="Mbox subject", body="Mbox plain body"))
        box.flush()
    finally:
        box.close()

    docs = list(EmailExportsConnector([root]).iter_documents(None))

    assert len(docs) == 1
    assert docs[0].title == "Email: Mbox subject"
    assert "Path: mailbox.mbox" in docs[0].markdown
    assert "Mbox plain body" in docs[0].markdown


def test_ensure_email_exports_account_is_personal_only(user_id: UUID, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_email_exports_roots", str(tmp_path))
    monkeypatch.setattr(settings, "conn_email_exports_max_bytes", 4096)
    monkeypatch.setattr(settings, "conn_email_exports_max_body_chars", 512)

    account_id = ensure_email_exports_account(user_id, settings)

    with session() as s:
        row = s.execute(
            select(connector_accounts).where(connector_accounts.c.account_id == account_id)
        ).one()

    assert row.connector_slug == "email_exports"
    assert row.account_kind == "personal"
    assert row.scope == "personal"
    assert row.owner_id == user_id
    assert row.settings_redacted["root_count"] == 1
    assert "email" not in row.settings_redacted

    monkeypatch.setattr(settings, "node_kind", "company")
    with pytest.raises(ValueError, match="personal-node only"):
        ensure_email_exports_account(user_id, settings)


def test_email_exports_runner_imports_idempotently(user_id: UUID, tmp_path, monkeypatch):
    root = tmp_path / "email"
    root.mkdir()
    (root / "message.eml").write_bytes(_email_message().as_bytes())

    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "embedding", "mock")
    monkeypatch.setattr(settings, "llm", "mock")
    monkeypatch.setattr(settings, "conn_email_exports_roots", str(root))
    monkeypatch.setattr(settings, "conn_email_exports_max_bytes", 4096)
    monkeypatch.setattr(settings, "conn_email_exports_max_body_chars", 512)
    register_default_connector_providers(replace=True)

    provider = get_connector_provider("email_exports")
    assert provider is not None
    assert provider.manifest.supports_account_kind("personal")
    assert not provider.manifest.supports_account_kind("company")

    account_id = ensure_email_exports_account(user_id, settings)
    first = run_connector_account_sync(account_id, user_id, chat_model=MockChat())

    assert first.status == "succeeded"
    assert first.report is not None
    assert first.report.created == 1
    assert first.report.errors == 0

    with session() as s:
        doc = s.execute(
            select(documents.c.source, documents.c.scope, documents.c.source_account_id)
        ).one()
        item_count = s.execute(select(func.count()).select_from(connector_items)).scalar_one()

    assert doc.source == "email_exports"
    assert doc.scope == "personal"
    assert doc.source_account_id == account_id
    assert item_count == 1

    state = get_sync_state(account_id)
    assert state is not None
    assert state.last_sync_at is not None
    assert state.last_error is None

    second = run_connector_account_sync(account_id, user_id, chat_model=MockChat())
    assert second.status == "succeeded"
    assert second.report is not None
    assert len(second.report.doc_ids) == 0
