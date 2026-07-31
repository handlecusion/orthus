"""P10 mail read dual-auth: knowledge token reads ONLY its own mailboxes.

`GET /mail/inbox` and `GET /mail/message` accept the P8.7b dual-auth (session
OR knowledge-scoped collector token) so the gateway agent's `mail_list`/
`mail_get` tools can read the already-ingested company mail. The heart of this
suite is the owner boundary: token A must never enumerate or fetch owner B's
mailbox rows (P6.7.2 §12.4-2 isolation applied to the token's `user_id`).
Everything else on `/mail` (patch/trash/send/attachment) stays session-only.
"""

from __future__ import annotations

import uuid

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import insert

from orthus.api.main import app
from orthus.api.routes import mail as mail_routes
from orthus.collector.auth import hash_collector_token
from orthus.connectors.account_config import configure_connector_account
from orthus.db import session
from orthus.mail.backends import MailBackendConfig
from orthus.schemas.canonical import CanonicalEmail, MailInboxResponse
from orthus.settings import get_settings
from orthus.tables import collector_tokens, users

client = TestClient(app)

COMPANY_NODE = "company"
DEMO_USER_HEADERS = {"X-User-Id": "00000000-0000-4000-8000-000000000001"}


def _enable(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)


def _make_token(user_id, *, scopes: list[str], node_id: str = COMPANY_NODE) -> str:
    token = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=node_id,
                name="mail-test",
                token_hash=hash_collector_token(token),
                scopes=scopes,
            )
        )
        s.commit()
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _configure_mail_account(owner_id, *, owner_addr: str, base_url: str) -> None:
    configure_connector_account(
        "mail_nova",
        owner_id,
        input_settings={"base_url": base_url, "owner_addr": owner_addr, "ingest_scope": "owner"},
        input_secrets={"api_key": f"{owner_addr}-key"},
        account_kind="personal",
        owner_email=owner_addr,
    )


def _second_owner() -> uuid.UUID:
    other = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other, display_name="Owner B"))
        s.commit()
    return other


def _record_backend_fetches(monkeypatch) -> list[str]:
    """Route every provider HTTP call through a MockTransport, recording the
    `owner` param each backend fetch carries (whose mailbox got enumerated)."""
    fetched_owners: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched_owners.append(request.url.params.get("owner", ""))
        return httpx.Response(200, json={"emails": [], "total": 0, "unread": 0})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _patched)
    return fetched_owners


# --- dual-auth surface ---------------------------------------------------------


def test_inbox_knowledge_token_binds_to_token_owner(user_id, monkeypatch):
    """Token path is bound to token.user_id — the same owner_id the session path
    passes into `list_unified_inbox`."""
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["knowledge"])
    seen_owner_ids = []

    async def fake_inbox(_settings, *, owner_id, **_kwargs):
        seen_owner_ids.append(owner_id)
        return MailInboxResponse(items=[], total=0, unread=0, backends=[])

    monkeypatch.setattr(mail_routes, "list_unified_inbox", fake_inbox)

    response = client.get("/mail/inbox", headers=_headers(token))

    assert response.status_code == 200
    assert seen_owner_ids == [user_id]


def test_inbox_session_path_unchanged(user_id, monkeypatch):
    """Demo/session identity still reads the inbox exactly as before."""
    _enable(monkeypatch)
    seen_owner_ids = []

    async def fake_inbox(_settings, *, owner_id, **_kwargs):
        seen_owner_ids.append(owner_id)
        return MailInboxResponse(items=[], total=0, unread=0, backends=[])

    monkeypatch.setattr(mail_routes, "list_unified_inbox", fake_inbox)

    response = client.get("/mail/inbox", headers=DEMO_USER_HEADERS)

    assert response.status_code == 200
    assert len(seen_owner_ids) == 1


# --- owner boundary (the heart) --------------------------------------------------


def test_token_a_never_enumerates_owner_b_mailbox(user_id, monkeypatch):
    """Multi-account on: token A's inbox fan-out fetches ONLY A's mailbox rows.

    Owner B's personal (ingest_scope=owner) mailbox address must never appear in
    any provider request made on behalf of token A."""
    _enable(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_multi_account_enabled", True)
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)
    other = _second_owner()
    _configure_mail_account(user_id, owner_addr="a@nova.example", base_url="https://a.nova.test")
    _configure_mail_account(other, owner_addr="b@nova.example", base_url="https://b.nova.test")
    fetched_owners = _record_backend_fetches(monkeypatch)
    token_a = _make_token(user_id, scopes=["knowledge"])

    response = client.get("/mail/inbox", headers=_headers(token_a))

    assert response.status_code == 200
    assert set(fetched_owners) == {"a@nova.example"}
    assert "b@nova.example" not in fetched_owners
    assert {b["backend"] for b in response.json()["backends"]} == {"nova"}


def test_token_b_sees_only_its_own_mailbox(user_id, monkeypatch):
    _enable(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "mail_multi_account_enabled", True)
    monkeypatch.setattr("orthus.mail.backends.gws_command_available", lambda _command: False)
    other = _second_owner()
    _configure_mail_account(user_id, owner_addr="a@nova.example", base_url="https://a.nova.test")
    _configure_mail_account(other, owner_addr="b@nova.example", base_url="https://b.nova.test")
    fetched_owners = _record_backend_fetches(monkeypatch)
    token_b = _make_token(other, scopes=["knowledge"])

    response = client.get("/mail/inbox", headers=_headers(token_b))

    assert response.status_code == 200
    assert set(fetched_owners) == {"b@nova.example"}
    assert "a@nova.example" not in fetched_owners


def test_message_token_resolves_config_with_token_owner(user_id, monkeypatch):
    """`GET /mail/message` under a token resolves the backend config with the
    token's user_id (owner-isolated resolve), not any other identity."""
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["knowledge"])
    seen: dict = {}

    def fake_resolve(_settings, backend, *, owner_id=None, account_id=None):
        seen.update(backend=backend, owner_id=owner_id)
        return MailBackendConfig(
            backend="acme",
            base_url="https://mail.example.test",
            owner="biz@acme.example",
            bearer_token="x",
        )

    async def fake_detail(_cfg, external_id, _client):
        return CanonicalEmail(
            backend="acme",
            external_id=external_id,
            scope="company",
            from_addr="lead@acme.example",
            to_addr=["biz@acme.example"],
            subject="hello",
            body_text="full body",
        )

    monkeypatch.setattr(mail_routes, "resolve_backend_config", fake_resolve)
    monkeypatch.setattr(mail_routes, "fetch_backend_email_detail", fake_detail)

    response = client.get("/mail/message?backend=acme&id=abc123", headers=_headers(token))

    assert response.status_code == 200
    assert response.json()["body_text"] == "full body"
    assert seen["owner_id"] == user_id


# --- rejects ---------------------------------------------------------------------


def test_token_without_knowledge_scope_403(user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["commands"])

    response = client.get("/mail/inbox", headers=_headers(token))

    assert response.status_code == 403


def test_invalid_token_401(user_id, monkeypatch):
    _enable(monkeypatch)

    response = client.get("/mail/inbox", headers=_headers("dct_notarealtoken"))

    assert response.status_code == 401


def test_flag_off_token_rejected_401(user_id, monkeypatch):
    """Collector API disabled (fail-closed default): a dct_ bearer is treated as
    an invalid credential; the pre-PR session contract is untouched."""
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", False)
    token = _make_token(user_id, scopes=["knowledge"])

    response = client.get("/mail/inbox", headers=_headers(token))

    assert response.status_code == 401


def test_anonymous_session_still_401(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")

    response = client.get("/mail/inbox")

    assert response.status_code == 401


def test_mutation_routes_stay_session_only(user_id, monkeypatch):
    """A knowledge token must not authenticate mail mutation (PATCH flags): the
    write side of /mail keeps `get_current_user` (no token path at all)."""
    _enable(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    token = _make_token(user_id, scopes=["knowledge", "knowledge:write"])

    response = client.patch(
        "/mail/message?backend=acme&id=abc123",
        json={"read": True},
        headers=_headers(token),
    )

    assert response.status_code == 401
