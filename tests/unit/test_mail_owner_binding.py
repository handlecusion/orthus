"""P6.7.4 Model B tiered mail mailbox registration auth (`docs/p6-unified-mail.md`
§12.11 개정 3).

Replaces the P6.7.2 single owner_addr==email binding. Rules, fail-closed:
- owner_addr domain MUST be @nova.example / @acme.example, else reject.
- owner/admin may register ANY company address (multiple).
- a regular member may register only an address == their verified email.
- JWT/demo/collector (email is None) fails closed.
Non-mail connectors are unaffected.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from sqlalchemy import insert

from orthus.connectors.account_config import configure_connector_account
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import auth_identities, users


def _enable(settings) -> None:
    settings.owner_scope_enabled = True
    settings.node_kind = "company"
    settings.node_id = "company"


def _configure(
    owner_id: UUID,
    *,
    owner_addr: str,
    owner_email: str | None,
    actor_role: str | None = None,
    slug: str = "mail_nova",
) -> UUID:
    return configure_connector_account(
        slug,
        owner_id,
        input_settings={
            "base_url": "https://row.mail.test",
            "owner_addr": owner_addr,
            "ingest_scope": "owner",
        },
        input_secrets={"api_key": "row-nova-key"},
        account_kind="personal",
        owner_email=owner_email,
        actor_role=actor_role,
    )


def test_member_registers_own_email_ok(user_id: UUID):
    settings = get_settings()
    _enable(settings)

    account_id = _configure(
        user_id,
        owner_addr="me@nova.example",
        owner_email="me@nova.example",
        actor_role="member",
    )

    assert account_id is not None


def test_member_other_company_address_rejects(user_id: UUID):
    settings = get_settings()
    _enable(settings)

    with pytest.raises(ValueError, match="owner_addr must match"):
        _configure(
            user_id,
            owner_addr="someone-else@nova.example",
            owner_email="me@nova.example",
            actor_role="member",
        )


def test_non_company_domain_rejects(user_id: UUID):
    settings = get_settings()
    _enable(settings)

    # An owner/admin still cannot register a non-company domain.
    with pytest.raises(ValueError, match="company mail domain"):
        _configure(
            user_id,
            owner_addr="me@gmail.com",
            owner_email="me@gmail.com",
            actor_role="admin",
        )


@pytest.mark.parametrize(
    ("slug", "owner_addr", "expected"),
    [
        ("mail_nova", "me@acme.example", "@nova.example"),
        ("mail_acme", "me@nova.example", "@acme.example"),
    ],
)
def test_mail_slug_must_match_owner_addr_domain(
    user_id: UUID,
    slug: str,
    owner_addr: str,
    expected: str,
):
    settings = get_settings()
    _enable(settings)

    with pytest.raises(ValueError, match=expected):
        _configure(
            user_id,
            owner_addr=owner_addr,
            owner_email="boss@acme.example",
            actor_role="admin",
            slug=slug,
        )


def test_owner_email_none_fails_closed(user_id: UUID):
    settings = get_settings()
    _enable(settings)

    # Even an owner role with no verified email (JWT/demo/collector) fails closed.
    with pytest.raises(ValueError, match="verified session email"):
        _configure(
            user_id,
            owner_addr="me@nova.example",
            owner_email=None,
            actor_role="owner",
        )


def test_admin_registers_arbitrary_company_address(user_id: UUID):
    settings = get_settings()
    _enable(settings)

    # admin email differs from the mailbox address; the manager tier allows it.
    account_id = _configure(
        user_id,
        owner_addr="info@nova.example",
        owner_email="boss@nova.example",
        actor_role="admin",
    )

    assert account_id is not None


def test_admin_cannot_register_another_verified_users_mailbox(user_id: UUID):
    settings = get_settings()
    _enable(settings)
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

    with pytest.raises(ValueError, match="another verified user"):
        _configure(
            user_id,
            owner_addr="other@nova.example",
            owner_email="boss@nova.example",
            actor_role="admin",
        )


def test_owner_binding_is_case_insensitive(user_id: UUID):
    settings = get_settings()
    _enable(settings)

    account_id = _configure(
        user_id,
        owner_addr="Me@Nova.EXAMPLE",
        owner_email="me@nova.example",
        actor_role="member",
    )

    assert account_id is not None


def test_non_mail_connector_does_not_require_owner_email(user_id: UUID):
    settings = get_settings()
    _enable(settings)

    # github is a personal connector with no mail identity binding; owner_email and
    # actor_role None are both fine.
    account_id = configure_connector_account(
        "github",
        user_id,
        input_settings={"repos": "owner/repo"},
        input_secrets={"token": "ghp_x"},
        account_kind="personal",
        owner_email=None,
        actor_role=None,
    )

    assert account_id is not None
