"""C4 personal AI session connectors."""

from __future__ import annotations

import json
import os
from uuid import UUID

import pytest
from sqlalchemy import func, select

from orthus.connectors.ai_sessions import AiSessionsConnector
from orthus.connectors.ai_sessions_account import ensure_ai_sessions_account
from orthus.connectors.registry import get_connector_provider, register_default_connector_providers
from orthus.connectors.runner import run_connector_account_sync
from orthus.connectors.state import get_sync_state
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.settings import get_settings
from orthus.tables import connector_accounts, connector_items, documents


def _write_jsonl(path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_ai_sessions_connector_filters_tools_and_hides_absolute_paths(tmp_path):
    root = tmp_path / "codex"
    root.mkdir()
    session_path = root / "session.jsonl"
    _write_jsonl(
        session_path,
        [
            {
                "timestamp": "2026-05-29T01:00:00Z",
                "cwd": "<local>",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Build connector. token=sk-abcdefghijklmnopqrstuvwxyz user@example.com",
                        },
                        {"type": "tool_result", "content": "FULL FILE SHOULD NOT APPEAR"},
                    ],
                },
            },
            {
                "timestamp": "2026-05-29T01:01:00Z",
                "message": {"role": "assistant", "content": "Use connector substrate."},
            },
            {"message": {"role": "tool", "content": "tool output skip"}},
        ],
    )

    connector = AiSessionsConnector(source="codex_sessions", label="Codex Sessions", roots=[root])
    docs = list(connector.iter_documents(None))

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source == "codex_sessions"
    assert doc.source_external_id is not None
    assert doc.source_external_id.startswith("path-sha256:")
    assert str(root) not in doc.markdown
    assert "<local>" not in doc.markdown
    assert "Path: session.jsonl" in doc.markdown
    assert "Build connector." in doc.markdown
    assert "[REDACTED_SECRET]" in doc.markdown
    assert "u***@example.com" in doc.markdown
    assert "Use connector substrate." in doc.markdown
    assert "FULL FILE SHOULD NOT APPEAR" not in doc.markdown
    assert "tool output skip" not in doc.markdown


def test_ai_sessions_connector_reads_json_messages_and_skips_missing_roots(tmp_path):
    root = tmp_path / "claude"
    root.mkdir()
    session_path = root / "session.json"
    session_path.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "human", "content": "Claude user request"},
                    {"role": "assistant", "content": [{"type": "text", "text": "Claude answer"}]},
                    {"role": "assistant", "content": [{"type": "tool_use", "text": "skip"}]},
                ]
            }
        ),
        encoding="utf-8",
    )

    connector = AiSessionsConnector(
        source="claude_sessions",
        label="Claude Sessions",
        roots=[tmp_path / "missing", root],
    )
    docs = list(connector.iter_documents(None))

    assert len(docs) == 1
    assert "Claude user request" in docs[0].markdown
    assert "Claude answer" in docs[0].markdown
    assert "skip" not in docs[0].markdown


def test_ai_sessions_connector_caps_files_newest_first(tmp_path):
    root = tmp_path / "claude"
    root.mkdir()
    for index in range(3):
        path = root / f"session-{index}.jsonl"
        _write_jsonl(path, [{"message": {"role": "user", "content": f"message {index}"}}])
        os.utime(path, (1_800_000_000 + index, 1_800_000_000 + index))

    connector = AiSessionsConnector(
        source="claude_sessions",
        label="Claude Sessions",
        roots=[root],
        max_files=2,
    )
    docs = list(connector.iter_documents(None))

    assert [doc.title for doc in docs] == [
        "Claude Sessions: session-2.jsonl",
        "Claude Sessions: session-1.jsonl",
    ]
    assert all("session-0" not in doc.markdown for doc in docs)


def test_ensure_ai_sessions_account_is_personal_only(user_id: UUID, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "conn_codex_sessions_roots", str(tmp_path))
    monkeypatch.setattr(settings, "conn_ai_sessions_max_bytes", 4096)
    monkeypatch.setattr(settings, "conn_ai_sessions_max_files", 10)
    monkeypatch.setattr(settings, "conn_ai_sessions_max_messages", 25)
    monkeypatch.setattr(settings, "conn_ai_sessions_max_message_chars", 512)

    account_id = ensure_ai_sessions_account("codex_sessions", user_id, settings)

    with session() as s:
        row = s.execute(
            select(connector_accounts).where(connector_accounts.c.account_id == account_id)
        ).one()

    assert row.connector_slug == "codex_sessions"
    assert row.account_kind == "personal"
    assert row.scope == "personal"
    assert row.owner_id == user_id
    assert row.settings_redacted["root_count"] == 1
    assert row.settings_redacted["max_files"] == 10
    assert "codex" not in row.settings_redacted

    monkeypatch.setattr(settings, "node_kind", "company")
    with pytest.raises(ValueError, match="personal-node only"):
        ensure_ai_sessions_account("codex_sessions", user_id, settings)


def test_ai_sessions_runner_imports_codex_and_claude_idempotently(
    user_id: UUID,
    tmp_path,
    monkeypatch,
):
    codex_root = tmp_path / "codex"
    claude_root = tmp_path / "claude"
    codex_root.mkdir()
    claude_root.mkdir()
    _write_jsonl(
        codex_root / "codex.jsonl",
        [{"message": {"role": "user", "content": "Codex session local wiki note"}}],
    )
    _write_jsonl(
        claude_root / "claude.jsonl",
        [{"message": {"role": "user", "content": "Claude session local wiki note"}}],
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "embedding", "mock")
    monkeypatch.setattr(settings, "llm", "mock")
    monkeypatch.setattr(settings, "conn_codex_sessions_roots", str(codex_root))
    monkeypatch.setattr(settings, "conn_claude_sessions_roots", str(claude_root))
    monkeypatch.setattr(settings, "conn_ai_sessions_max_bytes", 4096)
    monkeypatch.setattr(settings, "conn_ai_sessions_max_files", 10)
    monkeypatch.setattr(settings, "conn_ai_sessions_max_messages", 25)
    monkeypatch.setattr(settings, "conn_ai_sessions_max_message_chars", 512)
    register_default_connector_providers(replace=True)

    codex_provider = get_connector_provider("codex_sessions")
    claude_provider = get_connector_provider("claude_sessions")
    assert codex_provider is not None
    assert claude_provider is not None
    assert codex_provider.manifest.supports_account_kind("personal")
    assert not codex_provider.manifest.supports_account_kind("company")

    codex_account = ensure_ai_sessions_account("codex_sessions", user_id, settings)
    claude_account = ensure_ai_sessions_account("claude_sessions", user_id, settings)

    codex_first = run_connector_account_sync(codex_account, user_id, chat_model=MockChat())
    claude_first = run_connector_account_sync(claude_account, user_id, chat_model=MockChat())

    assert codex_first.status == "succeeded"
    assert claude_first.status == "succeeded"
    assert codex_first.report is not None and codex_first.report.created == 1
    assert claude_first.report is not None and claude_first.report.created == 1

    with session() as s:
        rows = s.execute(
            select(documents.c.source, documents.c.scope, documents.c.source_account_id)
            .select_from(documents)
            .order_by(documents.c.source)
        ).all()
        item_count = s.execute(select(func.count()).select_from(connector_items)).scalar_one()

    assert [(row.source, row.scope) for row in rows] == [
        ("claude_sessions", "personal"),
        ("codex_sessions", "personal"),
    ]
    assert {row.source_account_id for row in rows} == {codex_account, claude_account}
    assert item_count == 2

    state = get_sync_state(codex_account)
    assert state is not None
    assert state.last_sync_at is not None
    assert state.last_error is None

    codex_second = run_connector_account_sync(codex_account, user_id, chat_model=MockChat())
    assert codex_second.status == "succeeded"
    assert codex_second.report is not None
    assert len(codex_second.report.doc_ids) == 0
