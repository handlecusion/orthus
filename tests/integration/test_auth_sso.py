"""Cross-node SSO session bridge (hub issues tickets, RP consumes them)."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from orthus.api.main import app
from orthus.auth import (
    decode_hs256_jwt,
    mint_sso_ticket,
    self_api_origin,
    sign_hs256_jwt,
    verify_and_consume_sso_ticket,
)
from orthus.settings import Settings, get_settings

SHARED = "test-sso-shared-secret"


# --- ticket primitives (direct calls) ---------------------------------------


def _hub() -> Settings:
    return Settings(
        sso_shared_secret=SHARED,
        auth_public_base_url="http://localhost:8820",
        node_id="company",
    )


def _rp(public_base: str = "http://localhost:8830") -> Settings:
    return Settings(
        sso_shared_secret=SHARED,
        auth_public_base_url=public_base,
        node_id="personal-a",
    )


def test_sso_ticket_roundtrip_then_replay_rejected(clean):
    rp = _rp()
    aud = self_api_origin(rp)
    ticket = mint_sso_ticket("owner@acme.example", "sub-1", aud, _hub())

    assert verify_and_consume_sso_ticket(ticket, rp) == "owner@acme.example"

    with pytest.raises(HTTPException) as exc:
        verify_and_consume_sso_ticket(ticket, rp)
    assert exc.value.status_code == 401
    assert exc.value.detail == "sso ticket replay"


def test_sso_ticket_rejects_wrong_audience(clean):
    rp = _rp()
    ticket = mint_sso_ticket("owner@acme.example", "sub-1", "http://localhost:9999", _hub())

    with pytest.raises(HTTPException) as exc:
        verify_and_consume_sso_ticket(ticket, rp)
    assert exc.value.status_code == 401
    assert exc.value.detail == "sso audience invalid"


def test_sso_ticket_rejects_bad_signature(clean):
    rp = _rp()
    aud = self_api_origin(rp)
    ticket = mint_sso_ticket("owner@acme.example", "sub-1", aud, _hub())
    forged_rp = Settings(
        sso_shared_secret="different-secret",
        auth_public_base_url="http://localhost:8830",
        node_id="personal-a",
    )

    with pytest.raises(HTTPException) as exc:
        verify_and_consume_sso_ticket(ticket, forged_rp)
    assert exc.value.status_code == 401
    assert exc.value.detail == "jwt signature invalid"


def test_sso_ticket_rejects_expired(clean):
    rp = _rp()
    aud = self_api_origin(rp)
    expired = sign_hs256_jwt(
        {
            "kind": "sso_ticket",
            "jti": "jti-expired-1",
            "sub": "sub-1",
            "email": "owner@acme.example",
            "aud": aud,
            "iss": "http://localhost:8820",
            "iat": int(time.time()) - 120,
            "exp": int(time.time()) - 60,
        },
        SHARED,
    )

    with pytest.raises(HTTPException) as exc:
        verify_and_consume_sso_ticket(expired, rp)
    assert exc.value.status_code == 401
    assert exc.value.detail == "jwt expired"


# --- route-level: relying party (personal) ----------------------------------


def _rp_settings(monkeypatch, owner_email: str = "owner@acme.example") -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret-rp")
    monkeypatch.setattr(settings, "auth_public_base_url", "http://localhost:8830")
    monkeypatch.setattr(settings, "auth_web_base_url", "http://localhost:3830")
    monkeypatch.setattr(settings, "auth_cookie_name", "orthus_session_personal_ys")
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", True)
    monkeypatch.setattr(settings, "personal_owner_email", owner_email)
    monkeypatch.setattr(settings, "sso_shared_secret", SHARED)
    monkeypatch.setattr(settings, "sso_hub_base_url", "http://localhost:8820/api")
    monkeypatch.setattr(settings, "sso_trusted_return_origins", "")
    monkeypatch.setattr(settings, "sso_ticket_ttl_seconds", 45)


def test_auth_config_reports_sso_role_rp(clean, monkeypatch):
    client = TestClient(app)
    _rp_settings(monkeypatch)

    assert client.get("/auth/config").json()["sso_role"] == "rp"


def test_sso_start_redirects_to_hub(clean, monkeypatch):
    client = TestClient(app)
    _rp_settings(monkeypatch)

    res = client.get("/auth/sso/start?next=%2Fwiki", follow_redirects=False)

    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("http://localhost:8820/api/auth/sso/issue?")
    params = parse_qs(urlparse(location).query)
    assert params["return_to"] == ["http://localhost:8830/auth/sso/consume"]
    assert params["next"] == ["/wiki"]


def test_sso_consume_without_ticket_falls_back_to_login(clean, monkeypatch):
    client = TestClient(app)
    _rp_settings(monkeypatch)

    res = client.get("/auth/sso/consume?next=%2Fwiki", follow_redirects=False)

    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("http://localhost:3830/login?")
    params = parse_qs(urlparse(location).query)
    assert params["next"] == ["/wiki"]
    assert params["sso"] == ["nosession"]
    assert "set-cookie" not in res.headers


def test_sso_consume_with_valid_ticket_sets_session(clean, monkeypatch):
    client = TestClient(app)
    _rp_settings(monkeypatch)
    settings = get_settings()
    ticket = mint_sso_ticket("owner@acme.example", "sub-1", self_api_origin(settings), settings)

    res = client.get(f"/auth/sso/consume?ticket={ticket}&next=%2Fwiki", follow_redirects=False)

    assert res.status_code == 302
    assert res.headers["location"] == "http://localhost:3830/wiki"
    assert "orthus_session_personal_ys=" in res.headers["set-cookie"]

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "owner@acme.example"
    assert me.json()["role"] == "owner"


def test_sso_consume_rejects_replayed_ticket(clean, monkeypatch):
    client = TestClient(app)
    _rp_settings(monkeypatch)
    settings = get_settings()
    ticket = mint_sso_ticket("owner@acme.example", "sub-1", self_api_origin(settings), settings)

    first = client.get(f"/auth/sso/consume?ticket={ticket}", follow_redirects=False)
    assert first.status_code == 302
    assert "set-cookie" in first.headers

    second = client.get(f"/auth/sso/consume?ticket={ticket}", follow_redirects=False)
    assert second.status_code == 302
    assert second.headers["location"] == "http://localhost:3830/login?error=sso_failed"
    assert "set-cookie" not in second.headers


def test_sso_consume_rejects_uninvited_email(clean, monkeypatch):
    client = TestClient(app)
    _rp_settings(monkeypatch)
    settings = get_settings()
    ticket = mint_sso_ticket("stranger@acme.example", "sub-x", self_api_origin(settings), settings)

    res = client.get(f"/auth/sso/consume?ticket={ticket}", follow_redirects=False)

    assert res.status_code == 302
    assert res.headers["location"] == "http://localhost:3830/login?error=not_invited"
    assert "set-cookie" not in res.headers


# --- route-level: hub (central) ---------------------------------------------


def _hub_settings(monkeypatch, admin_email: str = "owner@acme.example") -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_public_base_url", "http://localhost:8820")
    monkeypatch.setattr(settings, "auth_web_base_url", "http://localhost:3820")
    monkeypatch.setattr(settings, "auth_cookie_name", "orthus_session_company")
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", True)
    monkeypatch.setattr(settings, "central_admin_emails", admin_email)
    monkeypatch.setattr(settings, "sso_shared_secret", SHARED)
    monkeypatch.setattr(
        settings,
        "sso_trusted_return_origins",
        "https://orthus-personal.example.com,http://localhost:8830",
    )
    monkeypatch.setattr(settings, "sso_hub_base_url", "")
    monkeypatch.setattr(settings, "sso_ticket_ttl_seconds", 45)


def test_auth_config_reports_sso_role_hub(clean, monkeypatch):
    client = TestClient(app)
    _hub_settings(monkeypatch)

    assert client.get("/auth/config").json()["sso_role"] == "hub"


def test_auth_config_reports_sso_role_off(clean, monkeypatch):
    client = TestClient(app)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")

    assert client.get("/auth/config").json()["sso_role"] == "off"


def test_sso_issue_rejects_untrusted_return_to(clean, monkeypatch):
    client = TestClient(app)
    _hub_settings(monkeypatch)

    res = client.get(
        "/auth/sso/issue?return_to=https%3A%2F%2Fevil.example%2Fauth%2Fsso%2Fconsume",
        follow_redirects=False,
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "sso return_to not trusted"


def test_sso_issue_silent_without_session_bounces_back(clean, monkeypatch):
    client = TestClient(app)
    _hub_settings(monkeypatch)

    res = client.get(
        "/auth/sso/issue?return_to=http%3A%2F%2Flocalhost%3A8830%2Fauth%2Fsso%2Fconsume"
        "&next=%2Fwiki&silent=1",
        follow_redirects=False,
    )

    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("http://localhost:8830/auth/sso/consume?")
    params = parse_qs(urlparse(location).query)
    assert params["sso"] == ["nosession"]
    assert params["next"] == ["/wiki"]
    assert "ticket" not in params


def test_sso_issue_interactive_without_session_routes_to_hub_login(clean, monkeypatch):
    client = TestClient(app)
    _hub_settings(monkeypatch)

    res = client.get(
        "/auth/sso/issue?return_to=http%3A%2F%2Flocalhost%3A8830%2Fauth%2Fsso%2Fconsume"
        "&next=%2Fwiki",
        follow_redirects=False,
    )

    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("http://localhost:3820/login?")
    next_param = parse_qs(urlparse(location).query)["next"][0]
    assert next_param.startswith("/sso-continue?")
    resume = parse_qs(urlparse(next_param).query)
    assert resume["return_to"] == ["http://localhost:8830/auth/sso/consume"]
    assert resume["next"] == ["/wiki"]


def test_sso_issue_with_session_mints_ticket(clean, monkeypatch):
    client = TestClient(app)
    _hub_settings(monkeypatch)
    assert client.post("/auth/dev-login", json={"email": "owner@acme.example"}).status_code == 200

    res = client.get(
        "/auth/sso/issue?return_to=http%3A%2F%2Flocalhost%3A8830%2Fauth%2Fsso%2Fconsume"
        "&next=%2Fwiki",
        follow_redirects=False,
    )

    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("http://localhost:8830/auth/sso/consume?")
    params = parse_qs(urlparse(location).query)
    assert params["next"] == ["/wiki"]
    claims = decode_hs256_jwt(params["ticket"][0], SHARED)
    assert claims["kind"] == "sso_ticket"
    assert claims["aud"] == "http://localhost:8830"
    assert claims["email"] == "owner@acme.example"


# --- desktop audience (same-node hub mints + consumes) ----------------------

DESKTOP_URI = "orthus://sso/consume"


def _desktop_settings(monkeypatch, admin_email: str = "owner@acme.example") -> None:
    """Company (hub) node that also consumes its own ticket via a desktop URI."""
    _hub_settings(monkeypatch, admin_email=admin_email)
    settings = get_settings()
    monkeypatch.setattr(settings, "sso_desktop_return_uris", DESKTOP_URI)


def test_validate_sso_return_to_desktop_uri_is_audience(clean):
    settings = Settings(
        sso_shared_secret=SHARED,
        auth_public_base_url="http://localhost:8820",
        node_id="company",
        sso_desktop_return_uris=DESKTOP_URI,
    )
    from orthus.auth import validate_sso_return_to

    assert validate_sso_return_to(DESKTOP_URI, settings) == DESKTOP_URI


def test_validate_sso_return_to_untrusted_custom_scheme_rejected(clean):
    settings = Settings(
        sso_shared_secret=SHARED,
        auth_public_base_url="http://localhost:8820",
        node_id="company",
        sso_desktop_return_uris=DESKTOP_URI,
    )
    from orthus.auth import validate_sso_return_to

    with pytest.raises(HTTPException) as exc:
        validate_sso_return_to("orthus://evil/consume", settings)
    assert exc.value.status_code == 400
    assert exc.value.detail == "sso return_to invalid"


def test_sso_ticket_desktop_audience_roundtrip_then_replay(clean):
    node = Settings(
        sso_shared_secret=SHARED,
        auth_public_base_url="http://localhost:8820",
        node_id="company",
        sso_desktop_return_uris=DESKTOP_URI,
    )
    ticket = mint_sso_ticket("owner@acme.example", "sub-1", DESKTOP_URI, node)

    assert verify_and_consume_sso_ticket(ticket, node) == "owner@acme.example"

    with pytest.raises(HTTPException) as exc:
        verify_and_consume_sso_ticket(ticket, node)
    assert exc.value.status_code == 401
    assert exc.value.detail == "sso ticket replay"


def test_sso_issue_desktop_return_to_mints_ticket(clean, monkeypatch):
    client = TestClient(app)
    _desktop_settings(monkeypatch)
    assert client.post("/auth/dev-login", json={"email": "owner@acme.example"}).status_code == 200

    res = client.get(
        "/auth/sso/issue?return_to=orthus%3A%2F%2Fsso%2Fconsume&next=%2Fwiki",
        follow_redirects=False,
    )

    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("orthus://sso/consume?")
    params = parse_qs(urlparse(location).query)
    assert params["next"] == ["/wiki"]
    claims = decode_hs256_jwt(params["ticket"][0], SHARED)
    assert claims["kind"] == "sso_ticket"
    assert claims["aud"] == DESKTOP_URI
    assert claims["email"] == "owner@acme.example"


def test_sso_issue_desktop_uri_without_allowlist_rejected(clean, monkeypatch):
    client = TestClient(app)
    # Hub role but no desktop URI configured: the custom-scheme return_to is
    # untrusted and must 400 rather than mint a ticket.
    _hub_settings(monkeypatch)

    res = client.get(
        "/auth/sso/issue?return_to=orthus%3A%2F%2Fsso%2Fconsume",
        follow_redirects=False,
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "sso return_to invalid"


def test_sso_consume_desktop_ticket_same_node_sets_session(clean, monkeypatch):
    client = TestClient(app)
    _desktop_settings(monkeypatch)
    settings = get_settings()
    ticket = mint_sso_ticket("owner@acme.example", "sub-1", DESKTOP_URI, settings)

    res = client.get(f"/auth/sso/consume?ticket={ticket}&next=%2Fwiki", follow_redirects=False)

    assert res.status_code == 302
    assert res.headers["location"] == "http://localhost:3820/wiki"
    assert "orthus_session_company=" in res.headers["set-cookie"]

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "owner@acme.example"


def test_sso_consume_desktop_ticket_replay_rejected(clean, monkeypatch):
    client = TestClient(app)
    _desktop_settings(monkeypatch)
    settings = get_settings()
    ticket = mint_sso_ticket("owner@acme.example", "sub-1", DESKTOP_URI, settings)

    first = client.get(f"/auth/sso/consume?ticket={ticket}", follow_redirects=False)
    assert first.status_code == 302
    assert "set-cookie" in first.headers

    second = client.get(f"/auth/sso/consume?ticket={ticket}", follow_redirects=False)
    assert second.status_code == 302
    assert second.headers["location"] == "http://localhost:3820/login?error=sso_failed"
    assert "set-cookie" not in second.headers


def test_sso_consume_desktop_enabled_without_rp_no_longer_404s(clean, monkeypatch):
    client = TestClient(app)
    # Company node, desktop-enabled, NO rp config (no sso_hub_base_url): the
    # consume route must accept the request (falling back to login on no ticket)
    # rather than 404.
    _hub_settings(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "sso_desktop_return_uris", DESKTOP_URI)
    monkeypatch.setattr(settings, "sso_hub_base_url", "")

    res = client.get("/auth/sso/consume?next=%2Fwiki", follow_redirects=False)

    assert res.status_code == 302
    assert res.headers["location"].startswith("http://localhost:3820/login?")


def test_sso_consume_neither_rp_nor_desktop_still_404s(clean, monkeypatch):
    client = TestClient(app)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "sso_shared_secret", SHARED)
    monkeypatch.setattr(settings, "sso_hub_base_url", "")
    monkeypatch.setattr(settings, "sso_desktop_return_uris", "")

    res = client.get("/auth/sso/consume?next=%2Fwiki", follow_redirects=False)

    assert res.status_code == 404
    assert res.json()["detail"] == "sso not enabled"


def test_sso_issue_https_origin_not_in_desktop_list_unchanged(clean, monkeypatch):
    client = TestClient(app)
    # Desktop URI configured alongside the trusted https origins: an https
    # return_to that is only in the trusted-origin list (not the desktop list)
    # must still validate and mint a normal origin-audience ticket.
    _desktop_settings(monkeypatch)
    assert client.post("/auth/dev-login", json={"email": "owner@acme.example"}).status_code == 200

    res = client.get(
        "/auth/sso/issue?return_to=http%3A%2F%2Flocalhost%3A8830%2Fauth%2Fsso%2Fconsume"
        "&next=%2Fwiki",
        follow_redirects=False,
    )

    assert res.status_code == 302
    location = res.headers["location"]
    assert location.startswith("http://localhost:8830/auth/sso/consume?")
    claims = decode_hs256_jwt(parse_qs(urlparse(location).query)["ticket"][0], SHARED)
    assert claims["aud"] == "http://localhost:8830"
