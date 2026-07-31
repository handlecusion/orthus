"""P6.7.2 inbox read isolation (`docs/p6-unified-mail.md` §12.4-2).

With multi-account on, `list_unified_inbox` enumerates ONLY that owner's mail
account rows (gmail is not part of the company inbox). Another owner's mailbox
must never be fetched, and an owner with zero rows gets an empty inbox (no env
fan-out). Only flag off keeps the legacy env fan-out.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
from sqlalchemy import insert

from orthus.connectors.account_config import configure_connector_account
from orthus.db import session
from orthus.mail.backends import list_unified_inbox
from orthus.settings import get_settings
from orthus.tables import users


def _enable_multi(settings) -> None:
    settings.mail_multi_account_enabled = True
    settings.owner_scope_enabled = True
    settings.node_kind = "company"
    settings.node_id = "company"


def _configure_mail_account(owner_id: UUID, *, owner_addr: str, base_url: str) -> None:
    configure_connector_account(
        "mail_nova",
        owner_id,
        input_settings={"base_url": base_url, "owner_addr": owner_addr, "ingest_scope": "owner"},
        input_secrets={"api_key": f"{owner_addr}-key"},
        account_kind="personal",
        owner_email=owner_addr,
    )


def _recording_transport() -> tuple[httpx.MockTransport, list[str]]:
    fetched_owners: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # gws/gmail never hits HTTP; only the nova/acme backends do.
        fetched_owners.append(request.url.params.get("owner", ""))
        return httpx.Response(200, json={"emails": [], "total": 0, "unread": 0})

    return httpx.MockTransport(handler), fetched_owners


def _two_owners(user_id: UUID) -> UUID:
    other = uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other, display_name="Owner B"))
        s.commit()
    _configure_mail_account(user_id, owner_addr="a@nova.example", base_url="https://a.nova.test")
    _configure_mail_account(other, owner_addr="b@nova.example", base_url="https://b.nova.test")
    return other


def test_owner_a_only_sees_own_mailbox(user_id: UUID, monkeypatch):
    settings = get_settings()
    _enable_multi(settings)
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)
    _two_owners(user_id)
    transport, fetched_owners = _recording_transport()

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await list_unified_inbox(settings, owner_id=user_id, client=client)

    response = asyncio.run(run())

    # Only owner A's mailbox is fetched; owner B's address never appears.
    assert set(fetched_owners) == {"a@nova.example"}
    assert "b@nova.example" not in fetched_owners
    # Backend statuses cover exactly A's nova row — gmail is not in the company inbox.
    assert {b.backend for b in response.backends} == {"nova"}


def test_owner_b_only_sees_own_mailbox(user_id: UUID, monkeypatch):
    settings = get_settings()
    _enable_multi(settings)
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)
    other = _two_owners(user_id)
    transport, fetched_owners = _recording_transport()

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await list_unified_inbox(settings, owner_id=other, client=client)

    asyncio.run(run())

    assert set(fetched_owners) == {"b@nova.example"}
    assert "a@nova.example" not in fetched_owners


def test_flag_on_zero_rows_returns_empty_inbox(user_id: UUID, monkeypatch):
    settings = get_settings()
    _enable_multi(settings)
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)
    # Env single-account values are present and would show if we fell through.
    settings.mail_nova_base_url = "https://env.nova.test"
    settings.mail_nova_api_key = "env-key"
    settings.mail_nova_owner = "envowner@nova.example"
    transport, fetched_owners = _recording_transport()

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await list_unified_inbox(settings, owner_id=user_id, client=client)

    response = asyncio.run(run())

    # No configured mailboxes -> nothing fetched, no backends, no env fan-out.
    assert fetched_owners == []
    assert response.backends == []
    assert response.total == 0


def test_flag_off_uses_env_fan_out(user_id: UUID, monkeypatch):
    settings = get_settings()
    settings.mail_multi_account_enabled = False
    settings.owner_scope_enabled = True
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.mail_nova_base_url = "https://env.nova.test"
    settings.mail_nova_api_key = "env-key"
    settings.mail_nova_owner = "envowner@nova.example"
    settings.mail_acme_base_url = ""
    settings.mail_acme_api_token = ""
    settings.mail_acme_session = ""
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)
    # Even with an account row present, flag off must use env fan-out.
    _configure_mail_account(user_id, owner_addr="a@nova.example", base_url="https://a.nova.test")
    transport, fetched_owners = _recording_transport()

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await list_unified_inbox(settings, owner_id=user_id, client=client)

    asyncio.run(run())

    # Env single-account owner is fetched, NOT the account row's address.
    assert set(fetched_owners) == {"envowner@nova.example"}
