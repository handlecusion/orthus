"""P8.7b dual-auth: session/demo/jwt identity OR a knowledge-scoped collector token.

`get_session_user_or_knowledge_token` accepts EITHER the normal browser/demo/jwt
identity (via `authenticate_request`) OR a collector token carrying the
`knowledge` scope. The token path resolves to its owning `user_id` with
`role=None` and `auth_mode="collector_token"`, so the existing owner-row filters
(the real privacy boundary) still apply while `require_node_operator` — which
only gates `auth_mode == "session"` — is a no-op for tokens.

A collector token still authenticates the knowledge surface ONLY: it never gains
a browser session, and the session/demo/jwt path never satisfies the token
branch. Routes that must stay token-inaccessible (promote, allowlist, command
create, agent-work decisions) keep using `get_current_user`, which has no token
path at all.

`POST /ask` under token auth is additionally rate limited per token to
30 calls / 5 minutes (in-process, fail-closed 429 beyond). Session/demo/jwt
callers are never rate limited here.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from uuid import UUID

from fastapi import Header, HTTPException, Request

from orthus.auth import AuthenticatedUser, authenticate_request
from orthus.collector.auth import (
    CollectorAuthError,
    CollectorDisabledError,
    assert_collector_api_enabled,
    authenticate_collector_token,
    effective_scopes,
)
from orthus.settings import get_settings

KNOWLEDGE_SCOPE = "knowledge"
KNOWLEDGE_WRITE_SCOPE = "knowledge:write"
COLLECTOR_TOKEN_AUTH_MODE = "collector_token"

# Per-token /ask rate limit. In-process only (the central runtime is a single
# process); a multi-process deployment would move this to a shared store.
ASK_RATE_LIMIT_CALLS = 30
ASK_RATE_LIMIT_WINDOW_SECONDS = 300


def _is_collector_bearer(authorization: str | None) -> bool:
    scheme, _, token = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and token.strip().startswith("dct_")


def _knowledge_token_user(authorization: str | None, *, required_scope: str) -> AuthenticatedUser:
    settings = get_settings()
    try:
        assert_collector_api_enabled(settings)
    except CollectorDisabledError:
        # The collector surface is hidden on this node; treat the bearer as an
        # ordinary invalid credential rather than revealing the gate.
        raise HTTPException(status_code=401, detail="collector token invalid") from None
    try:
        token = authenticate_collector_token(authorization, settings)
    except CollectorAuthError:
        raise HTTPException(status_code=401, detail="collector token invalid") from None
    if required_scope not in effective_scopes(token.scopes):
        raise HTTPException(status_code=403, detail=f"scope '{required_scope}' required")
    return AuthenticatedUser(
        user_id=token.user_id,
        auth_mode=COLLECTOR_TOKEN_AUTH_MODE,
        display_name="Knowledge Token",
        email=None,
        role=None,
        node_id=token.node_id,
    )


def get_session_user_or_knowledge_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> AuthenticatedUser:
    """Dual-auth for read knowledge endpoints (scope `knowledge`).

    A `dct_` bearer authenticates as a knowledge-scoped collector token; anything
    else falls through to the normal session/demo/jwt identity unchanged."""
    if _is_collector_bearer(authorization):
        return _knowledge_token_user(authorization, required_scope=KNOWLEDGE_SCOPE)
    return authenticate_request(request, get_settings(), authorization, x_user_id)


def get_session_user_or_knowledge_write_token(
    request: Request,
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> AuthenticatedUser:
    """Dual-auth for the wiki-task candidate endpoint (scope `knowledge:write`)."""
    if _is_collector_bearer(authorization):
        return _knowledge_token_user(authorization, required_scope=KNOWLEDGE_WRITE_SCOPE)
    return authenticate_request(request, get_settings(), authorization, x_user_id)


class _TokenAskRateLimiter:
    """Sliding-window counter keyed by collector-token user_id."""

    def __init__(self, *, calls: int, window_seconds: int) -> None:
        self._calls = calls
        self._window = window_seconds
        self._lock = threading.Lock()
        self._hits: dict[UUID, deque[float]] = {}

    def check(self, user_id: UUID, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        cutoff = now - self._window
        with self._lock:
            bucket = self._hits.setdefault(user_id, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._calls:
                return False
            bucket.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_ask_rate_limiter = _TokenAskRateLimiter(
    calls=ASK_RATE_LIMIT_CALLS, window_seconds=ASK_RATE_LIMIT_WINDOW_SECONDS
)


def enforce_token_ask_rate_limit(current: AuthenticatedUser) -> None:
    """429 once a token exceeds the /ask window; session/demo/jwt are exempt."""
    if current.auth_mode != COLLECTOR_TOKEN_AUTH_MODE:
        return
    if not _ask_rate_limiter.check(current.user_id):
        raise HTTPException(status_code=429, detail="ask rate limit exceeded")


def reset_token_ask_rate_limit() -> None:
    """Test hook: clear the in-process window between cases."""
    _ask_rate_limiter.reset()


# Per-token POST /wiki/tasks (candidate) limit + size caps. A `knowledge:write`
# token is owner-bound, but an injected agent could spam the shared review queue
# or stuff a huge note. Lower window than /ask since candidate submission is rare.
# Session/demo/jwt callers (trusted operators) are exempt.
WIKI_TASK_RATE_LIMIT_CALLS = 10
WIKI_TASK_RATE_LIMIT_WINDOW_SECONDS = 300
WIKI_TASK_MAX_NOTE_CHARS = 8000
WIKI_TASK_MAX_EVIDENCE_URLS = 20

_wiki_task_rate_limiter = _TokenAskRateLimiter(
    calls=WIKI_TASK_RATE_LIMIT_CALLS, window_seconds=WIKI_TASK_RATE_LIMIT_WINDOW_SECONDS
)


def enforce_token_wiki_task_rate_limit(current: AuthenticatedUser) -> None:
    """429 once a token exceeds the wiki-task candidate window; non-token exempt."""
    if current.auth_mode != COLLECTOR_TOKEN_AUTH_MODE:
        return
    if not _wiki_task_rate_limiter.check(current.user_id):
        raise HTTPException(status_code=429, detail="wiki task rate limit exceeded")


def reset_token_wiki_task_rate_limit() -> None:
    """Test hook: clear the in-process window between cases."""
    _wiki_task_rate_limiter.reset()


# Per-token dashboard/personal-board write limit. The shared team calendar and the
# owner's personal schedule become writable by a `knowledge:write` token (the
# owner's orthus-mcp / orthus CLI). An owner-bound token is trusted, but an injected
# agent could spam or delete shared calendar rows; cap token writes. Session/demo/jwt
# operators are exempt. Lower than /ask since calendar/board edits are infrequent.
DASHBOARD_WRITE_RATE_LIMIT_CALLS = 30
DASHBOARD_WRITE_RATE_LIMIT_WINDOW_SECONDS = 300

_dashboard_write_rate_limiter = _TokenAskRateLimiter(
    calls=DASHBOARD_WRITE_RATE_LIMIT_CALLS,
    window_seconds=DASHBOARD_WRITE_RATE_LIMIT_WINDOW_SECONDS,
)


def enforce_token_dashboard_write_rate_limit(current: AuthenticatedUser) -> None:
    """429 once a token exceeds the dashboard/board write window; non-token exempt."""
    if current.auth_mode != COLLECTOR_TOKEN_AUTH_MODE:
        return
    if not _dashboard_write_rate_limiter.check(current.user_id):
        raise HTTPException(status_code=429, detail="dashboard write rate limit exceeded")


def reset_token_dashboard_write_rate_limit() -> None:
    """Test hook: clear the in-process window between cases."""
    _dashboard_write_rate_limiter.reset()
