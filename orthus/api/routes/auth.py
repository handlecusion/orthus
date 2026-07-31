"""S1 auth routes: Google OAuth, session, and allowlist management."""

from __future__ import annotations

import uuid
from datetime import datetime
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.api.deps import get_current_user
from orthus.auth import (
    AuthenticatedUser,
    MANAGER_ROLES,
    OAuthProfile,
    allowlist_role,
    begin_google_oauth_url,
    clear_session_cookie,
    complete_google_oauth,
    consume_magic_link_token,
    create_session_token,
    create_magic_link_issue,
    deliver_magic_link_issue,
    ensure_bootstrap_allowlist,
    is_local_auth_base,
    mint_sso_ticket,
    normalize_email,
    normalize_magic_link_email,
    oauth_login_error,
    revoke_session_request,
    safe_next_path,
    set_session_cookie,
    sso_consume_return_url,
    sso_desktop_enabled,
    sso_hub_enabled,
    sso_login_error,
    sso_role_value,
    sso_rp_enabled,
    user_from_session_request,
    utcnow,
    user_id_for_allowed_profile,
    validate_sso_return_to,
    verify_and_consume_sso_ticket,
    web_redirect_url,
)
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import auth_allowlist

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthConfigOut(BaseModel):
    auth_mode: str
    node_kind: str
    node_id: str
    provider: str
    provider_configured: bool
    dev_login_enabled: bool
    login_url: str
    email_domain: str
    sso_role: str  # "hub" | "rp" | "off" — cross-node session bridge role
    owner_scope_enabled: bool  # P8.5: central merges the logged-in user's own personal data


class AuthMeOut(BaseModel):
    authenticated: bool
    auth_mode: str
    user_id: UUID
    display_name: str
    email: str | None
    role: str | None
    node_id: str


class DevLoginIn(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _emailish(cls, value: str) -> str:
        email = normalize_email(value)
        if "@" not in email:
            raise ValueError("email required")
        return email


class MagicLinkRequestIn(BaseModel):
    email: str
    next: str = "/editor"


class MagicLinkRequestOut(BaseModel):
    accepted: bool
    delivery: str
    email_domain: str


class AllowlistEntryOut(BaseModel):
    allowlist_id: UUID
    node_id: str
    email: str
    role: str
    created_by: UUID | None
    created_at: datetime | None
    revoked_at: datetime | None


class AllowlistEntryIn(BaseModel):
    email: str
    role: str = Field(pattern="^(owner|admin|member|viewer)$")

    @field_validator("email")
    @classmethod
    def _emailish(cls, value: str) -> str:
        email = normalize_email(value)
        if "@" not in email:
            raise ValueError("email required")
        return email


@router.get("/config", response_model=AuthConfigOut)
def auth_config() -> AuthConfigOut:
    settings = get_settings()
    return AuthConfigOut(
        auth_mode=settings.auth_mode,
        node_kind=settings.node_kind,
        node_id=settings.node_id,
        provider="magic_link",
        provider_configured=_magic_link_provider_configured(settings),
        dev_login_enabled=settings.auth_dev_login_enabled,
        login_url="/auth/magic-link/request",
        email_domain=settings.auth_magic_link_domain,
        sso_role=sso_role_value(settings),
        owner_scope_enabled=settings.owner_scope_enabled,
    )


@router.get("/me", response_model=AuthMeOut)
def auth_me(current: AuthenticatedUser = Depends(get_current_user)) -> AuthMeOut:
    settings = get_settings()
    return AuthMeOut(
        authenticated=True,
        auth_mode=current.auth_mode,
        user_id=current.user_id,
        display_name=current.display_name,
        email=current.email,
        role=current.role,
        node_id=current.node_id or settings.node_id,
    )


@router.get("/oauth/google/start")
def google_start(next: str = Query(default="/editor")) -> RedirectResponse:
    settings = get_settings()
    if settings.auth_mode != "session":
        raise HTTPException(status_code=400, detail="session auth not enabled")
    return RedirectResponse(begin_google_oauth_url(next, settings), status_code=302)


@router.get("/oauth/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    settings = get_settings()
    if settings.auth_mode != "session":
        raise HTTPException(status_code=400, detail="session auth not enabled")
    if error:
        return RedirectResponse(
            web_redirect_url(f"/login?error={quote(error, safe='')}", settings),
            status_code=302,
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="oauth code/state missing")

    try:
        profile, next_path = complete_google_oauth(code, state, settings)
        user_id = user_id_for_allowed_profile(profile, settings)
    except HTTPException as exc:
        if exc.status_code in {401, 403}:
            error_code = oauth_login_error(exc)
            return RedirectResponse(
                web_redirect_url(f"/login?error={quote(error_code, safe='')}", settings),
                status_code=302,
            )
        raise

    token = create_session_token(user_id, settings, request)
    response = RedirectResponse(web_redirect_url(next_path, settings), status_code=302)
    set_session_cookie(response, token, settings)
    return response


@router.get("/sso/start")
def sso_start(
    next: str = Query(default="/editor"),
    silent: int = Query(default=0),
) -> RedirectResponse:
    """RP-side entry: bounce the browser to the hub to obtain a session ticket."""
    settings = get_settings()
    if settings.auth_mode != "session" or not sso_rp_enabled(settings):
        raise HTTPException(status_code=404, detail="sso not enabled")
    params = {"return_to": sso_consume_return_url(settings), "next": safe_next_path(next)}
    if silent:
        params["silent"] = "1"
    hub = settings.sso_hub_base_url.rstrip("/")
    return RedirectResponse(f"{hub}/auth/sso/issue?{urlencode(params)}", status_code=302)


@router.get("/sso/issue")
def sso_issue(
    request: Request,
    return_to: str = Query(default=""),
    next: str = Query(default="/editor"),
    silent: int = Query(default=0),
) -> RedirectResponse:
    """Hub-side: mint a ticket for an authenticated user, or route them to login."""
    settings = get_settings()
    if settings.auth_mode != "session" or not (
        sso_hub_enabled(settings) or sso_desktop_enabled(settings)
    ):
        raise HTTPException(status_code=404, detail="sso not enabled")
    audience = validate_sso_return_to(return_to, settings)
    next_path = safe_next_path(next)

    try:
        current = user_from_session_request(request, settings)
    except HTTPException as exc:
        if exc.status_code not in {401, 403}:
            raise
        current = None

    if current is None or not current.email:
        if silent:
            # No hub session under a silent probe: bounce back without a ticket so
            # the RP shows its own login. The RP guards against re-probing.
            bounce = urlencode({"sso": "nosession", "next": next_path})
            return RedirectResponse(f"{return_to}?{bounce}", status_code=302)
        # Interactive: log in at the hub, then resume issue via the continue page.
        resume = "/sso-continue?" + urlencode({"return_to": return_to, "next": next_path})
        login_path = "/login?" + urlencode({"next": resume})
        return RedirectResponse(web_redirect_url(login_path, settings), status_code=302)

    ticket = mint_sso_ticket(current.email, str(current.user_id), audience, settings)
    forward = urlencode({"ticket": ticket, "next": next_path})
    return RedirectResponse(f"{return_to}?{forward}", status_code=302)


@router.get("/sso/consume")
def sso_consume(
    request: Request,
    ticket: str | None = None,
    next: str = Query(default="/editor"),
    sso: str | None = None,
) -> RedirectResponse:
    """RP-side: verify a hub ticket and mint a local session cookie."""
    settings = get_settings()
    if settings.auth_mode != "session" or not (
        sso_rp_enabled(settings) or sso_desktop_enabled(settings)
    ):
        raise HTTPException(status_code=404, detail="sso not enabled")
    next_path = safe_next_path(next)
    if not ticket:
        # Hub had no session (silent bounce): fall back to the RP's own login.
        login_path = "/login?" + urlencode({"next": next_path, "sso": "nosession"})
        return RedirectResponse(web_redirect_url(login_path, settings), status_code=302)

    try:
        email = verify_and_consume_sso_ticket(ticket, settings)
        profile = OAuthProfile(
            provider="magic_link",
            subject=email,
            email=email,
            email_verified=True,
            display_name=email,
        )
        user_id = user_id_for_allowed_profile(profile, settings)
    except HTTPException as exc:
        if exc.status_code in {401, 403}:
            error_code = sso_login_error(exc)
            return RedirectResponse(
                web_redirect_url(f"/login?error={quote(error_code, safe='')}", settings),
                status_code=302,
            )
        raise

    token = create_session_token(user_id, settings, request)
    response = RedirectResponse(web_redirect_url(next_path, settings), status_code=302)
    set_session_cookie(response, token, settings)
    return response


@router.post("/magic-link/request", response_model=MagicLinkRequestOut)
def request_magic_link(body: MagicLinkRequestIn, request: Request) -> MagicLinkRequestOut:
    settings = get_settings()
    if settings.auth_mode != "session":
        raise HTTPException(status_code=400, detail="session auth not enabled")

    delivery = settings.auth_magic_link_delivery.strip().lower() or "none"
    if delivery not in {"none", "console", "resend"}:
        raise HTTPException(status_code=500, detail="unsupported magic link delivery")
    email = normalize_magic_link_email(body.email, settings)

    # Keep response identical for invited and uninvited accounts.
    if allowlist_role(email, settings) is not None:
        try:
            issue = create_magic_link_issue(email, body.next, settings, request)
            delivery = deliver_magic_link_issue(issue, settings)
        except HTTPException as exc:
            if exc.status_code not in {403, 500, 502}:
                raise

    return MagicLinkRequestOut(
        accepted=True,
        delivery=delivery,
        email_domain=settings.auth_magic_link_domain,
    )


@router.get("/magic-link/consume")
def consume_magic_link(request: Request, token: str = Query(default="")) -> RedirectResponse:
    settings = get_settings()
    if settings.auth_mode != "session":
        raise HTTPException(status_code=400, detail="session auth not enabled")
    try:
        user_id, next_path, _email = consume_magic_link_token(token, settings)
    except HTTPException as exc:
        if exc.status_code in {401, 403}:
            return RedirectResponse(
                web_redirect_url("/login?error=magic_link_invalid", settings),
                status_code=302,
            )
        raise

    session_token = create_session_token(user_id, settings, request)
    response = RedirectResponse(web_redirect_url(next_path, settings), status_code=302)
    set_session_cookie(response, session_token, settings)
    return response


def _magic_link_provider_configured(settings) -> bool:
    delivery = settings.auth_magic_link_delivery.strip().lower()
    if delivery == "console":
        return True
    if delivery == "resend":
        return bool(settings.resend_api_key and settings.auth_magic_link_from_email.strip())
    return False


@router.post("/dev-login", response_model=AuthMeOut)
def dev_login(body: DevLoginIn, request: Request) -> JSONResponse:
    settings = get_settings()
    if settings.auth_mode != "session" or not settings.auth_dev_login_enabled:
        raise HTTPException(status_code=404, detail="dev login disabled")
    if not is_local_auth_base(settings.auth_public_base_url):
        raise HTTPException(status_code=403, detail="dev login requires local auth base")
    email = normalize_email(str(body.email))
    profile = OAuthProfile(
        provider="dev",
        subject=email,
        email=email,
        email_verified=True,
        display_name=email,
    )
    user_id = user_id_for_allowed_profile(profile, settings)
    token = create_session_token(user_id, settings, request)
    role = allowlist_role(email, settings)
    payload = AuthMeOut(
        authenticated=True,
        auth_mode="session",
        user_id=user_id,
        display_name=email,
        email=email,
        role=role,
        node_id=settings.node_id,
    ).model_dump(mode="json")
    response = JSONResponse(payload)
    set_session_cookie(response, token, settings)
    return response


@router.post("/logout", status_code=204)
def logout(request: Request) -> Response:
    settings = get_settings()
    revoke_session_request(request, settings)
    response = Response(status_code=204)
    clear_session_cookie(response, settings)
    return response


@router.get("/allowlist", response_model=list[AllowlistEntryOut])
def list_allowlist(
    current: AuthenticatedUser = Depends(get_current_user),
) -> list[AllowlistEntryOut]:
    _require_allowlist_admin(current)
    settings = get_settings()
    ensure_bootstrap_allowlist(settings)
    with session() as s:
        rows = s.execute(
            select(
                auth_allowlist.c.allowlist_id,
                auth_allowlist.c.node_id,
                auth_allowlist.c.email,
                auth_allowlist.c.role,
                auth_allowlist.c.created_by,
                auth_allowlist.c.created_at,
                auth_allowlist.c.revoked_at,
            )
            .where(auth_allowlist.c.node_id == settings.node_id)
            .order_by(auth_allowlist.c.email.asc())
        ).all()
    return [
        AllowlistEntryOut(
            allowlist_id=r.allowlist_id,
            node_id=r.node_id,
            email=r.email,
            role=r.role,
            created_by=r.created_by,
            created_at=r.created_at,
            revoked_at=r.revoked_at,
        )
        for r in rows
    ]


@router.post("/allowlist", response_model=AllowlistEntryOut, status_code=201)
def upsert_allowlist_entry(
    body: AllowlistEntryIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> AllowlistEntryOut:
    _require_allowlist_admin(current)
    settings = get_settings()
    email = normalize_email(str(body.email))
    now = utcnow()
    with session() as s:
        ensure_bootstrap_allowlist(settings)
        _reject_last_manager_change(s, settings.node_id, email, new_role=body.role)
        stmt = pg_insert(auth_allowlist).values(
            allowlist_id=uuid.uuid4(),
            node_id=settings.node_id,
            email=email,
            role=body.role,
            created_by=current.user_id,
            created_at=now,
            revoked_at=None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[auth_allowlist.c.node_id, auth_allowlist.c.email],
            set_={"role": body.role, "created_by": current.user_id, "revoked_at": None},
        ).returning(auth_allowlist)
        row = s.execute(stmt).first()
        s.commit()
    if not row:
        raise HTTPException(status_code=500, detail="allowlist write failed")
    return _allowlist_out(row)


@router.delete("/allowlist/{email}", status_code=204)
def revoke_allowlist_entry(
    email: str,
    current: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    _require_allowlist_admin(current)
    settings = get_settings()
    if current.email and normalize_email(email) == normalize_email(current.email):
        raise HTTPException(status_code=400, detail="cannot revoke own account")
    target_email = normalize_email(email)
    with session() as s:
        ensure_bootstrap_allowlist(settings)
        _reject_last_manager_change(s, settings.node_id, target_email, revoke=True)
        s.execute(
            update(auth_allowlist)
            .where(
                auth_allowlist.c.node_id == settings.node_id,
                auth_allowlist.c.email == target_email,
            )
            .values(revoked_at=utcnow())
        )
        s.commit()
    return Response(status_code=204)


def _require_allowlist_admin(current: AuthenticatedUser) -> None:
    if current.auth_mode != "session" or current.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="allowlist admin required")


def _reject_last_manager_change(
    s,
    node_id: str,
    email: str,
    *,
    new_role: str | None = None,
    revoke: bool = False,
) -> None:
    rows = s.execute(
        select(auth_allowlist.c.email, auth_allowlist.c.role).where(
            auth_allowlist.c.node_id == node_id,
            auth_allowlist.c.revoked_at.is_(None),
        )
    ).all()
    active_roles = {normalize_email(row.email): str(row.role) for row in rows}
    target_current = active_roles.get(normalize_email(email))
    if target_current not in MANAGER_ROLES:
        return
    manager_count = sum(1 for role in active_roles.values() if role in MANAGER_ROLES)
    target_remains_manager = new_role in MANAGER_ROLES and not revoke
    if manager_count <= 1 and not target_remains_manager:
        raise HTTPException(status_code=400, detail="last owner/admin required")


def _allowlist_out(row) -> AllowlistEntryOut:
    data = row._mapping if hasattr(row, "_mapping") else row
    return AllowlistEntryOut(
        allowlist_id=data["allowlist_id"],
        node_id=data["node_id"],
        email=data["email"],
        role=data["role"],
        created_by=data["created_by"],
        created_at=data["created_at"],
        revoked_at=data["revoked_at"],
    )
