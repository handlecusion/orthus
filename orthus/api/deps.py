"""Request dependencies for demo and JWT identity modes."""

from __future__ import annotations

from uuid import UUID

from fastapi import Header, Request, Response

from orthus.auth import (
    AuthenticatedUser,
    authenticate_request,
    session_cookie_name,
    set_session_cookie,
)
from orthus.settings import get_settings


def get_current_user(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> AuthenticatedUser:
    settings = get_settings()
    user = authenticate_request(request, settings, authorization, x_user_id)
    # Roll the session cookie on every authenticated request so an actively used
    # session never lapses browser-side. The cookie Max-Age would otherwise stay
    # pinned to login time and drop at the absolute window even for active users;
    # re-issuing it here keeps "log in once, stay logged in" true on mobile. DB
    # sliding/absolute expiry is still enforced in authenticate_request.
    if settings.auth_mode == "session":
        token = request.cookies.get(session_cookie_name(settings))
        if token:
            set_session_cookie(response, token, settings)
    return user


def get_user_id(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> UUID:
    return get_current_user(request, response, authorization, x_user_id).user_id
