"""P6.7.3 pull account-loop.

With `ORTHUS_MAIL_MULTI_ACCOUNT_ENABLED` on, `pull_ingest_all` iterates every
active mail account row across owners (a system/background job), and each
account is routed by its own config and `ingest_scope`
(`docs/p6-unified-mail.md` §12.5 / §12.2).
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy import insert, select

from orthus.connectors.account_config import configure_connector_account
from orthus.db import session
from orthus.mail import pull as pull_module
from orthus.mail.pull import pull_ingest_account, pull_ingest_all
from orthus.settings import get_settings
from orthus.tables import connector_accounts, documents, users, wiki_pages

NOVA_INBOX = {
    "total": 1,
    "unread": 1,
    "emails": [
        {
            "id": "nova-msg-1",
            "message_id": "<nova-msg-1@nova.example>",
            "from_addr": "Lead <lead@example.com>",
            "to_addr": ["a@nova.example"],
            "subject": "Nova inbound",
            "created_at": "2026-06-10T10:00:00Z",
        }
    ],
}
NOVA_MESSAGE = {
    "id": "nova-msg-1",
    "message_id": "<nova-msg-1@nova.example>",
    "from_addr": "Lead <lead@example.com>",
    "to_addr": ["a@nova.example"],
    "subject": "Nova inbound",
    "body_text": "Nova body kept unredacted per mail ingest.",
    "created_at": "2026-06-10T10:00:00Z",
}
DZ_INBOX = {
    "total": 1,
    "unread": 0,
    "emails": [
        {
            "id": "dz-msg-1",
            "message_id": "<dz-msg-1@acme.example>",
            "from_addr": "Partner <partner@example.com>",
            "to_addr": ["b@acme.example"],
            "subject": "DZ inbound",
            "received_at": "2026-06-10T11:00:00Z",
        }
    ],
}
DZ_MESSAGE = {
    "email": {
        "id": "dz-msg-1",
        "message_id": "<dz-msg-1@acme.example>",
        "from_addr": "Partner <partner@example.com>",
        "to_addr": ["b@acme.example"],
        "subject": "DZ inbound",
        "body_text": "DZ body.",
        "received_at": "2026-06-10T11:00:00Z",
    }
}


def _client(inbox: dict, message: dict, message_path: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/mail/inbox"):
            return httpx.Response(200, json=inbox)
        if request.url.path.endswith(message_path):
            return httpx.Response(200, json=message)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _new_user(name: str) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name=name))
        s.commit()
    return uid


def _register(slug, owner_id, owner_addr, secret_key, ingest_scope) -> None:
    settings = get_settings()
    prior = settings.owner_scope_enabled
    settings.owner_scope_enabled = True
    try:
        configure_connector_account(
            slug,
            owner_id,
            input_settings={
                "base_url": "https://mail.test",
                "owner_addr": owner_addr,
                "ingest_scope": ingest_scope,
            },
            input_secrets={secret_key: "row-secret"},
            account_kind="personal",
            owner_email=owner_addr,
        )
    finally:
        settings.owner_scope_enabled = prior


def test_pull_all_iterates_account_rows_routed_by_ingest_scope(clean, user_id, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_multi_account_enabled", True)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "mail_pull_ingest_enabled", True)
    monkeypatch.setattr(settings, "mail_ingest_service_user_id", str(user_id))

    owner_a = user_id  # nova mailbox -> owner-scope
    owner_b = _new_user("Owner B")  # acme mailbox -> company-scope
    _register("mail_nova", owner_a, "a@nova.example", "api_key", "owner")
    _register("mail_acme", owner_b, "b@acme.example", "api_token", "company")

    clients = {
        "nova": _client(NOVA_INBOX, NOVA_MESSAGE, "/mail/nova-msg-1"),
        "acme": _client(DZ_INBOX, DZ_MESSAGE, "/mail/dz-msg-1"),
    }
    real = pull_ingest_account

    def routed(account, s, **kwargs):
        slug = str(account.get("connector_slug"))
        backend = "nova" if slug == "mail_nova" else "acme"
        kwargs.setdefault("client", clients[backend])
        return real(account, s, **kwargs)

    monkeypatch.setattr(pull_module, "pull_ingest_account", routed)

    results = pull_ingest_all(settings)

    by_backend = {r.backend: r for r in results}
    assert set(by_backend) == {"nova", "acme"}
    assert by_backend["nova"].ingested == 1
    assert by_backend["acme"].ingested == 1

    with session() as s:
        nova_doc = s.execute(
            select(documents.c.scope, documents.c.user_id).where(documents.c.source == "mail_nova")
        ).first()
        dz_doc = s.execute(
            select(documents.c.scope, documents.c.user_id).where(
                documents.c.source == "mail_acme"
            )
        ).first()
        owner_pages = s.execute(
            select(wiki_pages.c.owner_id).where(wiki_pages.c.scope == "personal")
        ).all()
        company_pages = s.execute(
            select(wiki_pages.c.owner_id).where(wiki_pages.c.scope == "company")
        ).all()

    # nova mailbox routed owner-scope -> personal + owner_a; dz routed company.
    assert nova_doc.scope == "personal"
    assert nova_doc.user_id == owner_a
    assert dz_doc.scope == "company"
    assert owner_pages and all(owner_id == owner_a for (owner_id,) in owner_pages)
    assert company_pages and all(owner_id is None for (owner_id,) in company_pages)


def test_pull_all_flag_on_zero_rows_no_env_fallback(clean, user_id, monkeypatch):
    """Flag ON, no account rows: nothing ingested, no env single-account fallback."""
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_multi_account_enabled", True)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "mail_pull_ingest_enabled", True)
    # Env single-account values are present and would ingest if we fell through.
    monkeypatch.setattr(settings, "mail_nova_base_url", "https://env.nova.test")
    monkeypatch.setattr(settings, "mail_nova_api_key", "env-key")
    monkeypatch.setattr(settings, "mail_nova_owner", "envowner@nova.example")

    results = pull_ingest_all(settings)

    assert results == []
    with session() as s:
        rows = s.execute(select(documents.c.doc_id)).all()
    assert rows == []


def test_pull_all_flag_off_uses_env_single_account(clean, user_id, monkeypatch):
    """Flag OFF: byte-identical legacy env path (no account-row enumeration)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_multi_account_enabled", False)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_pull_ingest_enabled", False)

    # Even with an account row present, flag-off pull must ignore rows entirely
    # and return the env single-account backend list (both disabled -> enabled False).
    _register("mail_nova", user_id, "a@nova.example", "api_key", "owner")

    results = pull_ingest_all(settings)

    assert [r.backend for r in results] == ["nova", "acme"]
    assert all(r.enabled is False for r in results)
    with session() as s:
        rows = s.execute(select(documents.c.doc_id)).all()
    assert rows == []
    # The account row stays untouched by the env path.
    with session() as s:
        acct = s.execute(
            select(connector_accounts.c.account_id).where(
                connector_accounts.c.connector_slug == "mail_nova"
            )
        ).all()
    assert len(acct) == 1
