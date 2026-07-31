"""P6.7.2 send from_addr routing + per-mailbox ownership (`docs/p6-unified-mail.md` §12.4-3).

With multi-account on, `resolve_send_backend_config` must resolve the from_addr to
a row that owner owns and use THAT row's secret/base_url. A from_addr they do not
own — including the case where the owner holds zero rows — rejects with
`MailSendOwnershipError`; it must never fall through to env routing. Only flag off
keeps the legacy env/domain routing.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import insert

from orthus.connectors.account_config import configure_connector_account
from orthus.db import session
from orthus.mail.send import (
    MailSendOwnershipError,
    MailSendRoutingError,
    resolve_send_backend_config,
    send_mail,
)
from orthus.schemas.canonical import MailSendRequest
from orthus.settings import get_settings
from orthus.tables import auth_identities, users


def _configure_mail_account(
    slug: str,
    owner_id: UUID,
    *,
    base_url: str,
    owner_addr: str,
    secret_key: str,
    secret_value: str,
) -> None:
    configure_connector_account(
        slug,
        owner_id,
        input_settings={"base_url": base_url, "owner_addr": owner_addr, "ingest_scope": "owner"},
        input_secrets={secret_key: secret_value},
        account_kind="personal",
        owner_email=owner_addr,
    )


def _enable_multi(settings) -> None:
    settings.mail_multi_account_enabled = True
    settings.owner_scope_enabled = True
    settings.node_kind = "company"
    settings.node_id = "company"


def _request(from_addr: str) -> MailSendRequest:
    return MailSendRequest(from_addr=from_addr, to="dest@example.com", subject="s", text="body")


def test_flag_off_uses_env_domain_routing(user_id: UUID):
    settings = get_settings()
    settings.mail_multi_account_enabled = False
    settings.mail_nova_base_url = "https://env.nova.test"
    settings.mail_nova_api_key = "env-nova-key"
    settings.mail_nova_owner = "owner@nova.example"

    cfg = resolve_send_backend_config("owner@nova.example", settings, user_id)

    assert cfg.backend == "nova"
    assert cfg.base_url == "https://env.nova.test/v0"
    assert cfg.token == "env-nova-key"


def test_flag_on_no_rows_rejects_no_env_fallthrough(user_id: UUID):
    settings = get_settings()
    _enable_multi(settings)
    # Env values are present and would route if we fell through — we must NOT.
    settings.mail_acme_base_url = "https://env.dz.test"
    settings.mail_acme_send_token = "env-dz-send"
    settings.mail_acme_owner = "owner@acme.example"

    # Owner has zero mail rows -> strictly self-service, no env send fallback.
    with pytest.raises(MailSendOwnershipError):
        resolve_send_backend_config("lead@acme.example", settings, user_id)


def test_flag_on_owned_from_addr_routes_to_that_rows_token(user_id: UUID):
    settings = get_settings()
    _enable_multi(settings)
    _configure_mail_account(
        "mail_nova",
        user_id,
        base_url="https://row.nova.test",
        owner_addr="me@nova.example",
        secret_key="api_key",
        secret_value="row-nova-key",
    )

    cfg = resolve_send_backend_config("me@nova.example", settings, user_id)

    assert cfg.backend == "nova"
    assert cfg.base_url == "https://row.nova.test/v0"
    assert cfg.token == "row-nova-key"
    assert cfg.owner == "me@nova.example"


def test_flag_on_owned_addr_without_token_uses_company_env_key(user_id: UUID):
    """A mailbox registered with only its address sends via the shared company
    app key + base_url (no per-mailbox token on the row)."""
    settings = get_settings()
    _enable_multi(settings)
    settings.mail_nova_base_url = "https://env.nova.test"
    settings.mail_nova_api_key = "env-nova-key"
    configure_connector_account(
        "mail_nova",
        user_id,
        input_settings={"owner_addr": "me@nova.example", "ingest_scope": "owner"},
        input_secrets={},
        account_kind="personal",
        owner_email="me@nova.example",
    )

    cfg = resolve_send_backend_config("me@nova.example", settings, user_id)

    assert cfg.backend == "nova"
    assert cfg.base_url == "https://env.nova.test/v0"
    assert cfg.token == "env-nova-key"
    assert cfg.owner == "me@nova.example"


def test_flag_on_owned_from_addr_is_case_insensitive(user_id: UUID):
    settings = get_settings()
    _enable_multi(settings)
    _configure_mail_account(
        "mail_acme",
        user_id,
        base_url="https://row.dz.test",
        owner_addr="me@acme.example",
        secret_key="api_token",
        secret_value="row-dz-token",
    )

    cfg = resolve_send_backend_config("Me@Acme.EXAMPLE", settings, user_id)

    assert cfg.backend == "acme"
    assert cfg.token == "row-dz-token"


def test_flag_on_unowned_from_addr_rejects_no_env_fallthrough(user_id: UUID):
    settings = get_settings()
    _enable_multi(settings)
    # Env values are present and would route if we fell through — we must NOT.
    settings.mail_nova_base_url = "https://env.nova.test"
    settings.mail_nova_api_key = "env-nova-key"
    settings.mail_nova_owner = "owner@nova.example"
    _configure_mail_account(
        "mail_nova",
        user_id,
        base_url="https://row.nova.test",
        owner_addr="me@nova.example",
        secret_key="api_key",
        secret_value="row-nova-key",
    )

    # `someone-else@nova.example` is a valid send domain but not a row this owner owns.
    with pytest.raises(MailSendOwnershipError):
        resolve_send_backend_config("someone-else@nova.example", settings, user_id)


def test_flag_on_other_owners_mailbox_never_sendable(user_id: UUID):
    """Security: owner A cannot send as owner B's mailbox even though both have rows."""
    settings = get_settings()
    _enable_multi(settings)

    other_owner = uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_owner, display_name="Other Owner"))
        s.commit()
    _configure_mail_account(
        "mail_nova",
        other_owner,
        base_url="https://other.nova.test",
        owner_addr="other@nova.example",
        secret_key="api_key",
        secret_value="other-key",
    )
    _configure_mail_account(
        "mail_nova",
        user_id,
        base_url="https://row.nova.test",
        owner_addr="me@nova.example",
        secret_key="api_key",
        secret_value="row-nova-key",
    )

    # user_id owns me@nova.example but NOT other@nova.example -> reject, never other's token.
    with pytest.raises(MailSendOwnershipError):
        resolve_send_backend_config("other@nova.example", settings, user_id)


def test_flag_on_stale_row_for_another_verified_user_never_sendable(user_id: UUID):
    settings = get_settings()
    _enable_multi(settings)
    settings.mail_nova_base_url = "https://env.nova.test"
    settings.mail_nova_api_key = "env-nova-key"
    _configure_mail_account(
        "mail_nova",
        user_id,
        base_url="https://row.nova.test",
        owner_addr="other@nova.example",
        secret_key="api_key",
        secret_value="row-nova-key",
    )
    other_owner = uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_owner, display_name="Other Owner"))
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid4(),
                user_id=other_owner,
                provider="test",
                provider_subject=f"sub-{other_owner}",
                email="other@nova.example",
                email_verified=True,
            )
        )
        s.commit()

    with pytest.raises(MailSendOwnershipError):
        resolve_send_backend_config("other@nova.example", settings, user_id)


def test_routing_reject_for_unroutable_domain(user_id: UUID):
    settings = get_settings()
    _enable_multi(settings)

    with pytest.raises(MailSendRoutingError):
        resolve_send_backend_config("me@gmail.com", settings, user_id)


def test_send_mail_owned_from_addr_uses_row_token(user_id: UUID):
    settings = get_settings()
    _enable_multi(settings)
    _configure_mail_account(
        "mail_acme",
        user_id,
        base_url="https://row.dz.test",
        owner_addr="me@acme.example",
        secret_key="api_token",
        secret_value="row-dz-token",
    )
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "msg-1", "resend_id": "re_1"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await send_mail(
                _request("me@acme.example"), settings, owner_id=user_id, client=client
            )

    result = asyncio.run(run())

    assert result.backend == "acme"
    assert result.status == "sent"
    assert seen["url"] == "https://row.dz.test/mail/send"
    assert seen["auth"] == "Bearer row-dz-token"


def test_send_mail_unowned_from_addr_raises_ownership(user_id: UUID):
    settings = get_settings()
    _enable_multi(settings)
    settings.mail_nova_base_url = "https://env.nova.test"
    settings.mail_nova_api_key = "env-nova-key"
    _configure_mail_account(
        "mail_nova",
        user_id,
        base_url="https://row.nova.test",
        owner_addr="me@nova.example",
        secret_key="api_key",
        secret_value="row-nova-key",
    )

    async def run():
        await send_mail(_request("intruder@nova.example"), settings, owner_id=user_id)

    with pytest.raises(MailSendOwnershipError):
        asyncio.run(run())
