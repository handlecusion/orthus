from __future__ import annotations

import asyncio

import httpx
import pytest

from orthus.mail.send import (
    MailSendRoutingError,
    MailSendUnconfiguredError,
    backend_for_from_addr,
    backend_send_configured,
    send_mail,
)
from orthus.schemas.canonical import MailSendRequest
from orthus.settings import get_settings


def _request(from_addr: str) -> MailSendRequest:
    return MailSendRequest(from_addr=from_addr, to="dest@example.com", subject="s", text="body")


def test_backend_for_from_addr_routes_company_domains():
    assert backend_for_from_addr("lead@acme.example") == "acme"
    assert backend_for_from_addr("Owner <owner@nova.example>".split("<")[-1].rstrip(">")) == "nova"
    assert backend_for_from_addr("owner@nova.example") == "nova"


def test_backend_for_from_addr_rejects_other_domains():
    for addr in ("me@gmail.com", "x@example.com", "no-at-symbol"):
        with pytest.raises(MailSendRoutingError):
            backend_for_from_addr(addr)


def test_backend_send_configured_requires_send_token(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_acme_base_url", "https://dz.test")
    monkeypatch.setattr(settings, "mail_acme_send_token", "")
    monkeypatch.setattr(settings, "mail_acme_api_token", "read-token")
    monkeypatch.setattr(settings, "mail_acme_session", "read-session")
    # Read creds present, but the scoped send token is unset -> send unconfigured.
    assert backend_send_configured("acme", settings) is False

    monkeypatch.setattr(settings, "mail_acme_send_token", "dz-send-token")
    assert backend_send_configured("acme", settings) is True


def test_backend_send_configured_nova_uses_read_api_key(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_nova_base_url", "https://nova.test")
    monkeypatch.setattr(settings, "mail_nova_api_key", "")
    assert backend_send_configured("nova", settings) is False
    monkeypatch.setattr(settings, "mail_nova_api_key", "nova-key")
    assert backend_send_configured("nova", settings) is True


def test_send_mail_unconfigured_raises(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_nova_base_url", "https://nova.test")
    monkeypatch.setattr(settings, "mail_nova_api_key", "")

    async def run():
        await send_mail(_request("owner@nova.example"), settings)

    with pytest.raises(MailSendUnconfiguredError):
        asyncio.run(run())


def test_send_mail_acme_success(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_acme_base_url", "https://dz.test")
    monkeypatch.setattr(settings, "mail_acme_send_token", "dz-send-token")
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "msg-7", "resend_id": "re_123"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await send_mail(_request("lead@acme.example"), settings, client=client)

    result = asyncio.run(run())
    assert result.backend == "acme"
    assert result.status == "sent"
    assert result.provider_message_id == "re_123"
    assert seen["url"] == "https://dz.test/mail/send"
    assert seen["auth"] == "Bearer dz-send-token"


def test_send_mail_nova_success(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_nova_base_url", "https://nova.test/v0")
    monkeypatch.setattr(settings, "mail_nova_api_key", "nova-key")
    monkeypatch.setattr(settings, "mail_nova_owner", "owner@nova.example")
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "nova-9"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await send_mail(_request("owner@nova.example"), settings, client=client)

    result = asyncio.run(run())
    assert result.backend == "nova"
    assert result.status == "sent"
    assert result.provider_message_id == "nova-9"
    assert seen["url"] == "https://nova.test/v0/mail/send"
    assert seen["auth"] == "Bearer nova-key"


def test_send_mail_acme_403_maps_to_failed(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_acme_base_url", "https://dz.test")
    monkeypatch.setattr(settings, "mail_acme_send_token", "dz-send-token")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "secret upstream detail"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await send_mail(_request("lead@acme.example"), settings, client=client)

    result = asyncio.run(run())
    assert result.status == "failed"
    assert result.provider_message_id is None
    assert result.error == "403 forbidden"
    assert "secret upstream detail" not in (result.error or "")


def test_send_mail_acme_502_maps_to_failed(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_acme_base_url", "https://dz.test")
    monkeypatch.setattr(settings, "mail_acme_send_token", "dz-send-token")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "delivery failed"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await send_mail(_request("lead@acme.example"), settings, client=client)

    result = asyncio.run(run())
    assert result.status == "failed"
    assert result.error == "502 delivery failed"


# --- slice 3: cc/bcc/reply_to_id (참조 + Re 답장) ---


def test_mail_send_request_normalizes_cc_list():
    req = MailSendRequest(
        from_addr="me@nova.example",
        to="you@example.com",
        subject="s",
        text="t",
        cc=" a@x.com ,b@y.com ",
        reply_to_id="msg-1",
    )
    assert req.cc == "a@x.com, b@y.com"
    assert req.bcc is None
    assert req.reply_to_id == "msg-1"


def test_mail_send_request_normalizes_to_list():
    req = MailSendRequest(
        from_addr="me@nova.example",
        to=" a@x.com ,b@y.com ",
        subject="s",
        text="t",
    )
    assert req.to == "a@x.com, b@y.com"


def test_mail_send_request_rejects_invalid_cc():
    with pytest.raises(ValueError):
        MailSendRequest(
            from_addr="me@nova.example", to="you@example.com", subject="s", text="t", cc="not-an-email"
        )


def test_nova_body_includes_cc_bcc_reply_when_present():
    from orthus.mail.send import _nova_send_body

    req = MailSendRequest(
        from_addr="me@nova.example",
        to="you@example.com",
        subject="s",
        text="t",
        cc="c@x.com",
        bcc="b@x.com",
        reply_to_id="m-9",
    )
    body = _nova_send_body(None, req)
    assert body["cc"] == "c@x.com"
    assert body["bcc"] == "b@x.com"
    assert body["reply_to_id"] == "m-9"


def test_optional_send_fields_absent_keeps_body_unchanged():
    """참조/답장을 안 쓰면 발송 바디는 기존과 바이트 동일(회귀 방지)."""
    from orthus.mail.send import _apply_optional_send_fields

    req = MailSendRequest(from_addr="me@nova.example", to="you@example.com", subject="s", text="t")
    body: dict[str, object] = {}
    _apply_optional_send_fields(body, req)
    assert body == {}


def test_acme_send_carries_cc(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_acme_base_url", "https://dz.test")
    monkeypatch.setattr(settings, "mail_acme_send_token", "dz-send-token")

    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "dz-1"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            req = MailSendRequest(
                from_addr="lead@acme.example",
                to="dest@example.com",
                subject="s",
                text="body",
                cc="cc@example.com",
                reply_to_id="orig-1",
            )
            return await send_mail(req, settings, client=client)

    result = asyncio.run(run())
    assert result.status == "sent"
    assert captured["cc"] == "cc@example.com"
    assert captured["reply_to_id"] == "orig-1"
