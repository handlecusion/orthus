"""Node-local periodic connector tick CLI."""

from __future__ import annotations

import json
from sqlalchemy import select

from orthus.connectors import tick
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import connector_runs, documents


def test_connector_tick_ensures_and_runs_due_chat_exports(user_id, tmp_path, monkeypatch, capsys):
    root = tmp_path / "chat-exports"
    root.mkdir()
    (root / "conversations.json").write_text(
        json.dumps(
            [
                {
                    "id": "tick-conv",
                    "title": "Tick connector sync",
                    "mapping": {
                        "u": {
                            "message": {
                                "author": {"role": "user"},
                                "content": {"parts": ["Tick sync user message"]},
                            }
                        }
                    },
                }
            ]
        ),
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

    tick.main(["chat_exports", "--concurrency", "2"])

    captured = capsys.readouterr()
    assert "chat_exports periodic succeeded" in captured.out
    assert "created=1" in captured.out
    with session() as s:
        doc = s.execute(select(documents.c.source, documents.c.scope)).one()
        run = s.execute(select(connector_runs.c.reason, connector_runs.c.status)).one()
    assert doc.source == "chat_exports"
    assert doc.scope == "personal"
    assert run.reason == "periodic"
    assert run.status == "succeeded"


def test_connector_slugs_merges_positionals_and_env():
    assert tick.connector_slugs(
        ["gws_gmail", "gws_gmail"],
        "gws_drive, chat_exports",
    ) == ["gws_gmail", "gws_drive", "chat_exports"]
