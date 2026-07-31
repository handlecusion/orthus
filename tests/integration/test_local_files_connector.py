"""C3 personal local-files connector."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from orthus.connectors.local_files import (
    LocalFilesConnector,
    parse_local_file_extensions,
    parse_local_file_roots,
)
from orthus.connectors.local_files_account import ensure_local_files_account
from orthus.connectors.registry import (
    get_connector_provider,
    register_default_connector_providers,
)
from orthus.connectors.runner import run_connector_account_sync
from orthus.connectors.state import get_sync_state
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.settings import get_settings
from orthus.tables import connector_accounts, connector_items, documents


def test_local_files_connector_filters_and_hides_absolute_paths(tmp_path):
    root = tmp_path / "imports"
    root.mkdir()
    notes = root / "notes"
    notes.mkdir()
    doc_path = notes / "alpha.md"
    doc_path.write_text("Alpha local note", encoding="utf-8")
    (root / "skip.pdf").write_text("not allowed", encoding="utf-8")
    (root / "binary.md").write_bytes(b"hello\x00world")
    hidden = root / ".git"
    hidden.mkdir()
    (hidden / "secret.md").write_text("hidden", encoding="utf-8")

    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (root / "outside.md").symlink_to(outside)

    connector = LocalFilesConnector(
        [root],
        extensions=parse_local_file_extensions(".md,.txt"),
        max_bytes=1000,
    )
    docs = list(connector.iter_documents(None))

    assert len(docs) == 1
    doc = docs[0]
    assert doc.title == "notes/alpha.md"
    assert doc.source == "local_files"
    assert doc.source_external_id is not None
    assert doc.source_external_id.startswith("path-sha256:")
    assert str(root) not in doc.markdown
    assert "Path: notes/alpha.md" in doc.markdown
    assert "Alpha local note" in doc.markdown

    future = datetime.now(UTC) + timedelta(seconds=1)
    assert list(connector.iter_documents(future)) == []


def test_parse_local_file_roots_expands_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setenv("ORTHUS_TEST_ROOT", str(tmp_path))
    roots = parse_local_file_roots("$ORTHUS_TEST_ROOT, $ORTHUS_TEST_ROOT")
    assert roots == (tmp_path.resolve(),)


def test_ensure_local_files_account_is_personal_only(user_id: UUID, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_local_files_roots", str(tmp_path))
    monkeypatch.setattr(settings, "conn_local_files_extensions", ".md,.txt")
    monkeypatch.setattr(settings, "conn_local_files_max_bytes", 4096)

    account_id = ensure_local_files_account(user_id, settings)

    with session() as s:
        row = s.execute(
            select(connector_accounts).where(connector_accounts.c.account_id == account_id)
        ).one()

    assert row.connector_slug == "local_files"
    assert row.account_kind == "personal"
    assert row.scope == "personal"
    assert row.owner_id == user_id
    assert row.node_id == "personal-a"
    assert row.auth_mode == "local_path"
    assert row.settings_redacted["root_count"] == 1
    assert row.settings_redacted["extensions"] == [".md", ".txt"]
    assert "imports" not in row.settings_redacted

    monkeypatch.setattr(settings, "node_kind", "company")
    with pytest.raises(ValueError, match="personal-node only"):
        ensure_local_files_account(user_id, settings)


def test_local_files_runner_imports_personal_docs_idempotently(
    user_id: UUID,
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "local-files"
    root.mkdir()
    (root / "daily.md").write_text("Daily note for personal wiki", encoding="utf-8")

    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "embedding", "mock")
    monkeypatch.setattr(settings, "llm", "mock")
    monkeypatch.setattr(settings, "conn_local_files_roots", str(root))
    monkeypatch.setattr(settings, "conn_local_files_extensions", ".md")
    monkeypatch.setattr(settings, "conn_local_files_max_bytes", 4096)
    register_default_connector_providers(replace=True)

    provider = get_connector_provider("local_files")
    assert provider is not None
    assert provider.manifest.supports_account_kind("personal")
    assert not provider.manifest.supports_account_kind("company")

    account_id = ensure_local_files_account(user_id, settings)
    first = run_connector_account_sync(account_id, user_id, chat_model=MockChat())

    assert first.status == "succeeded"
    assert first.report is not None
    assert first.report.created == 1
    assert first.report.errors == 0

    with session() as s:
        doc = s.execute(
            select(
                documents.c.doc_id,
                documents.c.source,
                documents.c.scope,
                documents.c.source_account_id,
                documents.c.title,
            )
        ).one()
        item_count = s.execute(select(func.count()).select_from(connector_items)).scalar_one()

    assert doc.source == "local_files"
    assert doc.scope == "personal"
    assert doc.source_account_id == account_id
    assert doc.title == "daily.md"
    assert item_count == 1

    state = get_sync_state(account_id)
    assert state is not None
    assert state.last_sync_at is not None
    assert state.last_error is None

    second = run_connector_account_sync(account_id, user_id, chat_model=MockChat())
    assert second.status == "succeeded"
    assert second.report is not None
    assert len(second.report.doc_ids) == 0

    with session() as s:
        doc_count = s.execute(select(func.count()).select_from(documents)).scalar_one()
    assert doc_count == 1
