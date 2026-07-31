from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from orthus.mail.backends import (
    MailAttachmentError,
    MailBackendConfig,
    backend_configs_from_settings,
    fetch_backend_attachment,
    list_unified_inbox,
    normalize_backend_email,
)
from orthus.secrets import clear_memory_secrets, put_secret
from orthus.settings import get_settings
from orthus.settings import Settings


def test_normalize_backend_email_maps_company_scope_and_attachments():
    account_id = uuid4()
    email = normalize_backend_email(
        "nova",
        {
            "id": "msg-1",
            "message_id": "<msg-1@nova.example>",
            "from": "CEO <ceo@nova.example>",
            "to": "ys@example.com",
            "cc": ["ops@acme.example"],
            "subject": "P6",
            "text": "Mail body",
            "created_at": "2026-06-10T10:00:00Z",
            "read": "false",
            "starred": "1",
            "attachments": [
                {"filename": "brief.pdf", "content_type": "application/pdf", "size": "1234"}
            ],
        },
        owner_addr="ys@example.com",
        account_id=account_id,
    )

    assert email.backend == "nova"
    assert email.account_id == account_id
    assert email.external_id == "msg-1"
    assert email.message_id == "<msg-1@nova.example>"
    assert email.scope == "company"
    assert email.owner_addr == "ys@example.com"
    assert email.from_addr == "CEO <ceo@nova.example>"
    assert email.to_addr == ["ys@example.com"]
    assert email.cc_addr == ["ops@acme.example"]
    assert email.subject == "P6"
    assert email.body_text == "Mail body"
    assert email.read is False
    assert email.starred is True
    assert email.received_at == datetime(2026, 6, 10, 10, 0, tzinfo=UTC)
    assert email.sent_at is None
    assert email.attachment_count == 1
    assert email.attachments[0].filename == "brief.pdf"


def test_normalize_backend_email_maps_personal_outbound():
    email = normalize_backend_email(
        "gmail",
        {
            "external_id": "gmail-1",
            "direction": "outbound",
            "from_addr": "ys@gmail.com",
            "to_addr": "friend@example.com",
            "subject": "hello",
            "sent_at": "Wed, 10 Jun 2026 12:00:00 +0900",
            "read": True,
        },
        owner_addr="ys@gmail.com",
    )

    assert email.scope == "personal"
    assert email.direction == "outbound"
    assert email.sent_at is not None
    assert email.received_at is None
    assert email.read is True


def test_display_scope_is_address_gated_not_ingest_destination():
    # Display scope marks the row by recipient domain mix; it is independent of the
    # ingest write destination (ingest_scope_and_owner, tested in integration).
    gmail_row = normalize_backend_email(
        "gmail",
        {
            "id": "mixed-gmail",
            "from_addr": "friend@example.com",
            "to_addr": "owner@gmail.com, ops@nova.example",
            "subject": "mixed thread",
        },
    )

    assert gmail_row.scope == "company"


def test_backend_configs_prefer_secret_refs(monkeypatch):
    settings = get_settings()
    clear_memory_secrets()
    monkeypatch.setattr(settings, "secret_backend", "memory")
    monkeypatch.setattr(settings, "mail_nova_base_url", "https://nova.test")
    monkeypatch.setattr(settings, "mail_nova_owner", "owner@nova.example")
    monkeypatch.setattr(settings, "mail_nova_api_key", "inline-nova-key")
    monkeypatch.setattr(settings, "mail_nova_api_key_secret_ref", "orthus/mail/nova/key")
    put_secret("orthus/mail/nova/key", "secret-nova-key", backend="memory")

    configs = backend_configs_from_settings(settings)

    assert configs[0].backend == "nova"
    assert configs[0].base_url == "https://nova.test/v0"
    assert configs[0].bearer_token == "secret-nova-key"


def test_nova_env_aliases_and_root_base_normalize(monkeypatch):
    monkeypatch.setenv("NOVA_API_BASE_URL", "https://api.nova.example")
    monkeypatch.setenv("NOVA_APP_API_KEY", "nova-app-key")

    settings = Settings(_env_file=None)
    configs = backend_configs_from_settings(settings)

    assert settings.mail_nova_base_url == "https://api.nova.example"
    assert settings.mail_nova_v0_base_url() == "https://api.nova.example/v0"
    assert configs[0].base_url == "https://api.nova.example/v0"
    assert configs[0].bearer_token == "nova-app-key"


def test_list_unified_inbox_merges_configured_backends(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)
    monkeypatch.setattr(settings, "mail_nova_base_url", "https://nova.test")
    monkeypatch.setattr(settings, "mail_nova_api_key", "nova-key")
    monkeypatch.setattr(settings, "mail_nova_owner", "owner@nova.example")
    monkeypatch.setattr(settings, "mail_acme_base_url", "https://acme.test")
    monkeypatch.setattr(settings, "mail_acme_api_token", "")
    monkeypatch.setattr(settings, "mail_acme_session", "acme-session")
    monkeypatch.setattr(settings, "mail_acme_owner", "owner@acme.example")
    monkeypatch.setattr(settings, "mail_inbox_default_limit", 50)

    seen: list[tuple[str, str | None, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(
            (
                str(request.url),
                request.headers.get("authorization"),
                request.headers.get("x-session"),
            )
        )
        if request.url.host == "nova.test":
            assert path in ("/v0/mail/all", "/v0/mail/trash")
            assert request.url.params["owner"] == "owner@nova.example"
            assert request.url.params["limit"] == "2"
            # Search is provider-side (full-mailbox LIKE), not window filtering.
            assert request.url.params["search"] == "p6"
            return httpx.Response(
                200,
                json={
                    "emails": [
                        {
                            "id": "nova-new",
                            "from": "lead@nova.example",
                            "to": "owner@nova.example",
                            "subject": "p6 new",
                            "created_at": "2026-06-10T11:00:00Z",
                            "read": False,
                        }
                    ],
                    "total": 7,
                    "unread": 3,
                },
            )
        if request.url.host == "acme.test":
            assert path in ("/mail/all", "/mail/trash")
            assert request.url.params["owner"] == "owner@acme.example"
            assert request.url.params["search"] == "p6"
            # Provider does the filtering: no dz mail matches "p6".
            return httpx.Response(200, json={"emails": [], "total": 0, "unread": 0})
        raise AssertionError(f"unexpected host {request.url.host}")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await list_unified_inbox(settings, limit=2, search="p6", client=client)

    result = asyncio.run(run())

    assert [item.external_id for item in result.items] == ["nova-new"]
    assert result.total == 1
    assert result.unread == 1
    assert [(status.backend, status.configured, status.ok) for status in result.backends] == [
        ("nova", True, True),
        ("acme", True, True),
        ("gmail", False, True),
    ]
    # Two folder fetches per backend (all + trash); auth is the same on each.
    nova_reqs = [s for s in seen if "nova.test" in s[0]]
    dz_reqs = [s for s in seen if "acme.test" in s[0]]
    assert nova_reqs and all(s[1] == "Bearer nova-key" for s in nova_reqs)
    assert dz_reqs and all(s[2] == "acme-session" for s in dz_reqs)


def test_nova_attachment_proxy_does_not_call_forbidden_app_key_endpoint():
    cfg = MailBackendConfig(
        backend="nova",
        base_url="https://api.nova.example/v0",
        owner="owner@nova.example",
        bearer_token="nova-app-key",
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("nova attachment endpoint must not be called with app key")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await fetch_backend_attachment(cfg, "mail/attachments/x.png", client)

    try:
        asyncio.run(run())
    except MailAttachmentError as exc:
        assert "nova attachment proxy unsupported" in str(exc)
    else:
        raise AssertionError("expected MailAttachmentError")


def test_list_unified_inbox_reads_gmail_from_gws(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "mail_nova_owner", "")
    monkeypatch.setattr(settings, "mail_acme_owner", "")
    monkeypatch.setattr(settings, "conn_gws_command", "gws")
    monkeypatch.setattr(settings, "conn_gws_gmail_query", "in:anywhere")
    monkeypatch.setattr(settings, "conn_gws_gmail_max_messages", 10)
    monkeypatch.setattr(settings, "conn_gws_gmail_max_body_chars", 500)
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: True)

    class FakeRunner:
        def __init__(self, **_kwargs):
            self.calls: list[list[str]] = []

        def run_json(self, args):
            call = list(args)
            self.calls.append(call)
            if call[:4] == ["gmail", "users", "messages", "list"]:
                return {
                    "messages": [
                        {"id": "g-1", "threadId": "t-1"},
                        {"id": "g-2", "threadId": "t-2"},
                    ]
                }
            if call[:2] == ["gmail", "+read"]:
                message_id = call[call.index("--id") + 1]
                if message_id == "g-1":
                    return {
                        "headers": [
                            {"name": "Message-ID", "value": "<g-1@gmail.test>"},
                            {"name": "Subject", "value": "personal inbox"},
                            {"name": "From", "value": "friend@example.com"},
                            {"name": "To", "value": "owner@gmail.com"},
                            {"name": "Date", "value": "Wed, 10 Jun 2026 08:00:00 +0900"},
                        ],
                        "labelIds": ["INBOX", "UNREAD"],
                        "body_text": "hello from gmail",
                    }
                return {
                    "subject": "sent note",
                    "from": "owner@gmail.com",
                    "to": "friend@example.com",
                    "date": "Wed, 10 Jun 2026 09:00:00 +0900",
                    "labelIds": ["SENT"],
                    "body_text": "sent from gmail",
                }
            raise AssertionError(f"unexpected gws call: {call}")

    monkeypatch.setattr("orthus.mail.backends.GwsCliRunner", FakeRunner)

    result = asyncio.run(list_unified_inbox(settings, limit=10))

    assert [item.external_id for item in result.items] == ["gmail:g-2", "gmail:g-1"]
    assert [(item.direction, item.read) for item in result.items] == [
        ("outbound", True),
        ("inbound", False),
    ]
    assert result.items[1].message_id == "<g-1@gmail.test>"
    assert result.items[1].scope == "personal"
    assert result.items[1].received_at == datetime(2026, 6, 9, 23, 0, tzinfo=UTC)
    assert result.total == 2
    assert result.unread == 1
    assert [(status.backend, status.configured, status.ok) for status in result.backends] == [
        ("nova", False, True),
        ("acme", False, True),
        ("gmail", True, True),
    ]


def test_list_unified_inbox_keeps_gmail_off_on_company_node(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_nova_owner", "")
    monkeypatch.setattr(settings, "mail_acme_owner", "")
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: True)

    class FailingRunner:
        def __init__(self, **_kwargs):
            raise AssertionError("company node must not call gws for Gmail inbox")

    monkeypatch.setattr("orthus.mail.backends.GwsCliRunner", FailingRunner)

    result = asyncio.run(list_unified_inbox(settings, limit=10))

    assert result.items == []
    assert [(status.backend, status.configured, status.ok) for status in result.backends] == [
        ("nova", False, True),
        ("acme", False, True),
        ("gmail", False, True),
    ]


def test_mail_kind_for_never_widens_unknown_to_company():
    settings = get_settings()
    # Garbage / empty / whitespace / unknown values must fall back to the
    # fail-safe "individual"; only an exact (case-insensitive) "shared" widens.
    for raw in ["", "  ", "garbage", "COMPANY", "company", "Individual123", "share", None]:
        settings.mail_nova_kind = raw  # type: ignore[assignment]
        assert settings.mail_kind_for("nova") == "individual"

    for raw in ["shared", "SHARED", "  Shared  "]:
        settings.mail_nova_kind = raw
        assert settings.mail_kind_for("nova") == "shared"

    for raw in ["individual", "INDIVIDUAL", "  Individual "]:
        settings.mail_nova_kind = raw
        assert settings.mail_kind_for("nova") == "individual"

    # An unconfigured backend name also defaults to the fail-safe.
    assert settings.mail_kind_for("unknown_backend") == "individual"


def test_list_unified_inbox_search_trusts_provider_filtering(monkeypatch):
    # A provider search hit may match deep body text the (truncated) list row
    # does not show — the local matcher must not re-filter company rows away.
    settings = get_settings()
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)
    monkeypatch.setattr(settings, "mail_nova_base_url", "https://nova.test")
    monkeypatch.setattr(settings, "mail_nova_api_key", "nova-key")
    monkeypatch.setattr(settings, "mail_nova_owner", "owner@nova.example")
    monkeypatch.setattr(settings, "mail_acme_base_url", "")
    monkeypatch.setattr(settings, "mail_acme_api_token", "")
    monkeypatch.setattr(settings, "mail_acme_session", "")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["search"] == "계약금"
        if request.url.path == "/v0/mail/trash":
            return httpx.Response(200, json={"emails": [], "total": 0, "unread": 0})
        return httpx.Response(
            200,
            json={
                "emails": [
                    {
                        "id": "deep-body-hit",
                        "from": "partner@example.com",
                        "to": "owner@nova.example",
                        # No visible field contains the query; the provider
                        # matched the full body it stores server-side.
                        "subject": "미팅 후속",
                        "body_text": "",
                        "created_at": "2026-06-10T11:00:00Z",
                    }
                ],
                "total": 1,
                "unread": 0,
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await list_unified_inbox(settings, limit=10, search="계약금", client=client)

    result = asyncio.run(run())

    assert [item.external_id for item in result.items] == ["deep-body-hit"]


def test_list_unified_inbox_marks_trash_route_rows_trashed(monkeypatch):
    # acme's list SELECT omits the `trashed` column; rows fetched via the
    # trash route must still be flagged so a trashed mail does not reappear as a
    # normal inbox row after reload.
    settings = get_settings()
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)
    monkeypatch.setattr(settings, "mail_nova_base_url", "https://nova.test")
    monkeypatch.setattr(settings, "mail_nova_api_key", "nova-key")
    monkeypatch.setattr(settings, "mail_nova_owner", "owner@nova.example")
    monkeypatch.setattr(settings, "mail_acme_base_url", "")
    monkeypatch.setattr(settings, "mail_acme_api_token", "")
    monkeypatch.setattr(settings, "mail_acme_session", "")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v0/mail/trash":
            return httpx.Response(
                200,
                json={
                    "emails": [
                        {
                            "id": "trashed-no-flag",
                            "from": "x@example.com",
                            "to": "owner@nova.example",
                            "subject": "bye",
                            "created_at": "2026-06-10T09:00:00Z",
                        }
                    ],
                    "total": 1,
                    "unread": 0,
                },
            )
        return httpx.Response(200, json={"emails": [], "total": 0, "unread": 0})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await list_unified_inbox(settings, limit=10, client=client)

    result = asyncio.run(run())

    assert [(i.external_id, i.trashed) for i in result.items] == [("trashed-no-flag", True)]
