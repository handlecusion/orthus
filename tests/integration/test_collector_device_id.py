"""Per-token device_id (migration 0070): multi-machine collector isolation.

One user account runs collector daemons on several machines (Mac mini + MacBook)
without collision. `device_id` is threaded server-side through connector accounts
and command routing; the daemon code is unchanged. `device_id == ""` reproduces
the exact pre-device behavior (backcompat).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.collector.auth import authenticate_collector_token, hash_collector_token
from orthus.collector.commands import create_command, list_pending_commands
from orthus.collector.rate_limit import reset_collector_owner_rate_limits
from orthus.connectors.account_config import account_settings, configure_connector_account
from orthus.connectors.state import ConnectorAccountSpec, deterministic_account_id
from orthus.db import session
from orthus.secrets import clear_memory_secrets
from orthus.settings import get_settings
from orthus.tables import collector_commands, collector_tokens, connector_accounts

client = TestClient(app)

COMPANY_NODE = "company"


def _enable(monkeypatch) -> None:
    reset_collector_owner_rate_limits()
    clear_memory_secrets()
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    monkeypatch.setattr(settings, "secret_backend", "memory")


def _make_token(
    user_id,
    *,
    node_id: str = COMPANY_NODE,
    device_id: str = "",
    scopes: list[str] | None = None,
) -> str:
    token = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    values = dict(
        token_id=uuid.uuid4(),
        user_id=user_id,
        node_id=node_id,
        name="test",
        token_hash=hash_collector_token(token),
        device_id=device_id,
    )
    if scopes is not None:
        values["scopes"] = scopes
    with session() as s:
        s.execute(insert(collector_tokens).values(**values))
        s.commit()
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _spec(device_id: str = "", *, owner_id) -> ConnectorAccountSpec:
    return ConnectorAccountSpec(
        connector_slug="local_files",
        account_kind="personal",
        node_id=COMPANY_NODE,
        auth_mode="local_path",
        owner_id=owner_id,
        device_id=device_id,
    )


# --- deterministic_account_id ---------------------------------------------------


def test_deterministic_account_id_empty_device_is_byte_identical(user_id):
    """device_id="" must hash to the same id as before the column existed."""
    owner = user_id
    legacy = ConnectorAccountSpec(
        connector_slug="local_files",
        account_kind="personal",
        node_id=COMPANY_NODE,
        auth_mode="local_path",
        owner_id=owner,
    )
    with_empty = _spec("", owner_id=owner)
    # Recompute the pre-0070 key by hand (no device part appended).
    expected = uuid.uuid5(
        uuid.UUID("1a65b6d6-5a52-4d19-b13d-4c6edcc637e1"),
        ":".join(["local_files", "personal", COMPANY_NODE, str(owner), "", "local_path"]),
    )
    assert deterministic_account_id(legacy) == expected
    assert deterministic_account_id(with_empty) == expected


def test_deterministic_account_id_distinct_per_device(user_id):
    owner = user_id
    none_id = deterministic_account_id(_spec("", owner_id=owner))
    mini_id = deterministic_account_id(_spec("macmini", owner_id=owner))
    book_id = deterministic_account_id(_spec("macbook", owner_id=owner))
    assert len({none_id, mini_id, book_id}) == 3


# --- configure_connector_account (independent settings per device) ---------------


def test_configure_connector_account_per_device_no_overwrite(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    settings = get_settings()
    mini = configure_connector_account(
        "local_files",
        user_id,
        input_settings={"roots": "~/mini-docs"},
        input_secrets={},
        settings=settings,
        account_kind="personal",
        device_id="macmini",
    )
    book = configure_connector_account(
        "local_files",
        user_id,
        input_settings={"roots": "~/book-docs"},
        input_secrets={},
        settings=settings,
        account_kind="personal",
        device_id="macbook",
    )
    assert mini != book
    with session() as s:
        rows = s.execute(
            select(
                connector_accounts.c.account_id,
                connector_accounts.c.device_id,
                connector_accounts.c.settings_redacted,
            ).where(connector_accounts.c.connector_slug == "local_files")
        ).all()
    by_account = {row.account_id: row for row in rows}
    assert len(by_account) == 2
    assert account_settings(dict(by_account[mini]._mapping))["roots"] == ["~/mini-docs"]
    assert account_settings(dict(by_account[book]._mapping))["roots"] == ["~/book-docs"]
    assert by_account[mini].device_id == "macmini"
    assert by_account[book].device_id == "macbook"


# --- auth -----------------------------------------------------------------------


def test_authenticate_collector_token_returns_device_id(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    settings = get_settings()
    token = _make_token(user_id, device_id="macmini", scopes=["commands"])
    resolved = authenticate_collector_token(f"Bearer {token}", settings)
    assert resolved.device_id == "macmini"
    assert resolved.user_id == user_id

    legacy = _make_token(user_id, device_id="", scopes=["commands"])
    assert authenticate_collector_token(f"Bearer {legacy}", settings).device_id == ""


# --- command routing ------------------------------------------------------------


def test_list_pending_commands_device_isolation(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    settings = get_settings()
    create_command(
        settings,
        user_id=user_id,
        created_by=user_id,
        kind="connector_sync",
        payload={"connector": "local_files", "scope": "broadcast"},
        device_id=None,
    )
    create_command(
        settings,
        user_id=user_id,
        created_by=user_id,
        kind="connector_sync",
        payload={"connector": "local_files", "scope": "mini"},
        device_id="macmini",
    )
    create_command(
        settings,
        user_id=user_id,
        created_by=user_id,
        kind="connector_sync",
        payload={"connector": "local_files", "scope": "book"},
        device_id="macbook",
    )

    def scopes(result) -> set[str]:
        return {item.payload.get("scope") for item in result.items}

    # Device token: broadcast + own only.
    assert scopes(list_pending_commands(user_id, "macmini")) == {"broadcast", "mini"}
    assert scopes(list_pending_commands(user_id, "macbook")) == {"broadcast", "book"}
    # Deviceless token (None/""): broadcast only.
    assert scopes(list_pending_commands(user_id, None)) == {"broadcast"}
    assert scopes(list_pending_commands(user_id, "")) == {"broadcast"}


def test_create_command_coalesce_respects_device(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    settings = get_settings()
    payload = {"connector": "local_files"}
    broadcast = create_command(
        settings,
        user_id=user_id,
        created_by=user_id,
        kind="connector_sync",
        payload=payload,
        device_id=None,
    )
    # Same payload but device-targeted: must NOT coalesce with the broadcast row.
    targeted = create_command(
        settings,
        user_id=user_id,
        created_by=user_id,
        kind="connector_sync",
        payload=payload,
        device_id="macmini",
    )
    assert broadcast.command_id != targeted.command_id
    assert broadcast.device_id is None
    assert targeted.device_id == "macmini"
    # A second device-targeted enqueue with the same device DOES coalesce.
    again = create_command(
        settings,
        user_id=user_id,
        created_by=user_id,
        kind="connector_sync",
        payload=payload,
        device_id="macmini",
    )
    assert again.command_id == targeted.command_id
    with session() as s:
        count = s.execute(
            select(collector_commands.c.command_id).where(collector_commands.c.user_id == user_id)
        ).all()
    assert len(count) == 2  # one broadcast + one macmini


# --- token issue device_id generation -------------------------------------------


def _session_settings(monkeypatch, admin_email: str) -> None:
    reset_collector_owner_rate_limits()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_public_base_url", "http://localhost:8820")
    monkeypatch.setattr(settings, "auth_web_base_url", "http://localhost:3820")
    monkeypatch.setattr(settings, "auth_cookie_name", "orthus_session_company")
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", True)
    monkeypatch.setattr(settings, "central_admin_emails", admin_email)
    monkeypatch.setattr(settings, "collector_api_enabled", True)


def test_issue_token_device_id_generation_disambiguates(clean, monkeypatch):
    admin_email = "admin@acme.example"
    _session_settings(monkeypatch, admin_email)
    local_client = TestClient(app)
    assert local_client.post("/auth/dev-login", json={"email": admin_email}).status_code == 200

    first = local_client.post(
        "/collector/tokens", json={"scopes": ["commands"], "device_label": "Mac mini"}
    )
    assert first.status_code == 200, first.text
    assert first.json()["item"]["device_id"] == "mac-mini"

    second = local_client.post(
        "/collector/tokens", json={"scopes": ["commands"], "device_label": "Mac mini"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["item"]["device_id"] == "mac-mini-2"


def test_partial_unique_index_blocks_duplicate_active_device(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    _make_token(user_id, device_id="macmini", scopes=["commands"])
    with pytest.raises(Exception):  # IntegrityError surfaces as a DBAPIError
        with session() as s:
            s.execute(
                insert(collector_tokens).values(
                    token_id=uuid.uuid4(),
                    user_id=user_id,
                    node_id=COMPANY_NODE,
                    name="dup",
                    token_hash=hash_collector_token(f"dct_{uuid.uuid4().hex}"),
                    scopes=["commands"],
                    device_id="macmini",
                )
            )
            s.commit()


def test_legacy_empty_device_tokens_do_not_collide(clean, user_id):
    """device_id='' rows are excluded from the partial unique index."""
    with session() as s:
        for _ in range(2):
            s.execute(
                insert(collector_tokens).values(
                    token_id=uuid.uuid4(),
                    user_id=user_id,
                    node_id=COMPANY_NODE,
                    name="legacy",
                    token_hash=hash_collector_token(f"dct_{uuid.uuid4().hex}"),
                    scopes=["ingest"],
                    device_id="",
                )
            )
        s.commit()
        count = s.execute(
            select(collector_tokens.c.token_id).where(collector_tokens.c.user_id == user_id)
        ).all()
    assert len(count) == 2


# --- /collector/config device scoping -------------------------------------------


def test_collector_config_returns_only_device_accounts(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    settings = get_settings()
    configure_connector_account(
        "local_files",
        user_id,
        input_settings={"roots": "~/mini-docs"},
        input_secrets={},
        settings=settings,
        account_kind="personal",
        device_id="macmini",
    )
    configure_connector_account(
        "local_files",
        user_id,
        input_settings={"roots": "~/book-docs"},
        input_secrets={},
        settings=settings,
        account_kind="personal",
        device_id="macbook",
    )

    mini_token = _make_token(user_id, device_id="macmini", scopes=["commands"])
    res = client.get("/collector/config", headers=_headers(mini_token))
    assert res.status_code == 200, res.text
    accounts = res.json()["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["connector_slug"] == "local_files"
    assert accounts[0]["settings"]["roots"] == ["~/mini-docs"]
    clear_memory_secrets()
