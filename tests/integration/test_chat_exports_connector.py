"""C5 personal chat export connector."""

from __future__ import annotations

import json
import zipfile
from uuid import UUID

import pytest
from sqlalchemy import func, select

from orthus.connectors.chat_exports import ChatExportsConnector
from orthus.connectors.chat_exports_account import ensure_chat_exports_account
from orthus.connectors.registry import get_connector_provider, register_default_connector_providers
from orthus.connectors.runner import run_connector_account_sync
from orthus.connectors.state import get_sync_state
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.settings import get_settings
from orthus.tables import connector_accounts, connector_items, documents


def _chatgpt_export() -> list[dict]:
    return [
        {
            "id": "conv-1",
            "title": "Connector planning",
            "mapping": {
                "u": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": [
                                "Import this export. token=sk-abcdefghijklmnopqrstuvwxyz user@example.com"
                            ],
                        },
                        "create_time": 1770000000,
                    }
                },
                "a": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["Use connector registry."]},
                    }
                },
                "tool": {
                    "message": {
                        "author": {"role": "tool"},
                        "content": {"content_type": "text", "parts": ["tool output skip"]},
                    }
                },
            },
        }
    ]


def test_chat_exports_connector_reads_json_and_filters_payloads(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    export_path = root / "conversations.json"
    export_path.write_text(json.dumps(_chatgpt_export(), ensure_ascii=False), encoding="utf-8")

    docs = list(ChatExportsConnector([root]).iter_documents(None))

    assert len(docs) == 1
    doc = docs[0]
    assert doc.title == "Chat export: Connector planning"
    assert doc.source == "chat_exports"
    assert doc.source_external_id is not None
    assert doc.source_external_id.startswith("chat-export:")
    assert str(root) not in doc.markdown
    assert "Path: conversations.json" in doc.markdown
    assert "Import this export." in doc.markdown
    assert "[REDACTED_SECRET]" in doc.markdown
    assert "u***@example.com" in doc.markdown
    assert "Use connector registry." in doc.markdown
    assert "tool output skip" not in doc.markdown


def test_chat_exports_connector_reads_zip_with_claude_shape(tmp_path):
    root = tmp_path / "exports"
    root.mkdir()
    export_path = root / "claude-export.zip"
    payload = {
        "conversations": [
            {
                "uuid": "claude-1",
                "name": "Claude export",
                "chat_messages": [
                    {"sender": "human", "text": "Claude exported user message"},
                    {"sender": "assistant", "text": "Claude exported assistant message"},
                    {"sender": "system", "text": "system skip"},
                ],
            }
        ]
    }
    with zipfile.ZipFile(export_path, "w") as zf:
        zf.writestr("conversations.json", json.dumps(payload, ensure_ascii=False))

    docs = list(ChatExportsConnector([root]).iter_documents(None))

    assert len(docs) == 1
    assert docs[0].title == "Chat export: Claude export"
    assert "Archive member: conversations.json" in docs[0].markdown
    assert "Claude exported user message" in docs[0].markdown
    assert "Claude exported assistant message" in docs[0].markdown
    assert "system skip" not in docs[0].markdown


def test_ensure_chat_exports_account_is_personal_only(user_id: UUID, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_chat_exports_roots", str(tmp_path))
    monkeypatch.setattr(settings, "conn_chat_exports_max_bytes", 4096)
    monkeypatch.setattr(settings, "conn_chat_exports_max_messages", 25)
    monkeypatch.setattr(settings, "conn_chat_exports_max_message_chars", 512)

    account_id = ensure_chat_exports_account(user_id, settings)

    with session() as s:
        row = s.execute(
            select(connector_accounts).where(connector_accounts.c.account_id == account_id)
        ).one()

    assert row.connector_slug == "chat_exports"
    assert row.account_kind == "personal"
    assert row.scope == "personal"
    assert row.owner_id == user_id
    assert row.settings_redacted["root_count"] == 1
    assert "exports" not in row.settings_redacted

    monkeypatch.setattr(settings, "node_kind", "company")
    with pytest.raises(ValueError, match="personal-node only"):
        ensure_chat_exports_account(user_id, settings)


def test_chat_exports_runner_imports_idempotently(user_id: UUID, tmp_path, monkeypatch):
    root = tmp_path / "exports"
    root.mkdir()
    (root / "conversations.json").write_text(
        json.dumps(_chatgpt_export(), ensure_ascii=False),
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "embedding", "mock")
    monkeypatch.setattr(settings, "llm", "mock")
    monkeypatch.setattr(settings, "conn_chat_exports_roots", str(root))
    monkeypatch.setattr(settings, "conn_chat_exports_max_bytes", 4096)
    monkeypatch.setattr(settings, "conn_chat_exports_max_messages", 25)
    monkeypatch.setattr(settings, "conn_chat_exports_max_message_chars", 512)
    register_default_connector_providers(replace=True)

    provider = get_connector_provider("chat_exports")
    assert provider is not None
    assert provider.manifest.supports_account_kind("personal")
    assert not provider.manifest.supports_account_kind("company")

    account_id = ensure_chat_exports_account(user_id, settings)
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

    assert doc.source == "chat_exports"
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
