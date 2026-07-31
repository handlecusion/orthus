"""P6.7.1 multi-account mail backend config resolution.

Acceptance: `backend_configs_from_accounts` is byte-for-byte identical to the
legacy env `backend_configs_from_settings` ONLY when the
`mail_multi_account_enabled` flag is off. With the flag on the company inbox is
strictly self-service: each active account row resolves to a `MailBackendConfig`
from its `settings_redacted` plus its keychain/secret-ref secret, zero rows yield
an empty list (no env single-account fallback), and gmail is NOT appended (gmail
belongs to the personal-mail page, not the company unified inbox).
"""

from __future__ import annotations

import uuid
from uuid import UUID

from sqlalchemy import insert, select, update

from orthus.connectors.account_config import configure_connector_account
from orthus.db import session
from orthus.mail.backends import (
    backend_configs_from_accounts,
    backend_configs_from_settings,
)
from orthus.settings import get_settings
from orthus.tables import auth_identities, connector_accounts, users


def _configure_mail_account(
    slug: str,
    owner_id: UUID,
    *,
    base_url: str,
    owner_addr: str,
    secret_key: str,
    secret_value: str,
    ingest_scope: str = "owner",
) -> UUID:
    return configure_connector_account(
        slug,
        owner_id,
        input_settings={
            "base_url": base_url,
            "owner_addr": owner_addr,
            "ingest_scope": ingest_scope,
        },
        input_secrets={secret_key: secret_value},
        account_kind="personal",
        # P6.7.2 identity binding: a mailbox can only be bound to its own owner's
        # verified email; these foundation tests register self-owned mailboxes.
        owner_email=owner_addr,
    )


def test_flag_off_matches_settings(user_id: UUID):
    settings = get_settings()
    settings.mail_multi_account_enabled = False
    settings.mail_nova_base_url = "https://nova.test"
    settings.mail_nova_owner = "owner@nova.example"
    settings.mail_nova_api_key = "inline-nova"
    settings.mail_acme_owner = "owner@acme.example"

    # Even with account rows present, the off flag must ignore them entirely.
    settings.owner_scope_enabled = True
    _configure_mail_account(
        "mail_nova",
        user_id,
        base_url="https://row.nova.test",
        owner_addr="row@nova.example",
        secret_key="api_key",
        secret_value="row-nova-key",
    )

    assert backend_configs_from_accounts(settings, user_id) == backend_configs_from_settings(
        settings
    )


def test_flag_on_with_account_rows_returns_only_rows(user_id: UUID):
    settings = get_settings()
    settings.mail_multi_account_enabled = True
    settings.owner_scope_enabled = True

    nova_account_id = _configure_mail_account(
        "mail_nova",
        user_id,
        base_url="https://row.nova.test",
        owner_addr="me@nova.example",
        secret_key="api_key",
        secret_value="row-nova-key",
    )
    dz_account_id = _configure_mail_account(
        "mail_acme",
        user_id,
        base_url="https://row.acme.test",
        owner_addr="me@acme.example",
        secret_key="api_token",
        secret_value="row-dz-token",
    )

    configs = backend_configs_from_accounts(settings, user_id)

    by_backend = {cfg.backend: cfg for cfg in configs}
    # Strictly the owner's configured mailboxes — no gmail, no env backends.
    assert set(by_backend) == {"nova", "acme"}

    nova = by_backend["nova"]
    assert nova.account_id == nova_account_id
    assert nova.base_url == "https://row.nova.test/v0"
    assert nova.owner == "me@nova.example"
    assert nova.bearer_token == "row-nova-key"
    assert nova.configured is True

    dz = by_backend["acme"]
    assert dz.account_id == dz_account_id
    assert dz.base_url == "https://row.acme.test"
    assert dz.owner == "me@acme.example"
    assert dz.bearer_token == "row-dz-token"
    assert dz.configured is True


def test_flag_on_skips_stale_cross_domain_mail_rows(user_id: UUID):
    """Rows created before the slug/domain guard must not render or route."""
    settings = get_settings()
    settings.mail_multi_account_enabled = True
    settings.owner_scope_enabled = True

    _configure_mail_account(
        "mail_nova",
        user_id,
        base_url="https://row.nova.test",
        owner_addr="me@nova.example",
        secret_key="api_key",
        secret_value="row-nova-key",
    )
    with session() as s:
        row = s.execute(
            select(connector_accounts.c.account_id, connector_accounts.c.settings_redacted).where(
                connector_accounts.c.connector_slug == "mail_nova",
                connector_accounts.c.owner_id == user_id,
            )
        ).one()
        stale_settings = dict(row.settings_redacted)
        stale_settings["owner_addr"] = ["me@acme.example"]
        s.execute(
            update(connector_accounts)
            .where(connector_accounts.c.account_id == row.account_id)
            .values(settings_redacted=stale_settings)
        )
        s.commit()

    assert backend_configs_from_accounts(settings, user_id) == []


def test_flag_on_skips_stale_row_for_another_verified_user(user_id: UUID):
    settings = get_settings()
    settings.mail_multi_account_enabled = True
    settings.owner_scope_enabled = True

    _configure_mail_account(
        "mail_nova",
        user_id,
        base_url="https://row.nova.test",
        owner_addr="other@nova.example",
        secret_key="api_key",
        secret_value="row-nova-key",
    )
    other_id = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_id, display_name="Other"))
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=other_id,
                provider="test",
                provider_subject=f"sub-{other_id}",
                email="other@nova.example",
                email_verified=True,
            )
        )
        s.commit()

    assert backend_configs_from_accounts(settings, user_id) == []


def test_flag_on_with_zero_rows_returns_empty(user_id: UUID):
    settings = get_settings()
    settings.mail_multi_account_enabled = True
    settings.owner_scope_enabled = True
    settings.mail_nova_base_url = "https://nova.test"
    settings.mail_nova_owner = "owner@nova.example"
    settings.mail_nova_api_key = "inline-nova"
    settings.mail_acme_owner = "owner@acme.example"

    # No account rows for this owner -> empty (no env single-account fallback).
    assert backend_configs_from_accounts(settings, user_id) == []


def test_flag_on_ignores_other_owners_rows(user_id: UUID):
    """An owner with no rows gets an empty list, never another owner's mailbox."""
    settings = get_settings()
    settings.mail_multi_account_enabled = True
    settings.owner_scope_enabled = True

    from uuid import uuid4

    from sqlalchemy import insert

    from orthus.db import session
    from orthus.tables import users

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

    # `user_id` has zero rows -> empty, never the other owner's mailbox or env.
    assert backend_configs_from_accounts(settings, user_id) == []


def test_flag_on_account_without_secret_uses_company_env_key(user_id: UUID):
    """A mailbox registered with just its address falls back to the shared
    company app key + base_url (the row carries no per-mailbox secret)."""
    settings = get_settings()
    settings.mail_multi_account_enabled = True
    settings.owner_scope_enabled = True
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

    configs = backend_configs_from_accounts(settings, user_id)
    by_backend = {cfg.backend: cfg for cfg in configs}
    assert set(by_backend) == {"nova"}
    nova = by_backend["nova"]
    assert nova.owner == "me@nova.example"
    assert nova.base_url == "https://env.nova.test/v0"
    assert nova.bearer_token == "env-nova-key"
    assert nova.configured is True
