"""Owner-scoped collector-token connector-config endpoints.

These mirror the session `/connectors` PUT/POST/DELETE flow but authenticate with
an owner-bound `dct_` collector token and the `commands` scope, so the owner's
`orthus` CLI can manage personal connectors on the company node. They are
fail-closed: the same enable flags as every collector route, owner-bound to the
token's `user_id`, and a missing `commands` scope is 403.
"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.collector.auth import hash_collector_token
from orthus.collector.rate_limit import reset_collector_owner_rate_limits
from orthus.db import session
from orthus.secrets import clear_memory_secrets
from orthus.settings import get_settings
from orthus.tables import audit_log, collector_tokens, connector_accounts, users

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


def _make_token(user_id, *, node_id: str = COMPANY_NODE, scopes: list[str] | None = None) -> str:
    token = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    values = dict(
        token_id=uuid.uuid4(),
        user_id=user_id,
        node_id=node_id,
        name="test",
        token_hash=hash_collector_token(token),
    )
    if scopes is not None:
        values["scopes"] = scopes
    with session() as s:
        s.execute(insert(collector_tokens).values(**values))
        s.commit()
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- list -----------------------------------------------------------------------


def test_collector_connectors_list_is_owner_personal_only(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["commands"])
    other_user = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_user, display_name="Other"))
        s.execute(
            insert(connector_accounts).values(
                account_id=uuid.uuid4(),
                connector_slug="github",
                account_kind="personal",
                node_id=COMPANY_NODE,
                scope="personal",
                owner_id=user_id,
                auth_mode="token",
                status="active",
                settings_redacted={"repos": ["ExampleOrg/Orthus-AI"]},
            )
        )
        s.execute(
            insert(connector_accounts).values(
                account_id=uuid.uuid4(),
                connector_slug="github",
                account_kind="personal",
                node_id=COMPANY_NODE,
                scope="personal",
                owner_id=other_user,
                auth_mode="token",
                status="active",
                settings_redacted={"repos": ["Other/Repo"]},
            )
        )
        s.execute(
            insert(connector_accounts).values(
                account_id=uuid.uuid4(),
                connector_slug="slack",
                account_kind="company",
                node_id=COMPANY_NODE,
                scope="company",
                owner_id=None,
                auth_mode="token",
                status="active",
                settings_redacted={"channels": ["C123"]},
            )
        )
        s.commit()

    res = client.get("/collector/connectors", headers=_headers(token))

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["node_kind"] == "company"
    manifests = {m["slug"]: m for m in body["manifests"]}
    assert {"github", "local_files", "gws_gmail"}.issubset(manifests)
    assert "slack" not in manifests
    assert [a["connector_slug"] for a in body["accounts"]] == ["github"]
    assert body["accounts"][0]["owner_id"] == str(user_id)
    serialized = str(body)
    assert "Other/Repo" not in serialized
    assert "channels" not in serialized


# --- config write ---------------------------------------------------------------


def test_collector_config_write_matches_session_row_shape(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["commands"])

    res = client.put(
        "/collector/connectors/github/config",
        headers=_headers(token),
        json={
            "secrets": {"token": "ghp_collector_config_token"},
            "settings": {"repos": "example-org/orthus"},
        },
    )

    assert res.status_code == 200, res.text
    account = res.json()
    assert account["connector_slug"] == "github"
    assert account["account_kind"] == "personal"
    assert account["scope"] == "personal"
    assert account["node_id"] == COMPANY_NODE
    assert account["owner_id"] == str(user_id)
    assert account["settings_redacted"]["repos"] == ["example-org/orthus"]
    assert account["settings_redacted"]["config_source"] == "web"
    assert account["settings_redacted"]["token_configured"] is True
    # The secret value is never echoed back.
    assert "ghp_collector_config_token" not in json.dumps(account)

    # Same owner-bound DB row shape as the session PUT /{slug}/config write.
    with session() as s:
        rows = s.execute(
            select(
                connector_accounts.c.connector_slug,
                connector_accounts.c.account_kind,
                connector_accounts.c.scope,
                connector_accounts.c.node_id,
                connector_accounts.c.owner_id,
                connector_accounts.c.settings_redacted,
            ).where(connector_accounts.c.connector_slug == "github")
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.account_kind == "personal"
    assert row.scope == "personal"
    assert row.node_id == COMPANY_NODE
    assert row.owner_id == user_id
    assert row.settings_redacted["config_source"] == "web"
    assert row.settings_redacted["secret_refs"]["token"].startswith("orthus/connectors/")
    assert "ghp_collector_config_token" not in json.dumps(row.settings_redacted)
    clear_memory_secrets()


def test_collector_config_write_records_audit(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["commands"])

    res = client.put(
        "/collector/connectors/github/config",
        headers=_headers(token),
        json={
            "secrets": {"token": "ghp_audit_token"},
            "settings": {"repos": "example-org/orthus"},
        },
    )
    assert res.status_code == 200, res.text

    with session() as s:
        rows = s.execute(
            select(audit_log.c.phase, audit_log.c.meta).where(
                audit_log.c.node == "connector.command"
            )
        ).all()
    assert any(
        r.meta.get("action") == "config"
        and r.meta.get("connector_slug") == "github"
        and r.meta.get("scope") == "personal"
        and r.meta.get("user_id") == str(user_id)
        for r in rows
    )
    # The secret value must not appear in the audit meta.
    assert "ghp_audit_token" not in str([r.meta for r in rows])
    clear_memory_secrets()


def test_collector_config_unknown_slug_rejected(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["commands"])
    res = client.put(
        "/collector/connectors/not_a_connector/config",
        headers=_headers(token),
        json={"settings": {}, "secrets": {}},
    )
    assert res.status_code == 400


# --- ensure ---------------------------------------------------------------------


def test_collector_ensure_surfaces_personal_node_only_constraint(clean, user_id, monkeypatch):
    # `ensure_default_connector_account` for a personal connector is structurally
    # personal-node only (same constraint as the session POST /{slug}/ensure route),
    # so on the company owner-scope node it returns a clear 400 rather than a row.
    # The PUT /config path is the real config mechanism on the company node.
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["commands"])

    res = client.post("/collector/connectors/local_files/ensure", headers=_headers(token))

    assert res.status_code == 400
    assert "personal-node only" in res.json()["detail"]


# --- delete ---------------------------------------------------------------------


def test_collector_delete_owner_bound_and_foreign_404(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["commands"])
    owner_account = uuid.uuid4()
    other_user = uuid.uuid4()
    other_account = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=other_user, display_name="Other"))
        s.execute(
            insert(connector_accounts).values(
                account_id=owner_account,
                connector_slug="github",
                account_kind="personal",
                node_id=COMPANY_NODE,
                scope="personal",
                owner_id=user_id,
                auth_mode="token",
                status="active",
                settings_redacted={"repos": ["ExampleOrg/Orthus-AI"]},
            )
        )
        s.execute(
            insert(connector_accounts).values(
                account_id=other_account,
                connector_slug="github",
                account_kind="personal",
                node_id=COMPANY_NODE,
                scope="personal",
                owner_id=other_user,
                auth_mode="token",
                status="active",
                settings_redacted={"repos": ["Other/Repo"]},
            )
        )
        s.commit()

    # Deleting another owner's row 404s without leaking its existence.
    foreign = client.delete(
        f"/collector/connectors/github/accounts/{other_account}",
        headers=_headers(token),
    )
    assert foreign.status_code == 404

    owned = client.delete(
        f"/collector/connectors/github/accounts/{owner_account}",
        headers=_headers(token),
    )
    assert owned.status_code == 200
    assert owned.json() == {"account_id": str(owner_account), "deleted": True}

    with session() as s:
        remaining = {
            row.account_id for row in s.execute(select(connector_accounts.c.account_id)).all()
        }
    assert owner_account not in remaining
    assert other_account in remaining


# --- fail-closed boundaries -----------------------------------------------------


def test_collector_connector_endpoints_require_commands_scope(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id, scopes=["knowledge"])  # lacks commands
    assert client.get("/collector/connectors", headers=_headers(token)).status_code == 403
    assert (
        client.put(
            "/collector/connectors/github/config",
            headers=_headers(token),
            json={"settings": {}, "secrets": {}},
        ).status_code
        == 403
    )
    assert (
        client.post("/collector/connectors/github/ensure", headers=_headers(token)).status_code
        == 403
    )
    assert (
        client.delete(
            f"/collector/connectors/github/accounts/{uuid.uuid4()}",
            headers=_headers(token),
        ).status_code
        == 403
    )


def test_collector_connector_endpoints_flag_off_404(clean):
    # Flag off: the collector surface 404s before auth/DB work.
    dummy = _headers("dct_dummy")
    assert client.get("/collector/connectors", headers=dummy).status_code == 404
    assert (
        client.put(
            "/collector/connectors/github/config",
            headers=dummy,
            json={"settings": {}, "secrets": {}},
        ).status_code
        == 404
    )
    assert client.post("/collector/connectors/github/ensure", headers=dummy).status_code == 404
    assert (
        client.delete(
            f"/collector/connectors/github/accounts/{uuid.uuid4()}", headers=dummy
        ).status_code
        == 404
    )


def test_collector_connector_endpoints_reject_session_and_demo(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    # A demo X-User-Id (no collector bearer) never satisfies the token endpoints.
    res = client.get("/collector/connectors", headers={"X-User-Id": str(user_id)})
    assert res.status_code == 401
