"""Auth helpers for demo, JWT, and S1 session boundaries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.db import session
from orthus.settings import Settings
from orthus.tables import (
    auth_allowlist,
    auth_identities,
    auth_magic_links,
    auth_sessions,
    auth_sso_tickets,
    users,
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

# 세션 sliding touch(UPDATE last_seen/expires) 스로틀 창 — user_from_session_request 참조.
_SESSION_TOUCH_THROTTLE = timedelta(minutes=5)
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
LOCAL_AUTH_HOSTS = {"localhost", "127.0.0.1", "::1"}
MANAGER_ROLES = {"owner", "admin"}

# Bootstrap admin/owner emails only need seeding once per process; the upsert is
# idempotent and runtime allowlist changes go through the /auth/allowlist routes,
# not this path. Running it on every auth check added a write + connection
# checkout to the request hot path and helped drain the pool under burst.
_bootstrap_done: set[str] = set()


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: UUID
    auth_mode: str
    display_name: str
    email: str | None = None
    role: str | None = None
    node_id: str | None = None


@dataclass(frozen=True)
class OAuthProfile:
    provider: str
    subject: str
    email: str
    email_verified: bool
    display_name: str | None = None


@dataclass(frozen=True)
class MagicLinkIssue:
    email: str
    link: str
    expires_at: datetime


def user_id_from_bearer_token(authorization: str | None, secret: str) -> UUID:
    if not secret:
        raise HTTPException(status_code=500, detail="ORTHUS_AUTH_JWT_SECRET not set")
    if not authorization:
        raise HTTPException(status_code=401, detail="missing bearer token")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="invalid authorization scheme")

    claims = decode_hs256_jwt(token, secret)
    subject = claims.get("sub") or claims.get("user_id")
    if not isinstance(subject, str):
        raise HTTPException(status_code=401, detail="jwt subject missing")
    try:
        return UUID(subject)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="jwt subject invalid") from exc


def decode_hs256_jwt(token: str, secret: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=401, detail="invalid jwt")

    header_raw, payload_raw, signature_raw = parts
    signed = f"{header_raw}.{payload_raw}".encode("ascii")
    expected = _b64url_encode(hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature_raw):
        raise HTTPException(status_code=401, detail="jwt signature invalid")

    header = _decode_json(header_raw)
    if header.get("alg") != "HS256":
        raise HTTPException(status_code=401, detail="jwt alg unsupported")
    if header.get("typ") not in (None, "JWT"):
        raise HTTPException(status_code=401, detail="jwt typ unsupported")

    payload = _decode_json(payload_raw)
    exp = payload.get("exp")
    if exp is not None:
        try:
            expired = float(exp) < time.time()
        except (TypeError, ValueError):
            raise HTTPException(status_code=401, detail="jwt exp invalid") from None
        if expired:
            raise HTTPException(status_code=401, detail="jwt expired")
    return payload


def sign_hs256_jwt(claims: dict[str, Any], secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    header_raw = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_raw = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signed = f"{header_raw}.{payload_raw}".encode("ascii")
    signature = _b64url_encode(hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest())
    return f"{header_raw}.{payload_raw}.{signature}"


def authenticate_request(
    request: Request,
    settings: Settings,
    authorization: str | None,
    x_user_id: str | None,
) -> AuthenticatedUser:
    if settings.auth_mode == "session":
        return user_from_session_request(request, settings)
    if settings.auth_mode == "jwt":
        user_id = user_id_from_bearer_token(authorization, settings.auth_jwt_secret)
        return _user_from_id(user_id, auth_mode="jwt")
    if settings.auth_mode != "demo":
        raise HTTPException(status_code=500, detail=f"unsupported auth mode: {settings.auth_mode}")

    if x_user_id:
        try:
            user_id = UUID(x_user_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="invalid X-User-Id") from e
        return AuthenticatedUser(
            user_id=user_id,
            auth_mode="demo",
            display_name="Demo User",
            role="demo",
            node_id=settings.node_id,
        )
    user_id = _demo_user_id()
    return _user_from_id(user_id, auth_mode="demo", role="demo", node_id=settings.node_id)


def user_from_session_request(request: Request, settings: Settings) -> AuthenticatedUser:
    token = request.cookies.get(session_cookie_name(settings))
    if not token:
        raise HTTPException(status_code=401, detail="missing session")
    token_hash = hash_session_token(token)
    now = utcnow()
    with session() as s:
        row = s.execute(
            select(
                auth_sessions.c.session_id,
                auth_sessions.c.user_id,
                auth_sessions.c.expires_at,
                auth_sessions.c.absolute_expires_at,
                auth_sessions.c.last_seen_at,
                users.c.display_name,
                auth_identities.c.email,
            )
            .select_from(
                auth_sessions.join(users, auth_sessions.c.user_id == users.c.user_id).join(
                    auth_identities, auth_sessions.c.user_id == auth_identities.c.user_id
                )
            )
            .where(
                auth_sessions.c.token_hash == token_hash,
                auth_sessions.c.node_id == settings.node_id,
                auth_sessions.c.revoked_at.is_(None),
                auth_identities.c.provider.in_(["google", "dev", "magic_link"]),
            )
            .limit(1)
        ).first()
        if not row:
            raise HTTPException(status_code=401, detail="invalid session")
        if row.absolute_expires_at <= now or row.expires_at <= now:
            s.execute(
                update(auth_sessions)
                .where(auth_sessions.c.session_id == row.session_id)
                .values(revoked_at=now)
            )
            s.commit()
            raise HTTPException(status_code=401, detail="session expired")

        # Resolve the allowlist role on the SAME session — a nested session()
        # here doubled the per-request connection cost and drained the pool.
        ensure_bootstrap_allowlist(settings)
        role_row = s.execute(
            select(auth_allowlist.c.role).where(
                auth_allowlist.c.node_id == settings.node_id,
                auth_allowlist.c.email == normalize_email(str(row.email)),
                auth_allowlist.c.revoked_at.is_(None),
            )
        ).first()
        role = str(role_row.role) if role_row else None
        if role is None:
            raise HTTPException(status_code=403, detail="account not allowed")
        # Keep actively used browser sessions alive across app/web/desktop updates.
        # The cookie is re-issued by get_current_user(); extend the DB horizon here
        # as well so an old mobile session does not hit a fixed login-time ceiling.
        next_absolute = max(
            row.absolute_expires_at,
            now + timedelta(days=settings.auth_session_absolute_days),
        )
        next_expires = min(now + timedelta(days=settings.auth_session_sliding_days), next_absolute)
        # sliding touch UPDATE는 5분 창으로 스로틀한다 — 창 안의 재기록은 horizon을
        # 몇 초 미는 것뿐이라(만료는 일 단위) 생략해도 시맨틱이 변하지 않고, 요청마다
        # 세션 행 write가 발생하던 비용을 없앤다. 단 back-dated horizon(구정책 세션
        # renewal)은 즉시 기록해야 하므로 horizon이 창 이상 점프하면 스로틀하지 않는다.
        stale_seen = row.last_seen_at is None or now - row.last_seen_at >= _SESSION_TOUCH_THROTTLE
        horizon_jump = (
            next_expires - row.expires_at >= _SESSION_TOUCH_THROTTLE
            or next_absolute - row.absolute_expires_at >= _SESSION_TOUCH_THROTTLE
        )
        if stale_seen or horizon_jump:
            s.execute(
                update(auth_sessions)
                .where(auth_sessions.c.session_id == row.session_id)
                .values(
                    last_seen_at=now,
                    expires_at=next_expires,
                    absolute_expires_at=next_absolute,
                )
            )
            s.commit()

    return AuthenticatedUser(
        user_id=row.user_id,
        auth_mode="session",
        display_name=row.display_name,
        email=str(row.email),
        role=role,
        node_id=settings.node_id,
    )


def begin_google_oauth_url(next_path: str, settings: Settings) -> str:
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth credentials not set")
    secret = _session_secret(settings)
    nonce = secrets.token_urlsafe(24)
    state_claims = {
        "kind": "oauth_state",
        "nonce": nonce,
        "next": safe_next_path(next_path),
        "exp": int(time.time()) + 600,
    }
    state = sign_hs256_jwt(state_claims, secret)
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": oauth_redirect_uri(settings),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def complete_google_oauth(code: str, state: str, settings: Settings) -> tuple[OAuthProfile, str]:
    claims = decode_hs256_jwt(state, _session_secret(settings))
    if claims.get("kind") != "oauth_state":
        raise HTTPException(status_code=401, detail="invalid oauth state")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str):
        raise HTTPException(status_code=401, detail="invalid oauth nonce")
    profile = fetch_google_profile(code, nonce, settings)
    return profile, safe_next_path(str(claims.get("next") or "/editor"))


def fetch_google_profile(code: str, nonce: str, settings: Settings) -> OAuthProfile:
    if not settings.google_oauth_client_secret:
        raise HTTPException(status_code=500, detail="Google OAuth client secret not set")
    with httpx.Client(timeout=10) as client:
        token_res = client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "redirect_uri": oauth_redirect_uri(settings),
                "grant_type": "authorization_code",
            },
        )
        if token_res.status_code >= 400:
            raise HTTPException(status_code=401, detail="google token exchange failed")
        id_token = token_res.json().get("id_token")
        if not isinstance(id_token, str):
            raise HTTPException(status_code=401, detail="google id_token missing")

        info_res = client.get(GOOGLE_TOKENINFO_URL, params={"id_token": id_token})
        if info_res.status_code >= 400:
            raise HTTPException(status_code=401, detail="google id_token invalid")
        info = info_res.json()

    payload = decode_unsigned_jwt_payload(id_token)
    aud = info.get("aud") or payload.get("aud")
    if isinstance(aud, list):
        audience_ok = settings.google_oauth_client_id in aud
    else:
        audience_ok = aud == settings.google_oauth_client_id
    if not audience_ok:
        raise HTTPException(status_code=401, detail="google audience invalid")
    if payload.get("iss") not in GOOGLE_ISSUERS:
        raise HTTPException(status_code=401, detail="google issuer invalid")
    try:
        expired = float(payload.get("exp", 0)) <= time.time()
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="google exp invalid") from None
    if expired:
        raise HTTPException(status_code=401, detail="google id_token expired")

    email = normalize_email(str(info.get("email") or ""))
    if not email:
        raise HTTPException(status_code=401, detail="google email missing")
    email_verified = _claim_bool(info.get("email_verified", payload.get("email_verified")))
    if payload.get("nonce") != nonce:
        raise HTTPException(status_code=401, detail="google nonce invalid")
    subject = str(info.get("sub") or payload.get("sub") or "")
    if not subject:
        raise HTTPException(status_code=401, detail="google subject missing")
    return OAuthProfile(
        provider="google",
        subject=subject,
        email=email,
        email_verified=email_verified,
        display_name=str(info.get("name") or payload.get("name") or email),
    )


def user_id_for_allowed_profile(profile: OAuthProfile, settings: Settings) -> UUID:
    if not profile.email_verified:
        raise HTTPException(status_code=403, detail="email not verified")
    if allowlist_role(profile.email, settings) is None:
        raise HTTPException(status_code=403, detail="account not invited")
    return upsert_identity_user(profile)


def create_magic_link_issue(
    email: str,
    next_path: str,
    settings: Settings,
    request: Request,
) -> MagicLinkIssue:
    normalized_email = normalize_magic_link_email(email, settings)
    if allowlist_role(normalized_email, settings) is None:
        raise HTTPException(status_code=403, detail="account not invited")

    token = secrets.token_urlsafe(40)
    now = utcnow()
    expires_at = now + timedelta(minutes=max(1, settings.auth_magic_link_ttl_minutes))
    user_agent = request.headers.get("user-agent", "")
    client_host = request.client.host if request.client else ""
    with session() as s:
        s.execute(
            pg_insert(auth_magic_links).values(
                magic_link_id=uuid.uuid4(),
                token_hash=hash_session_token(token),
                node_id=settings.node_id,
                email=normalized_email,
                next_path=safe_next_path(next_path),
                issued_at=now,
                expires_at=expires_at,
                consumed_at=None,
                request_user_agent_hash=_optional_hash(user_agent),
                request_ip_hash=_optional_hash(client_host),
            )
        )
        s.commit()

    query = urllib.parse.urlencode({"token": token})
    link = f"{settings.auth_public_base_url.rstrip('/')}/auth/magic-link/consume?{query}"
    return MagicLinkIssue(email=normalized_email, link=link, expires_at=expires_at)


def deliver_magic_link_issue(issue: MagicLinkIssue, settings: Settings) -> str:
    delivery = settings.auth_magic_link_delivery.strip().lower()
    if delivery in {"", "none"}:
        return "none"
    if delivery == "console":
        subject, text_body, _html_body = magic_link_email_content(issue)
        print(
            "\n".join(
                [
                    "[orthus.auth.magic_link]",
                    f"to={issue.email}",
                    f"subject={subject}",
                    text_body,
                ]
            )
        )
        return "console"
    if delivery == "resend":
        return _deliver_magic_link_resend(issue, settings)
    raise HTTPException(status_code=500, detail="unsupported magic link delivery")


def magic_link_email_content(issue: MagicLinkIssue) -> tuple[str, str, str]:
    subject = "Orthus 로그인 링크"
    text_body = (
        "아래 링크로 로그인하세요.\n\n"
        f"{issue.link}\n\n"
        f"이 링크는 {issue.expires_at.isoformat()}까지 유효하며 한 번만 사용할 수 있습니다."
    )
    html_body = (
        "<p>아래 버튼으로 로그인하세요.</p>"
        f'<p><a href="{issue.link}">로그인</a></p>'
        f"<p>이 링크는 {issue.expires_at.isoformat()}까지 유효하며 한 번만 사용할 수 있습니다.</p>"
    )
    return subject, text_body, html_body


def _deliver_magic_link_resend(issue: MagicLinkIssue, settings: Settings) -> str:
    if not settings.resend_api_key:
        raise HTTPException(status_code=500, detail="resend api key not set")
    if not settings.auth_magic_link_from_email.strip():
        raise HTTPException(status_code=500, detail="magic link sender not set")

    subject, text_body, html_body = magic_link_email_content(issue)
    payload = {
        "from": settings.auth_magic_link_from_email,
        "to": [issue.email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
        "tags": [{"name": "kind", "value": "auth_magic_link"}],
    }
    try:
        with httpx.Client(
            base_url=settings.resend_base_url.rstrip("/"),
            timeout=settings.resend_timeout_seconds,
        ) as client:
            response = client.post(
                "/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _log_magic_link_delivery_failure(
            issue.email, "resend_http_status", exc.response.status_code
        )
        raise HTTPException(status_code=502, detail="resend delivery failed") from exc
    except httpx.HTTPError as exc:
        _log_magic_link_delivery_failure(issue.email, exc.__class__.__name__, None)
        raise HTTPException(status_code=502, detail="resend delivery failed") from exc
    return "resend"


def consume_magic_link_token(token: str, settings: Settings) -> tuple[UUID, str, str]:
    raw_token = token.strip()
    if not raw_token or len(raw_token) > 512:
        raise HTTPException(status_code=401, detail="magic link invalid")
    now = utcnow()
    with session() as s:
        row = s.execute(
            update(auth_magic_links)
            .where(
                auth_magic_links.c.token_hash == hash_session_token(raw_token),
                auth_magic_links.c.node_id == settings.node_id,
                auth_magic_links.c.consumed_at.is_(None),
            )
            .values(consumed_at=now)
            .returning(
                auth_magic_links.c.email,
                auth_magic_links.c.next_path,
                auth_magic_links.c.expires_at,
            )
        ).first()
        s.commit()
    if not row:
        raise HTTPException(status_code=401, detail="magic link invalid")
    if row.expires_at <= now:
        raise HTTPException(status_code=401, detail="magic link expired")

    email = str(row.email)
    profile = OAuthProfile(
        provider="magic_link",
        subject=email,
        email=email,
        email_verified=True,
        display_name=email,
    )
    user_id = user_id_for_allowed_profile(profile, settings)
    return user_id, safe_next_path(str(row.next_path)), email


def create_session_token(user_id: UUID, settings: Settings, request: Request) -> str:
    token = secrets.token_urlsafe(40)
    now = utcnow()
    absolute = now + timedelta(days=settings.auth_session_absolute_days)
    expires = min(now + timedelta(days=settings.auth_session_sliding_days), absolute)
    user_agent = request.headers.get("user-agent", "")
    client_host = request.client.host if request.client else ""
    with session() as s:
        s.execute(
            pg_insert(auth_sessions).values(
                session_id=uuid.uuid4(),
                token_hash=hash_session_token(token),
                user_id=user_id,
                node_id=settings.node_id,
                issued_at=now,
                last_seen_at=now,
                expires_at=expires,
                absolute_expires_at=absolute,
                user_agent_hash=_optional_hash(user_agent),
                ip_hash=_optional_hash(client_host),
            )
        )
        s.commit()
    # 로그인하는 순간 회사 팀원 행에 이 계정을 연결한다(이메일 매칭). 내 보드/배정이
    # 바로 이어지게 하는 best-effort 훅 — 실패해도 로그인은 막지 않는다.
    if settings.node_kind == "company":
        try:
            from orthus.dashboard import link_member_for_login

            link_member_for_login(settings.node_id, user_id)
        except Exception:
            pass
    return token


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=session_cookie_name(settings),
        value=token,
        max_age=settings.auth_session_absolute_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=session_cookie_name(settings),
        path="/",
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
    )


def revoke_session_request(request: Request, settings: Settings) -> None:
    token = request.cookies.get(session_cookie_name(settings))
    if not token:
        return
    with session() as s:
        s.execute(
            update(auth_sessions)
            .where(
                auth_sessions.c.token_hash == hash_session_token(token),
                auth_sessions.c.node_id == settings.node_id,
                auth_sessions.c.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        s.commit()


def allowlist_role(email: str, settings: Settings) -> str | None:
    ensure_bootstrap_allowlist(settings)
    with session() as s:
        row = s.execute(
            select(auth_allowlist.c.role).where(
                auth_allowlist.c.node_id == settings.node_id,
                auth_allowlist.c.email == normalize_email(email),
                auth_allowlist.c.revoked_at.is_(None),
            )
        ).first()
    return str(row.role) if row else None


def ensure_bootstrap_allowlist(settings: Settings) -> None:
    emails: list[tuple[str, str]] = []
    if settings.node_kind == "company":
        emails.extend((email, "admin") for email in _split_emails(settings.central_admin_emails))
    else:
        emails.extend((email, "owner") for email in _split_emails(settings.personal_owner_email))
    if not emails:
        return
    # Keyed by the resolved bootstrap config, not just node_id: production emails
    # are stable so this seeds once, but tests that re-point the admin email under
    # the same node_id still re-seed.
    cache_key = f"{settings.node_id}|{settings.node_kind}|{sorted(emails)}"
    if cache_key in _bootstrap_done:
        return
    now = utcnow()
    with session() as s:
        for email, role in emails:
            stmt = pg_insert(auth_allowlist).values(
                allowlist_id=uuid.uuid4(),
                node_id=settings.node_id,
                email=email,
                role=role,
                created_by=None,
                created_at=now,
                revoked_at=None,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[auth_allowlist.c.node_id, auth_allowlist.c.email],
                set_={"role": role, "revoked_at": None},
            )
            s.execute(stmt)
        s.commit()
    _bootstrap_done.add(cache_key)


def upsert_identity_user(profile: OAuthProfile) -> UUID:
    email = normalize_email(profile.email)
    now = utcnow()
    display_name = profile.display_name or email
    with session() as s:
        existing = s.execute(
            select(auth_identities.c.user_id).where(
                auth_identities.c.provider == profile.provider,
                auth_identities.c.provider_subject == profile.subject,
            )
        ).first()
        if existing:
            user_id = existing.user_id
            s.execute(
                update(auth_identities)
                .where(
                    auth_identities.c.provider == profile.provider,
                    auth_identities.c.provider_subject == profile.subject,
                )
                .values(
                    email=email,
                    email_verified=profile.email_verified,
                    display_name=display_name,
                    last_login_at=now,
                )
            )
            s.execute(
                update(users).where(users.c.user_id == user_id).values(display_name=display_name)
            )
            s.commit()
            return user_id

        by_email = s.execute(
            select(auth_identities.c.user_id).where(auth_identities.c.email == email).limit(1)
        ).first()
        user_id = by_email.user_id if by_email else uuid.uuid4()
        if not by_email:
            s.execute(
                pg_insert(users).values(
                    user_id=user_id,
                    external_id=f"{profile.provider}:{profile.subject}",
                    display_name=display_name,
                    preferred_timezone="Asia/Seoul",
                    created_at=now,
                )
            )
        s.execute(
            pg_insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=user_id,
                provider=profile.provider,
                provider_subject=profile.subject,
                email=email,
                email_verified=profile.email_verified,
                display_name=display_name,
                created_at=now,
                last_login_at=now,
            )
        )
        s.commit()
        return user_id


def session_cookie_name(settings: Settings) -> str:
    if settings.auth_cookie_name:
        return settings.auth_cookie_name
    safe_node = settings.node_id.replace("-", "_")
    return f"orthus_session_{safe_node}"


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_magic_link_email(value: str, settings: Settings) -> str:
    raw = normalize_email(value)
    domain = normalize_email(settings.auth_magic_link_domain).lstrip("@")
    if not domain or "@" in domain:
        raise HTTPException(status_code=500, detail="invalid magic link domain")
    if "@" in raw:
        local_part, _, input_domain = raw.partition("@")
        if input_domain != domain:
            raise HTTPException(status_code=422, detail="email domain not allowed")
    else:
        local_part = raw
    if not _valid_email_local_part(local_part):
        raise HTTPException(status_code=422, detail="invalid email local part")
    return f"{local_part}@{domain}"


def safe_next_path(next_path: str) -> str:
    value = (next_path or "").strip()
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or re.search(r"[\x00-\x1f\x7f]", value)
    ):
        return "/editor"
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return "/editor"
    return value


def oauth_redirect_uri(settings: Settings) -> str:
    return f"{settings.auth_public_base_url.rstrip('/')}/auth/oauth/google/callback"


def web_redirect_url(path: str, settings: Settings) -> str:
    path = safe_next_path(path)
    if settings.auth_web_base_url:
        return f"{settings.auth_web_base_url.rstrip('/')}{path}"
    public = settings.auth_public_base_url.rstrip("/")
    if public.endswith("/api"):
        return f"{public[:-4]}{path}"
    origins = settings.cors_origin_list()
    if origins:
        return f"{origins[0].rstrip('/')}{path}"
    return path


def oauth_login_error(exc: HTTPException) -> str:
    detail = str(exc.detail)
    return {
        "account not invited": "not_invited",
        "email not verified": "email_not_verified",
        "google token exchange failed": "oauth_exchange_failed",
        "google id_token missing": "oauth_invalid_response",
        "google id_token invalid": "oauth_invalid_response",
        "google audience invalid": "oauth_invalid_response",
        "google issuer invalid": "oauth_invalid_response",
        "google exp invalid": "oauth_invalid_response",
        "google id_token expired": "oauth_expired",
        "google email missing": "oauth_invalid_response",
        "google nonce invalid": "oauth_invalid_state",
        "invalid oauth state": "oauth_invalid_state",
        "invalid oauth nonce": "oauth_invalid_state",
        "jwt expired": "oauth_invalid_state",
        "jwt signature invalid": "oauth_invalid_state",
        "jwt decode failed": "oauth_invalid_state",
    }.get(detail, "oauth_failed")


def require_node_operator(current: AuthenticatedUser) -> None:
    """Require admin-grade UI authority for node-local command triggers."""
    if current.auth_mode == "session" and current.role not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="node operator required")


# --- Cross-node SSO session bridge ------------------------------------------
# Identity is always established at the hub (central) via its own magic-link
# login. The hub then mints a short-lived, single-use, audience-bound ticket
# that a relying-party node (personal) consumes to mint its OWN local session.
# Each node keeps its own allowlist + session table + host-scoped cookie: the
# hub only asserts identity, it does not grant access on another node.

SSO_TICKET_KIND = "sso_ticket"


def sso_hub_enabled(settings: Settings) -> bool:
    return bool(settings.sso_shared_secret and settings.sso_trusted_origin_list())


def sso_rp_enabled(settings: Settings) -> bool:
    return bool(settings.sso_shared_secret and settings.sso_hub_base_url)


def sso_desktop_enabled(settings: Settings) -> bool:
    return bool(settings.sso_shared_secret and settings.sso_desktop_return_uri_list())


def sso_role_value(settings: Settings) -> str:
    if sso_hub_enabled(settings):
        return "hub"
    if sso_rp_enabled(settings):
        return "rp"
    return "off"


def _origin_of(url: str) -> str:
    parts = urllib.parse.urlsplit((url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def self_api_origin(settings: Settings) -> str:
    return _origin_of(settings.auth_public_base_url)


def sso_consume_return_url(settings: Settings) -> str:
    """RP-side: the absolute consume URL the hub redirects the ticket back to."""
    return f"{settings.auth_public_base_url.rstrip('/')}/auth/sso/consume"


def validate_sso_return_to(return_to: str, settings: Settings) -> str:
    """Hub-side: validate an RP consume URL and return its trusted origin.

    A desktop return_to is an exact custom-scheme URI (e.g. orthus://sso/consume)
    and acts as its own audience; the http(s) origin path is unchanged.
    """
    value = (return_to or "").strip()
    if value in set(settings.sso_desktop_return_uri_list()):
        return value
    parsed = urllib.parse.urlsplit(value)
    origin = _origin_of(value)
    if (
        not origin
        or parsed.scheme not in {"http", "https"}
        or not parsed.path.endswith("/auth/sso/consume")
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=400, detail="sso return_to invalid")
    if origin not in set(settings.sso_trusted_origin_list()):
        raise HTTPException(status_code=400, detail="sso return_to not trusted")
    return origin


def mint_sso_ticket(email: str, sub: str, audience_origin: str, settings: Settings) -> str:
    if not settings.sso_shared_secret:
        raise HTTPException(status_code=500, detail="sso shared secret not set")
    now = int(time.time())
    claims = {
        "kind": SSO_TICKET_KIND,
        "jti": secrets.token_urlsafe(24),
        "sub": sub,
        "email": normalize_email(email),
        "aud": audience_origin,
        "iss": self_api_origin(settings),
        "iat": now,
        "exp": now + max(5, settings.sso_ticket_ttl_seconds),
    }
    return sign_hs256_jwt(claims, settings.sso_shared_secret)


def verify_and_consume_sso_ticket(ticket: str, settings: Settings) -> str:
    """RP-side: check signature/exp/kind/audience + single-use; return the email.

    Raises HTTPException(401) on any failure so the route can fail closed.
    """
    if not settings.sso_shared_secret:
        raise HTTPException(status_code=500, detail="sso shared secret not set")
    raw = (ticket or "").strip()
    if not raw or len(raw) > 4096:
        raise HTTPException(status_code=401, detail="sso ticket invalid")
    claims = decode_hs256_jwt(raw, settings.sso_shared_secret)  # signature + exp
    if claims.get("kind") != SSO_TICKET_KIND:
        raise HTTPException(status_code=401, detail="sso ticket kind invalid")
    audience = claims.get("aud")
    valid_audiences = {self_api_origin(settings), *settings.sso_desktop_return_uri_list()}
    if not isinstance(audience, str) or audience not in valid_audiences:
        raise HTTPException(status_code=401, detail="sso audience invalid")
    email = normalize_email(str(claims.get("email") or ""))
    if "@" not in email:
        raise HTTPException(status_code=401, detail="sso email invalid")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not jti:
        raise HTTPException(status_code=401, detail="sso ticket id missing")
    if not _claim_sso_jti(jti, settings):
        raise HTTPException(status_code=401, detail="sso ticket replay")
    return email


def _claim_sso_jti(jti: str, settings: Settings) -> bool:
    """Insert the jti once; return False if already consumed (replay)."""
    now = utcnow()
    with session() as s:
        row = s.execute(
            pg_insert(auth_sso_tickets)
            .values(jti=jti, node_id=settings.node_id, consumed_at=now)
            .on_conflict_do_nothing(index_elements=[auth_sso_tickets.c.jti])
            .returning(auth_sso_tickets.c.jti)
        ).first()
        s.commit()
    return row is not None


def sso_login_error(exc: HTTPException) -> str:
    detail = str(exc.detail)
    if detail in {"account not invited", "account not allowed"}:
        return "not_invited"
    if detail == "email not verified":
        return "email_not_verified"
    return "sso_failed"


def is_local_auth_base(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", ""} and host in LOCAL_AUTH_HOSTS


def decode_unsigned_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise HTTPException(status_code=401, detail="jwt payload invalid")
    return _decode_json(parts[1])


def utcnow() -> datetime:
    return datetime.now(UTC)


def _session_secret(settings: Settings) -> str:
    secret = settings.auth_session_secret or settings.auth_jwt_secret
    if not secret:
        raise HTTPException(status_code=500, detail="ORTHUS_AUTH_SESSION_SECRET not set")
    return secret


def _user_from_id(
    user_id: UUID,
    auth_mode: str,
    role: str | None = None,
    node_id: str | None = None,
) -> AuthenticatedUser:
    with session() as s:
        row = s.execute(select(users.c.display_name).where(users.c.user_id == user_id)).first()
    if not row:
        raise HTTPException(status_code=401, detail="user not found")
    return AuthenticatedUser(
        user_id=user_id,
        auth_mode=auth_mode,
        display_name=row.display_name,
        role=role,
        node_id=node_id,
    )


def _demo_user_id() -> UUID:
    with session() as s:
        rows = s.execute(select(users.c.user_id).limit(2)).all()
    if not rows:
        raise HTTPException(status_code=404, detail="no users; run `make seed`")
    if len(rows) > 1:
        raise HTTPException(status_code=400, detail="multiple users; pass X-User-Id")
    return rows[0][0]


def _split_emails(value: str) -> list[str]:
    return [email for email in (normalize_email(v) for v in value.split(",")) if email]


def _claim_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def _optional_hash(value: str) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _log_magic_link_delivery_failure(email: str, reason: str, status_code: int | None) -> None:
    print(
        json.dumps(
            {
                "event": "auth.magic_link.delivery_failed",
                "delivery": "resend",
                "email_hash": _optional_hash(normalize_email(email)),
                "reason": reason,
                "status_code": status_code,
            },
            separators=(",", ":"),
        )
    )


def _valid_email_local_part(value: str) -> bool:
    if len(value) > 64 or not value:
        return False
    if value.startswith(".") or value.endswith(".") or ".." in value:
        return False
    return re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", value) is not None


def _decode_json(value: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(_pad_b64(value))
        decoded = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="jwt decode failed") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(status_code=401, detail="jwt payload invalid")
    return decoded


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _pad_b64(value: str) -> str:
    return value + "=" * (-len(value) % 4)
