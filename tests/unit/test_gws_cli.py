"""gws CLI connector normalization."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from orthus.connectors.gws_cli import (
    GwsCliError,
    GwsCliRunner,
    GwsCliUnavailable,
    GwsDriveConnector,
    GwsGmailConnector,
)


class _FakeGwsRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run_json(self, args):
        call = list(args)
        self.calls.append(call)
        if call[:4] == ["gmail", "users", "messages", "list"]:
            return {
                "messages": [
                    {"id": "msg-1", "threadId": "thread-1"},
                    {"id": "msg-2", "threadId": "thread-2"},
                ]
            }
        if call[:2] == ["gmail", "+read"]:
            message_id = call[call.index("--id") + 1]
            return {
                "message_id": f"<{message_id}@gmail.test>",
                "subject": f"Subject {message_id}",
                "from": "sender@example.com",
                "to": "me@example.com",
                "date": "Fri, 29 May 2026 10:11:12 +0900",
                "labelIds": ["INBOX", "UNREAD", "STARRED"] if message_id == "msg-1" else ["SENT"],
                "body_text": f"Body for {message_id} token=secret-value",
            }
        if call[:3] == ["drive", "files", "list"]:
            return {
                "files": [
                    {
                        "id": "drive-doc-1",
                        "name": "Roadmap",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-05-30T01:02:03Z",
                        "webViewLink": "https://drive.google.com/file/d/drive-doc-1",
                    }
                ]
            }
        raise AssertionError(f"unexpected gws call: {call}")

    def run_to_file(self, args, output_path: Path):
        self.calls.append(list(args))
        output_path.write_text("Drive body with api_key=secret-value", encoding="utf-8")
        return "{}"


def test_gws_gmail_connector_reads_message_bodies_with_since_query() -> None:
    runner = _FakeGwsRunner()
    connector = GwsGmailConnector(runner, query="in:inbox", max_messages=2, max_body_chars=500)

    docs = list(connector.iter_documents(datetime(2026, 5, 28, tzinfo=UTC)))

    assert [doc.source_external_id for doc in docs] == ["gmail:msg-1", "gmail:msg-2"]
    assert docs[0].source == "gws_gmail"
    assert "Subject msg-1" in docs[0].title
    assert "[REDACTED_SECRET]" in docs[0].markdown
    list_call = runner.calls[0]
    assert list_call[:4] == ["gmail", "users", "messages", "list"]
    assert "after:2026/05/28" in list_call[list_call.index("--params") + 1]


def test_gws_gmail_connector_exposes_structured_messages() -> None:
    runner = _FakeGwsRunner()
    connector = GwsGmailConnector(runner, query="in:anywhere", max_messages=2)

    messages = list(connector.iter_messages(None))

    assert [message.message_id for message in messages] == ["msg-1", "msg-2"]
    assert messages[0].rfc_message_id == "<msg-1@gmail.test>"
    assert messages[0].read is False
    assert messages[0].starred is True
    assert messages[0].direction == "inbound"
    assert messages[1].direction == "outbound"


def test_gws_drive_connector_exports_google_docs_text() -> None:
    runner = _FakeGwsRunner()
    connector = GwsDriveConnector(runner, query="trashed=false", max_files=1)

    docs = list(connector.iter_documents(None))

    assert len(docs) == 1
    doc = docs[0]
    assert doc.source == "gws_drive"
    assert doc.source_external_id == "drive:drive-doc-1"
    assert doc.source_last_edited_at == datetime(2026, 5, 30, 1, 2, 3, tzinfo=UTC)
    assert "Drive body" in doc.markdown
    assert "[REDACTED_SECRET]" in doc.markdown
    assert ["drive", "files", "export"] == runner.calls[1][:3]


def test_gws_runner_reports_missing_command() -> None:
    runner = GwsCliRunner(command="/tmp/gws")

    with pytest.raises(GwsCliUnavailable) as exc:
        runner.run_json(["drive", "files", "list"])

    assert "command not found" in str(exc.value)
    assert "gws auth login -s drive,gmail" in str(exc.value)


def test_gws_runner_rejects_non_allowlisted_command() -> None:
    runner = GwsCliRunner(command="bash -c gws")

    with pytest.raises(GwsCliError) as exc:
        runner.run_json(["drive", "files", "list"])

    assert "not allowlisted" in str(exc.value)


def test_gws_runner_invokes_subprocess_with_argv_without_shell(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout='{"ok": true}',
            stderr="",
        )

    monkeypatch.setattr("orthus.connectors.gws_cli.subprocess.run", fake_run)
    runner = GwsCliRunner(command="gws")

    assert runner.run_json(["drive", "files", "list"]) == {"ok": True}

    argv, kwargs = calls[0]
    assert argv == ["gws", "drive", "files", "list", "--format", "json"]
    assert kwargs.get("shell", False) is False
