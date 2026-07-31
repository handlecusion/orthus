"""P10 orthus-mcp mail read tools: mail_list shape/limit cap + mail_get truncate.

In-memory only (no network, no Postgres): the CentralClient is monkeypatched to
a recording fake, mirroring the ticket_* tool tests. The central-path wiring is
asserted with the same MockTransport pattern the inbox_summary test uses.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from orthus.mcp import server as server_module
from orthus.mcp.server import MAIL_BODY_TRUNCATE_CHARS, MAIL_LIST_MAX_LIMIT, build_server


def _call(server, name: str, args: dict) -> dict:
    result = asyncio.run(server.call_tool(name, args))
    content = result[0] if isinstance(result, (list, tuple)) else result
    return json.loads(content.text)


def _inbox_item(i: int, *, owner: str = "me@nova.example") -> dict:
    return {
        "backend": "nova",
        "account_id": None,
        "external_id": f"m{i}",
        "scope": "company",
        "owner_addr": owner,
        "from_addr": f"sender{i}@example.com",
        "subject": f"subject {i}",
        "body_text": f"body {i}",
        "read": False,
        "received_at": f"2026-07-1{i % 10}T09:00:00+00:00",
    }


class _FakeCentral:
    """Records the central calls the mail tools make; no network."""

    inbox_payload: dict = {"items": []}
    message_payload: dict = {}
    calls: list[tuple[str, dict]] = []

    def __init__(self, *args, **kwargs) -> None:  # CentralClient() signature
        pass

    def mail_inbox(self, *, limit: int, search: str | None = None) -> dict:
        _FakeCentral.calls.append(("mail_inbox", {"limit": limit, "search": search}))
        return _FakeCentral.inbox_payload

    def mail_message(self, *, backend: str, external_id: str, account_id=None) -> dict:
        _FakeCentral.calls.append(
            ("mail_message", {"backend": backend, "id": external_id, "account_id": account_id})
        )
        return _FakeCentral.message_payload


@pytest.fixture()
def fake_central(monkeypatch):
    _FakeCentral.calls = []
    _FakeCentral.inbox_payload = {"items": []}
    _FakeCentral.message_payload = {}
    monkeypatch.setattr(server_module, "CentralClient", _FakeCentral)
    return _FakeCentral


def test_mail_list_returns_light_rows_without_bodies(fake_central):
    fake_central.inbox_payload = {"items": [_inbox_item(1), _inbox_item(2)]}
    server = build_server()

    payload = _call(server, "mail_list", {"limit": 10})

    assert payload["count"] == 2
    row = payload["mails"][0]
    assert set(row) == {"message_id", "from", "subject", "date", "mailbox", "read"}
    assert row["message_id"] == "nova:-:m1"
    assert row["from"] == "sender1@example.com"
    assert row["mailbox"] == "me@nova.example"
    assert "body" not in row and "body_text" not in row
    # default limit=10 flows to central
    assert fake_central.calls == [("mail_inbox", {"limit": 10, "search": None})]


def test_mail_list_caps_limit_at_30(fake_central):
    fake_central.inbox_payload = {"items": [_inbox_item(i) for i in range(9)]}
    server = build_server()

    _call(server, "mail_list", {"limit": 500})

    assert fake_central.calls[0][1]["limit"] == MAIL_LIST_MAX_LIMIT == 30


def test_mail_list_mailbox_filter_and_query_passthrough(fake_central):
    fake_central.inbox_payload = {
        "items": [
            _inbox_item(1, owner="me@nova.example"),
            _inbox_item(2, owner="other@nova.example"),
        ]
    }
    server = build_server()

    payload = _call(server, "mail_list", {"mailbox": "ME@nova.example", "query": "발주"})

    assert payload["count"] == 1
    assert payload["mails"][0]["mailbox"] == "me@nova.example"
    assert fake_central.calls[0][1]["search"] == "발주"


def test_mail_get_truncates_body_and_lists_attachment_names(fake_central):
    long_body = "가" * (MAIL_BODY_TRUNCATE_CHARS + 500)
    fake_central.message_payload = {
        "backend": "nova",
        "external_id": "m1",
        "owner_addr": "me@nova.example",
        "from_addr": "sender@example.com",
        "to_addr": ["me@nova.example"],
        "subject": "long one",
        "body_text": long_body,
        "received_at": "2026-07-17T09:00:00+00:00",
        "attachments": [
            {"filename": "발주서.pdf", "content_type": "application/pdf", "size": 100},
            {"filename": "견적.xlsx"},
        ],
    }
    server = build_server()

    payload = _call(server, "mail_get", {"message_id": "nova:-:m1"})

    assert len(payload["body"]) == MAIL_BODY_TRUNCATE_CHARS
    assert payload["truncated"] is True
    assert payload["attachments"] == ["발주서.pdf", "견적.xlsx"]
    assert "다운로드" in payload["attachments_note"]
    # message_id round-trips to the central backend/id pair, no account_id.
    assert fake_central.calls == [
        ("mail_message", {"backend": "nova", "id": "m1", "account_id": None})
    ]


def test_mail_get_short_body_not_truncated(fake_central):
    fake_central.message_payload = {
        "backend": "acme",
        "external_id": "x1",
        "from_addr": "a@b.com",
        "subject": "s",
        "body_text": "short",
        "attachments": [],
    }
    server = build_server()

    payload = _call(
        server, "mail_get", {"message_id": "acme:6a1e0a1c-0000-4000-8000-000000000001:x1"}
    )

    assert payload["body"] == "short"
    assert payload["truncated"] is False
    # multi-account message_id carries the account_id through to central.
    assert fake_central.calls[0][1]["account_id"] == "6a1e0a1c-0000-4000-8000-000000000001"


def test_mail_get_invalid_message_id_is_tool_error(fake_central):
    from mcp.server.fastmcp.exceptions import ToolError

    server = build_server()
    raised: list[Exception] = []
    try:
        asyncio.run(server.call_tool("mail_get", {"message_id": "no-separators"}))
    except ToolError as exc:
        raised.append(exc)
    except Exception as exc:  # noqa: BLE001 — FastMCP may re-wrap
        raised.append(exc)
    assert raised and "message_id" in str(raised[0])
    assert fake_central.calls == []


def test_central_client_mail_paths():
    """CentralClient.mail_inbox/mail_message hit the dual-auth read routes."""
    import httpx

    from orthus.mcp.central import CentralClient, CentralConfig

    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"ok": True})

    client = CentralClient(CentralConfig(base_url="https://central.example", token="dct_x"))
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    httpx.Client = _patched  # type: ignore[assignment]
    try:
        client.mail_inbox(limit=10, search="q")
        client.mail_message(backend="nova", external_id="m1", account_id=None)
    finally:
        httpx.Client = real_client  # type: ignore[assignment]

    assert seen[0] == ("GET", "/mail/inbox", {"limit": "10", "search": "q"})
    assert seen[1] == ("GET", "/mail/message", {"backend": "nova", "id": "m1"})
