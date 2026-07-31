"""S1 auth mode boundary."""

from __future__ import annotations

import time
import base64
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from orthus.api.main import app
from orthus.auth import fetch_google_profile, sign_hs256_jwt
from orthus.db import session
from orthus.settings import get_settings, validate_runtime_settings
from orthus.tables import auth_magic_links, auth_sessions


def test_demo_auth_still_accepts_x_user_id(user_id, monkeypatch):
    client = TestClient(app)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")

    res = client.get("/documents", headers={"X-User-Id": str(user_id)})

    assert res.status_code == 200
    assert res.json() == []


def test_jwt_auth_rejects_x_user_id_without_bearer(user_id, monkeypatch):
    client = TestClient(app)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "jwt")
    monkeypatch.setattr(settings, "auth_jwt_secret", "test-secret")

    res = client.get("/documents", headers={"X-User-Id": str(user_id)})

    assert res.status_code == 401
    assert res.json()["detail"] == "missing bearer token"


def test_jwt_auth_accepts_signed_bearer_and_ignores_x_user_id(user_id, monkeypatch):
    client = TestClient(app)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "jwt")
    monkeypatch.setattr(settings, "auth_jwt_secret", "test-secret")
    token = sign_hs256_jwt(
        {"sub": str(user_id), "exp": int(time.time()) + 60},
        "test-secret",
    )

    res = client.get(
        "/documents",
        headers={
            "Authorization": f"Bearer {token}",
            "X-User-Id": str(uuid4()),
        },
    )

    assert res.status_code == 200
    assert res.json() == []


def test_jwt_auth_rejects_expired_token(user_id, monkeypatch):
    client = TestClient(app)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "jwt")
    monkeypatch.setattr(settings, "auth_jwt_secret", "test-secret")
    token = sign_hs256_jwt(
        {"sub": str(user_id), "exp": int(time.time()) - 1},
        "test-secret",
    )

    res = client.get("/documents", headers={"Authorization": f"Bearer {token}"})

    assert res.status_code == 401
    assert res.json()["detail"] == "jwt expired"


def test_oauth_start_uses_google_account_chooser(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")
    settings = get_settings()
    monkeypatch.setattr(settings, "google_oauth_client_id", "client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "client-secret")

    response = client.get(
        "/auth/oauth/google/start?next=%2Feditor",
        follow_redirects=False,
    )

    assert response.status_code == 302
    parsed = urlparse(response.headers["location"])
    assert (parsed.scheme, parsed.netloc, parsed.path) == (
        "https",
        "accounts.google.com",
        "/o/oauth2/v2/auth",
    )
    params = parse_qs(parsed.query)
    assert params["client_id"] == ["client-id"]
    assert params["redirect_uri"] == ["http://localhost:8830/auth/oauth/google/callback"]
    assert params["prompt"] == ["select_account"]
    assert set(" ".join(params["scope"]).split()) == {"openid", "email", "profile"}
    assert params["response_type"] == ["code"]
    assert params.get("state")
    assert params.get("nonce")


def test_session_dev_login_sets_cookie_and_authenticates(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")

    login = client.post("/auth/dev-login", json={"email": "owner@example.com"})
    assert login.status_code == 200
    assert "orthus_session_personal_ys" in login.headers["set-cookie"]
    assert login.json()["role"] == "owner"

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "owner@example.com"

    docs = client.get("/documents")
    assert docs.status_code == 200
    assert docs.json() == []


def test_authenticated_request_rolls_persistent_session_cookie(clean, monkeypatch):
    """Each authenticated request re-issues a persistent session cookie so an
    actively used session never lapses browser-side ("log in once, stay logged
    in" on mobile). DB sliding/absolute expiry is still enforced server-side."""
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")
    settings = get_settings()

    login = client.post("/auth/dev-login", json={"email": "owner@example.com"})
    assert login.status_code == 200

    me = client.get("/auth/me")
    assert me.status_code == 200
    set_cookie = me.headers.get("set-cookie", "")
    assert "orthus_session_personal_ys=" in set_cookie
    # Persistent (non-session) cookie whose Max-Age tracks the absolute window,
    # rolled forward on every request.
    expected_max_age = settings.auth_session_absolute_days * 24 * 60 * 60
    assert f"Max-Age={expected_max_age}" in set_cookie


def test_authenticated_request_extends_existing_db_session_horizon(clean, monkeypatch):
    """A valid older mobile session should be renewed server-side too.

    This covers users who logged in before the long-lived mobile policy: once
    they make a normal authenticated request, the DB expiry moves forward instead
    of preserving the original login-time absolute ceiling.
    """
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")
    settings = get_settings()

    login = client.post("/auth/dev-login", json={"email": "owner@example.com"})
    assert login.status_code == 200
    with session() as s:
        row = s.execute(
            select(
                auth_sessions.c.session_id,
                auth_sessions.c.issued_at,
            )
        ).one()
        old_expires = row.issued_at + timedelta(days=1)
        old_absolute = row.issued_at + timedelta(days=2)
        s.execute(
            auth_sessions.update()
            .where(auth_sessions.c.session_id == row.session_id)
            .values(expires_at=old_expires, absolute_expires_at=old_absolute)
        )
        s.commit()

    me = client.get("/auth/me")
    assert me.status_code == 200

    with session() as s:
        renewed = s.execute(
            select(
                auth_sessions.c.expires_at,
                auth_sessions.c.absolute_expires_at,
            )
            .where(auth_sessions.c.session_id == row.session_id)
        ).one()

    assert renewed.expires_at > old_expires
    assert renewed.absolute_expires_at > old_absolute
    assert renewed.absolute_expires_at - renewed.expires_at >= timedelta(
        days=settings.auth_session_absolute_days - settings.auth_session_sliding_days - 1
    )


def test_magic_link_request_is_allowlist_scoped_and_response_is_generic(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@acme.example")
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "console")
    delivered: list[tuple[str, str]] = []

    def _capture(issue, _settings):
        delivered.append((issue.email, issue.link))
        return "console"

    monkeypatch.setattr("orthus.api.routes.auth.deliver_magic_link_issue", _capture)

    allowed = client.post(
        "/auth/magic-link/request",
        json={"email": "owner", "next": "/wiki"},
    )
    denied = client.post(
        "/auth/magic-link/request",
        json={"email": "other", "next": "/wiki"},
    )

    assert allowed.status_code == 200
    assert denied.status_code == 200
    assert allowed.json() == denied.json()
    assert allowed.json()["accepted"] is True
    assert len(delivered) == 1
    assert delivered[0][0] == "owner@acme.example"
    assert "/auth/magic-link/consume?token=" in delivered[0][1]

    with session() as s:
        rows = s.execute(select(auth_magic_links.c.email)).all()
    assert [row.email for row in rows] == ["owner@acme.example"]


def test_magic_link_consume_sets_cookie_and_rejects_reuse(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@acme.example")
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "console")
    delivered: list[str] = []

    def _capture(issue, _settings):
        delivered.append(issue.link)
        return "console"

    monkeypatch.setattr("orthus.api.routes.auth.deliver_magic_link_issue", _capture)
    request_link = client.post(
        "/auth/magic-link/request",
        json={"email": "owner", "next": "/editor"},
    )
    assert request_link.status_code == 200

    consume = client.get(delivered[0].replace("http://localhost:8830", ""), follow_redirects=False)

    assert consume.status_code == 302
    assert consume.headers["location"] == "http://localhost:3830/editor"
    assert "orthus_session_personal_ys=" in consume.headers["set-cookie"]

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "owner@acme.example"
    assert me.json()["role"] == "owner"

    second = client.get(delivered[0].replace("http://localhost:8830", ""), follow_redirects=False)
    assert second.status_code == 302
    assert second.headers["location"] == "http://localhost:3830/login?error=magic_link_invalid"
    assert "set-cookie" not in second.headers


def test_magic_link_consume_rejects_expired_token(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@acme.example")
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "console")
    delivered: list[str] = []

    def _capture(issue, _settings):
        delivered.append(issue.link)
        return "console"

    monkeypatch.setattr("orthus.api.routes.auth.deliver_magic_link_issue", _capture)
    assert client.post("/auth/magic-link/request", json={"email": "owner"}).status_code == 200
    with session() as s:
        s.execute(
            auth_magic_links.update().values(
                expires_at=auth_magic_links.c.issued_at - text("interval '1 second'")
            )
        )
        s.commit()

    consume = client.get(delivered[0].replace("http://localhost:8830", ""), follow_redirects=False)

    assert consume.status_code == 302
    assert consume.headers["location"] == "http://localhost:3830/login?error=magic_link_invalid"
    assert "set-cookie" not in consume.headers


def test_magic_link_request_rejects_external_domain(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@acme.example")

    res = client.post("/auth/magic-link/request", json={"email": "owner@example.com"})

    assert res.status_code == 422
    assert res.json()["detail"] == "email domain not allowed"


def test_magic_link_resend_delivery_posts_email_payload(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@acme.example")
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "resend")
    monkeypatch.setattr(settings, "auth_magic_link_from_email", "Orthus <login@auth.acme.example>")
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    captured: list[httpx.Request] = []
    monkeypatch.setattr(
        "orthus.auth.httpx.Client",
        lambda base_url, timeout: _ResendClient(captured, status_code=200),
    )

    res = client.post("/auth/magic-link/request", json={"email": "owner", "next": "/wiki"})

    assert res.status_code == 200
    assert res.json()["delivery"] == "resend"
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["authorization"] == "Bearer re_test_key"
    payload = json.loads(request.content)
    assert payload["from"] == "Orthus <login@auth.acme.example>"
    assert payload["to"] == ["owner@acme.example"]
    assert payload["subject"] == "Orthus 로그인 링크"
    assert "auth/magic-link/consume?token=" in payload["html"]
    assert "auth/magic-link/consume?token=" in payload["text"]
    assert payload["tags"] == [{"name": "kind", "value": "auth_magic_link"}]


def test_magic_link_resend_delivery_failure_does_not_leak_allowlist(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@acme.example")
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "resend")
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(
        "orthus.auth.httpx.Client",
        lambda base_url, timeout: _ResendClient([], status_code=500),
    )

    allowed = client.post("/auth/magic-link/request", json={"email": "owner"})
    denied = client.post("/auth/magic-link/request", json={"email": "other"})

    assert allowed.status_code == 200
    assert denied.status_code == 200
    assert allowed.json() == denied.json()


def test_public_session_cookie_uses_secure_httponly_samesite(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_cookie_secure", True)

    login = client.post("/auth/dev-login", json={"email": "owner@example.com"})

    assert login.status_code == 200
    cookie = login.headers["set-cookie"]
    assert "orthus_session_personal_ys=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Domain=" not in cookie


def test_oauth_callback_sets_secure_host_scoped_cookie(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "google_oauth_client_id", "client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "client-secret")
    state = sign_hs256_jwt(
        {
            "kind": "oauth_state",
            "nonce": "nonce-1",
            "next": "/editor",
            "exp": int(time.time()) + 60,
        },
        "test-session-secret",
    )
    id_token = _unsigned_jwt(
        {
            "iss": "https://accounts.google.com",
            "aud": "client-id",
            "sub": "google-sub",
            "email": "owner@example.com",
            "email_verified": True,
            "nonce": "nonce-1",
            "exp": int(time.time()) + 60,
        }
    )
    monkeypatch.setattr("orthus.auth.httpx.Client", lambda timeout: _OAuthClient(id_token))

    callback = client.get(
        f"/auth/oauth/google/callback?code=code-1&state={state}",
        follow_redirects=False,
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "http://localhost:3830/editor"
    cookie = callback.headers["set-cookie"]
    assert "orthus_session_personal_ys=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Domain=" not in cookie


def test_oauth_callback_quotes_provider_error(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")

    callback = client.get(
        "/auth/oauth/google/callback?error=access%20denied%2Fconsent",
        follow_redirects=False,
    )

    assert callback.status_code == 302
    assert (
        callback.headers["location"]
        == "http://localhost:3830/login?error=access%20denied%2Fconsent"
    )
    assert "set-cookie" not in callback.headers


def test_oauth_callback_redirects_uninvited_account_without_cookie(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "google_oauth_client_id", "client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "client-secret")
    state = sign_hs256_jwt(
        {
            "kind": "oauth_state",
            "nonce": "nonce-1",
            "next": "/editor",
            "exp": int(time.time()) + 60,
        },
        "test-session-secret",
    )
    id_token = _unsigned_jwt(
        {
            "iss": "https://accounts.google.com",
            "aud": "client-id",
            "sub": "google-sub-other",
            "email": "other@example.com",
            "email_verified": True,
            "nonce": "nonce-1",
            "exp": int(time.time()) + 60,
        }
    )
    monkeypatch.setattr(
        "orthus.auth.httpx.Client",
        lambda timeout: _OAuthClient(
            id_token, email="other@example.com", subject="google-sub-other"
        ),
    )

    callback = client.get(
        f"/auth/oauth/google/callback?code=code-1&state={state}",
        follow_redirects=False,
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "http://localhost:3830/login?error=not_invited"
    assert "set-cookie" not in callback.headers


def test_session_login_sets_sliding_and_absolute_ttl(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")

    login = client.post("/auth/dev-login", json={"email": "owner@example.com"})

    assert login.status_code == 200
    settings = get_settings()
    with session() as s:
        row = s.execute(
            select(
                auth_sessions.c.issued_at,
                auth_sessions.c.expires_at,
                auth_sessions.c.absolute_expires_at,
            )
        ).one()
    assert row.expires_at - row.issued_at == timedelta(
        days=settings.auth_session_sliding_days
    )
    assert row.absolute_expires_at - row.issued_at == timedelta(
        days=settings.auth_session_absolute_days
    )


def test_session_dev_login_rejects_uninvited_account(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")

    res = client.post("/auth/dev-login", json={"email": "other@example.com"})

    assert res.status_code == 403
    assert res.json()["detail"] == "account not invited"


def test_session_logout_revokes_session(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")
    assert client.post("/auth/dev-login", json={"email": "owner@example.com"}).status_code == 200

    logout = client.post("/auth/logout")
    assert logout.status_code == 204

    me = client.get("/auth/me")
    assert me.status_code == 401


def test_session_logout_clears_secure_cookie(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    assert client.post("/auth/dev-login", json={"email": "owner@example.com"}).status_code == 200

    logout = client.post("/auth/logout")

    assert logout.status_code == 204
    cookie = logout.headers["set-cookie"]
    assert "orthus_session_personal_ys=" in cookie
    assert "Max-Age=0" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Domain=" not in cookie
    assert client.get("/auth/me").status_code == 401


def test_allowlist_admin_can_add_and_revoke_member(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")
    assert client.post("/auth/dev-login", json={"email": "owner@example.com"}).status_code == 200

    created = client.post(
        "/auth/allowlist",
        json={"email": "member@example.com", "role": "member"},
    )
    assert created.status_code == 201
    assert created.json()["email"] == "member@example.com"
    assert created.json()["role"] == "member"

    listed = client.get("/auth/allowlist")
    assert listed.status_code == 200
    emails = {row["email"]: row for row in listed.json()}
    assert emails["owner@example.com"]["role"] == "owner"
    assert emails["member@example.com"]["revoked_at"] is None

    revoked = client.delete("/auth/allowlist/member@example.com")
    assert revoked.status_code == 204

    listed_again = client.get("/auth/allowlist")
    row = next(r for r in listed_again.json() if r["email"] == "member@example.com")
    assert row["revoked_at"] is not None


def test_allowlist_rejects_last_owner_admin_demotion(clean, monkeypatch):
    client = TestClient(app)
    _session_settings(monkeypatch, owner_email="owner@example.com")
    assert client.post("/auth/dev-login", json={"email": "owner@example.com"}).status_code == 200

    res = client.post(
        "/auth/allowlist",
        json={"email": "owner@example.com", "role": "member"},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "last owner/admin required"


def test_google_profile_rejects_invalid_issuer(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_oauth_client_id", "client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "client-secret")
    monkeypatch.setattr(settings, "auth_public_base_url", "http://localhost:8820")
    id_token = _unsigned_jwt(
        {
            "iss": "https://evil.example",
            "aud": "client-id",
            "sub": "google-sub",
            "email": "owner@example.com",
            "email_verified": True,
            "nonce": "nonce-1",
            "exp": int(time.time()) + 60,
        }
    )
    monkeypatch.setattr("orthus.auth.httpx.Client", lambda timeout: _OAuthClient(id_token))

    with pytest.raises(HTTPException) as exc_info:
        fetch_google_profile("code", "nonce-1", settings)
    assert getattr(exc_info.value, "status_code") == 401
    assert getattr(exc_info.value, "detail") == "google issuer invalid"


def test_public_runtime_rejects_demo_auth(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "auth_public_base_url", "https://orthus-central.example.com/api")
    monkeypatch.setattr(settings, "auth_web_base_url", "https://orthus-central.example.com")

    with pytest.raises(RuntimeError, match="ORTHUS_AUTH_MODE=demo"):
        validate_runtime_settings(settings)


def test_public_runtime_rejects_insecure_session(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "auth_public_base_url", "https://orthus-central.example.com/api")
    monkeypatch.setattr(settings, "auth_web_base_url", "https://orthus-central.example.com")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "console")

    with pytest.raises(RuntimeError, match="ORTHUS_AUTH_COOKIE_SECURE=true"):
        validate_runtime_settings(settings)


def _isolate_ambient_sso(monkeypatch, settings) -> None:
    # 워크트리/운영 .env가 ORTHUS_SSO_* 를 설정하면 validate_runtime_settings의 SSO
    # 분기가 켜져 sso_shared_secret 미설정 시 거부된다. accept 테스트는 SSO를 검증
    # 대상으로 두지 않으므로 ambient SSO 설정을 비워 어떤 환경에서도 hermetic하게 한다.
    monkeypatch.setattr(settings, "sso_hub_base_url", "")
    monkeypatch.setattr(settings, "sso_trusted_return_origins", "")
    monkeypatch.setattr(settings, "sso_desktop_return_uris", "")


def test_public_runtime_accepts_hardened_session(monkeypatch):
    settings = get_settings()
    _isolate_ambient_sso(monkeypatch, settings)
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "auth_public_base_url", "https://orthus-central.example.com/api")
    monkeypatch.setattr(settings, "auth_web_base_url", "https://orthus-central.example.com")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "console")

    validate_runtime_settings(settings)


def test_public_runtime_rejects_enabled_mail_ingest_without_secrets(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "auth_public_base_url", "https://orthus-central.example.com/api")
    monkeypatch.setattr(settings, "auth_web_base_url", "https://orthus-central.example.com")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "console")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_ingest_enabled", True)
    monkeypatch.setattr(settings, "mail_ingest_secret", "")
    monkeypatch.setattr(settings, "mail_ingest_secret_ref", "")
    monkeypatch.setattr(settings, "mail_ingest_hmac_secret", "")
    monkeypatch.setattr(settings, "mail_ingest_hmac_secret_ref", "")

    with pytest.raises(RuntimeError, match="ORTHUS_MAIL_INGEST_SECRET"):
        validate_runtime_settings(settings)


def test_public_runtime_accepts_enabled_mail_ingest_with_secrets(monkeypatch):
    settings = get_settings()
    _isolate_ambient_sso(monkeypatch, settings)
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "auth_public_base_url", "https://orthus-central.example.com/api")
    monkeypatch.setattr(settings, "auth_web_base_url", "https://orthus-central.example.com")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "console")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "mail_ingest_enabled", True)
    monkeypatch.setattr(settings, "mail_ingest_secret", "ingest-secret")
    monkeypatch.setattr(settings, "mail_ingest_hmac_secret", "hmac-secret")

    validate_runtime_settings(settings)


def test_public_runtime_rejects_missing_magic_link_delivery(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "auth_public_base_url", "https://orthus-central.example.com/api")
    monkeypatch.setattr(settings, "auth_web_base_url", "https://orthus-central.example.com")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "none")

    with pytest.raises(RuntimeError, match="ORTHUS_AUTH_MAGIC_LINK_DELIVERY"):
        validate_runtime_settings(settings)


def test_public_runtime_rejects_resend_without_api_key(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "auth_public_base_url", "https://orthus-central.example.com/api")
    monkeypatch.setattr(settings, "auth_web_base_url", "https://orthus-central.example.com")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "resend")
    monkeypatch.setattr(settings, "resend_api_key", "")

    with pytest.raises(RuntimeError, match="ORTHUS_RESEND_API_KEY"):
        validate_runtime_settings(settings)


def test_public_runtime_accepts_resend_with_api_key(monkeypatch):
    settings = get_settings()
    _isolate_ambient_sso(monkeypatch, settings)
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "auth_public_base_url", "https://orthus-central.example.com/api")
    monkeypatch.setattr(settings, "auth_web_base_url", "https://orthus-central.example.com")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", False)
    monkeypatch.setattr(settings, "auth_magic_link_delivery", "resend")
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")

    validate_runtime_settings(settings)


def _session_settings(monkeypatch, owner_email: str) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "session")
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    monkeypatch.setattr(settings, "auth_session_secret", "test-session-secret")
    monkeypatch.setattr(settings, "auth_public_base_url", "http://localhost:8830")
    monkeypatch.setattr(settings, "auth_web_base_url", "http://localhost:3830")
    monkeypatch.setattr(settings, "auth_cookie_name", "orthus_session_personal_ys")
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "auth_dev_login_enabled", True)
    monkeypatch.setattr(settings, "personal_owner_email", owner_email)


class _OAuthResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def json(self) -> dict:
        return self._payload


class _ResendClient:
    def __init__(self, captured: list[httpx.Request], *, status_code: int) -> None:
        self.captured = captured
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, path: str, *, headers: dict[str, str], json: dict):
        request = httpx.Request(
            "POST",
            f"https://api.resend.com{path}",
            headers=headers,
            json=json,
        )
        self.captured.append(request)
        return httpx.Response(
            self.status_code,
            json={"id": "email_test"},
            request=request,
        )


class _OAuthClient:
    def __init__(
        self,
        id_token: str,
        *,
        email: str = "owner@example.com",
        subject: str = "google-sub",
    ) -> None:
        self.id_token = id_token
        self.email = email
        self.subject = subject

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, *args, **kwargs):
        return _OAuthResponse({"id_token": self.id_token})

    def get(self, *args, **kwargs):
        return _OAuthResponse(
            {
                "aud": "client-id",
                "sub": self.subject,
                "email": self.email,
                "email_verified": "true",
                "name": self.email,
            }
        )


def _unsigned_jwt(payload: dict) -> str:
    header = {"alg": "none"}
    return ".".join(
        [
            _b64url(header),
            _b64url(payload),
            "",
        ]
    )


def _b64url(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
