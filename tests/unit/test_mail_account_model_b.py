"""P6.7.4 Model B account_id + delete (`docs/p6-unified-mail.md` §12.11).

One user may register multiple mailboxes per domain: a mail slug keys its
account_id by the normalized owner_addr, so info@/support@ become distinct rows.
Non-mail connectors keep a discriminator-free, byte-identical account_id. A user
can delete only their own mailbox rows.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from sqlalchemy import insert

from orthus.connectors.account_config import configure_connector_account
from orthus.connectors.state import (
    _ACCOUNT_NS,
    ConnectorAccountSpec,
    delete_connector_account,
    deterministic_account_id,
    get_connector_account,
)
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import connector_accounts, users


def _enable(settings) -> None:
    settings.owner_scope_enabled = True
    settings.node_kind = "company"
    settings.node_id = "company"
    settings.mail_multi_account_enabled = True


def _register_mail(
    owner_id: UUID,
    *,
    owner_addr: str,
    actor_role: str = "admin",
) -> UUID:
    return configure_connector_account(
        "mail_acme",
        owner_id,
        input_settings={
            "base_url": "https://dz.test",
            "owner_addr": owner_addr,
            "ingest_scope": "owner",
        },
        input_secrets={"api_token": f"tok-{owner_addr}"},
        account_kind="personal",
        owner_email="boss@acme.example",
        actor_role=actor_role,
    )


def _owner_addr_of(row) -> str:
    value = row._mapping["settings_redacted"]["owner_addr"]
    return value[0] if isinstance(value, list) else value


def _mail_rows(owner_id: UUID, slug: str) -> list:
    with session() as s:
        return s.execute(
            connector_accounts.select().where(
                connector_accounts.c.owner_id == owner_id,
                connector_accounts.c.connector_slug == slug,
            )
        ).all()


def test_admin_registers_two_mailboxes_two_rows(user_id: UUID):
    settings = get_settings()
    _enable(settings)

    info_id = _register_mail(user_id, owner_addr="info@acme.example")
    support_id = _register_mail(user_id, owner_addr="support@acme.example")

    assert info_id != support_id
    rows = _mail_rows(user_id, "mail_acme")
    addrs = {_owner_addr_of(row) for row in rows}
    assert len(rows) == 2
    assert addrs == {"info@acme.example", "support@acme.example"}


def test_same_address_reconfig_upserts_same_row(user_id: UUID):
    settings = get_settings()
    _enable(settings)

    first = _register_mail(user_id, owner_addr="info@acme.example")
    again = _register_mail(user_id, owner_addr="Info@Acme.Example")

    assert first == again
    assert len(_mail_rows(user_id, "mail_acme")) == 1


def test_non_mail_account_id_unchanged():
    # The discriminator default ("") must append nothing, so a non-mail account_id
    # equals the pre-change 6-part-key uuid5 exactly.
    owner = uuid.uuid4()
    spec = ConnectorAccountSpec(
        connector_slug="github",
        account_kind="personal",
        node_id="personal-a",
        auth_mode="token",
        owner_id=owner,
    )
    expected_key = ":".join(["github", "personal", "personal-a", str(owner), "", "token"])
    assert deterministic_account_id(spec) == uuid.uuid5(_ACCOUNT_NS, expected_key)


def test_company_account_id_unchanged():
    spec = ConnectorAccountSpec(
        connector_slug="slack",
        account_kind="company",
        node_id="company",
        auth_mode="token",
        project="nova",
    )
    expected_key = ":".join(["slack", "company", "company", "", "nova", "token"])
    assert deterministic_account_id(spec) == uuid.uuid5(_ACCOUNT_NS, expected_key)


def test_delete_one_mailbox_leaves_others(user_id: UUID):
    settings = get_settings()
    _enable(settings)

    info_id = _register_mail(user_id, owner_addr="info@acme.example")
    support_id = _register_mail(user_id, owner_addr="support@acme.example")

    assert delete_connector_account(info_id) is True
    remaining = _mail_rows(user_id, "mail_acme")
    assert {row._mapping["account_id"] for row in remaining} == {support_id}


def test_owner_check_blocks_foreign_row(user_id: UUID):
    # The state helper deletes unconditionally; the route enforces owner_id==user.
    # This guards the helper-level invariant the route relies on: get_connector_account
    # exposes owner_id so the route can reject a foreign row before deleting.
    settings = get_settings()
    _enable(settings)

    other = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other, display_name="Other"))
        s.commit()
    other_id = _register_mail(other, owner_addr="info@acme.example")

    row = get_connector_account(other_id)
    assert row is not None
    assert row._mapping["owner_id"] == other
    assert row._mapping["owner_id"] != user_id


@pytest.mark.parametrize("slug", ["mail_nova", "mail_acme"])
def test_config_requires_owner_addr(user_id: UUID, slug: str):
    settings = get_settings()
    _enable(settings)

    with pytest.raises(ValueError, match="owner_addr setting required"):
        configure_connector_account(
            slug,
            user_id,
            input_settings={"base_url": "https://x.test", "ingest_scope": "owner"},
            input_secrets={"api_key": "k", "api_token": "k"},
            account_kind="personal",
            owner_email="me@nova.example",
            actor_role="admin",
        )
