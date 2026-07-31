"""Integration tests for the collector WebSocket notify channel (slice 8).

The endpoint is fail-closed behind `collector_ws_enabled` + company node +
`collector_api_enabled`, authenticates the same `dct_` collector token (needs the
`commands` scope, which `ingest` implies), and pushes a `command_available`
envelope when `create_command` enqueues for that owner. The HTTP claim/complete
path is untouched; this only removes poll latency.

These tests open the WS inside `with TestClient(app) as c:` so the app lifespan
runs and `WS_REGISTRY.set_loop(...)` captures the portal loop — without that the
cross-thread notify is a no-op.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert
from starlette.websockets import WebSocketDisconnect

from orthus.api.main import app
from orthus.collector.auth import hash_collector_token
from orthus.collector.commands import create_command
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import collector_tokens, users

COMPANY_NODE = "company"


def _enable_ws(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "collector_ws_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)


def _make_token(
    user_id,
    *,
    node_id: str = COMPANY_NODE,
    revoked: bool = False,
    scopes: list[str] | None = None,
) -> str:
    token = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    values = dict(
        token_id=uuid.uuid4(),
        user_id=user_id,
        node_id=node_id,
        name="ws-test",
        token_hash=hash_collector_token(token),
        revoked_at="2026-01-01T00:00:00Z" if revoked else None,
    )
    if scopes is not None:
        values["scopes"] = scopes
    with session() as s:
        s.execute(insert(collector_tokens).values(**values))
        s.commit()
    return token


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="WS Owner"))
        s.commit()
    return uid


def test_ws_receives_command_available_on_enqueue(clean, monkeypatch):
    _enable_ws(monkeypatch)
    owner_id = _make_user()
    token = _make_token(owner_id)  # default DB scope {ingest} implies commands
    settings = get_settings()

    with TestClient(app) as c:
        with c.websocket_connect(
            "/collector/ws", headers={"Authorization": f"Bearer {token}"}
        ) as ws:
            command = create_command(
                settings,
                user_id=owner_id,
                created_by=owner_id,
                kind="connector_sync",
                payload={"connector": "gws_gmail"},
            )
            message = ws.receive_json()
            assert message["type"] == "command_available"
            assert message["command_id"] == str(command.command_id)
            assert message["kind"] == "connector_sync"


def test_ws_only_notifies_owning_connection(clean, monkeypatch):
    _enable_ws(monkeypatch)
    owner_id = _make_user()
    other_id = _make_user()
    owner_token = _make_token(owner_id)
    settings = get_settings()

    with TestClient(app) as c:
        with c.websocket_connect(
            "/collector/ws", headers={"Authorization": f"Bearer {owner_token}"}
        ) as ws:
            # Enqueue for a DIFFERENT owner: the connected owner must not see it.
            create_command(
                settings,
                user_id=other_id,
                created_by=other_id,
                kind="connector_sync",
                payload={"connector": "gws_gmail"},
            )
            # Then enqueue for the connected owner: that one arrives.
            mine = create_command(
                settings,
                user_id=owner_id,
                created_by=owner_id,
                kind="connector_sync",
                payload={"connector": "local_files"},
            )
            message = ws.receive_json()
            assert message["command_id"] == str(mine.command_id)


def test_ws_flag_off_refuses_connection(clean, monkeypatch):
    # collector_api on but ws flag off -> handshake closed before accept.
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "collector_ws_enabled", False)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    owner_id = _make_user()
    token = _make_token(owner_id)

    with TestClient(app) as c:
        try:
            with c.websocket_connect(
                "/collector/ws", headers={"Authorization": f"Bearer {token}"}
            ) as ws:
                ws.receive_json()
            raise AssertionError("expected the disabled WS handshake to be refused")
        except WebSocketDisconnect as exc:
            assert exc.code == 4404


def test_ws_personal_node_refuses_connection(clean, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "collector_ws_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    owner_id = _make_user()
    token = _make_token(owner_id, node_id="personal-a")

    with TestClient(app) as c:
        try:
            with c.websocket_connect(
                "/collector/ws", headers={"Authorization": f"Bearer {token}"}
            ) as ws:
                ws.receive_json()
            raise AssertionError("expected the personal-node WS handshake to be refused")
        except WebSocketDisconnect as exc:
            assert exc.code == 4404


def test_ws_invalid_token_refused(clean, monkeypatch):
    _enable_ws(monkeypatch)
    with TestClient(app) as c:
        try:
            with c.websocket_connect(
                "/collector/ws",
                headers={"Authorization": "Bearer dct_not_a_real_token"},
            ) as ws:
                ws.receive_json()
            raise AssertionError("expected an invalid token to be refused")
        except WebSocketDisconnect as exc:
            assert exc.code == 4401


def test_ws_missing_auth_header_refused(clean, monkeypatch):
    _enable_ws(monkeypatch)
    with TestClient(app) as c:
        try:
            with c.websocket_connect("/collector/ws") as ws:
                ws.receive_json()
            raise AssertionError("expected a missing auth header to be refused")
        except WebSocketDisconnect as exc:
            assert exc.code == 4401


def test_ws_revoked_token_refused(clean, monkeypatch):
    _enable_ws(monkeypatch)
    owner_id = _make_user()
    token = _make_token(owner_id, revoked=True)
    with TestClient(app) as c:
        try:
            with c.websocket_connect(
                "/collector/ws", headers={"Authorization": f"Bearer {token}"}
            ) as ws:
                ws.receive_json()
            raise AssertionError("expected a revoked token to be refused")
        except WebSocketDisconnect as exc:
            assert exc.code == 4401


def test_ws_token_without_commands_scope_refused(clean, monkeypatch):
    _enable_ws(monkeypatch)
    owner_id = _make_user()
    # knowledge scope does not imply commands -> 4403.
    token = _make_token(owner_id, scopes=["knowledge"])
    with TestClient(app) as c:
        try:
            with c.websocket_connect(
                "/collector/ws", headers={"Authorization": f"Bearer {token}"}
            ) as ws:
                ws.receive_json()
            raise AssertionError("expected a non-commands-scope token to be refused")
        except WebSocketDisconnect as exc:
            assert exc.code == 4403


def test_ws_disconnect_unregisters_connection(clean, monkeypatch):
    # After the client disconnects, a later enqueue for that owner must not raise
    # and must find no live connection (queue was unregistered in finally).
    _enable_ws(monkeypatch)
    owner_id = _make_user()
    token = _make_token(owner_id)
    settings = get_settings()

    from orthus.collector.ws_registry import WS_REGISTRY

    with TestClient(app) as c:
        with c.websocket_connect("/collector/ws", headers={"Authorization": f"Bearer {token}"}):
            pass  # immediately close
        # The enqueue notify is a safe no-op once the connection is gone.
        create_command(
            settings,
            user_id=owner_id,
            created_by=owner_id,
            kind="connector_sync",
            payload={"connector": "gws_gmail"},
        )
        assert (COMPANY_NODE, owner_id) not in WS_REGISTRY._queues
