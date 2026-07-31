"""Collector token authentication, separate from browser/JWT identity.

A collector token is a `dct_<random>` bearer string. The DB stores only its
sha256 hash. Authentication looks up an active (not revoked) token whose
`node_id` matches the current node, resolves the owner user, and stamps
`last_used_at`. This dependency is deliberately distinct from
`get_current_user`: session cookies, JWTs, and `X-User-Id` never satisfy it,
and a collector token never satisfies the browser-session path.

P8.7a adds per-token scopes. The DB stores a `scopes TEXT[]`; the app enforces
the allowed vocabulary (`ALLOWED_SCOPES`) and ignores unknown scopes rather
than erroring, so a future scope can be issued before this code understands it.
`require_scope` is the FastAPI dependency factory that gates a route on one
scope; `effective_scopes` documents the backcompat rule that `ingest` implies
`commands` so pre-P8.7a daemon tokens (default `{ingest}`) keep polling.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select, update

from orthus.db import session
from orthus.settings import Settings, get_settings
from orthus.tables import collector_tokens

COLLECTOR_TOKEN_PREFIX = "dct_"

# App-enforced scope vocabulary. Unknown scopes stored in the DB are ignored
# (not fatal) so a token can be issued with a scope this code predates.
# TODO: the `agent_task` command path does not yet hard-enforce a per-token
# `agent_task` scope (it runs under the existing `commands` poll/claim/complete
# scope). The scope is registered here only so a token can be issued with it
# ahead of that enforcement.
ALLOWED_SCOPES = frozenset({"ingest", "commands", "knowledge", "knowledge:write", "agent_task"})


@dataclass(frozen=True)
class CollectorToken:
    token_id: UUID
    user_id: UUID
    node_id: str
    scopes: frozenset[str]
    # Per-token machine identity (migration 0070). "" = legacy/deviceless
    # token, which keeps the exact pre-device routing/account behavior. The
    # node_id match in authenticate_collector_token is unchanged.
    device_id: str = ""


class CollectorDisabledError(RuntimeError):
    """Raised when this node should not expose the collector surface."""


class CollectorAuthError(RuntimeError):
    """Raised when a collector bearer token is absent or invalid."""


def assert_collector_api_enabled(settings: Settings) -> None:
    if not settings.collector_api_enabled or settings.node_kind != "company":
        raise CollectorDisabledError("collector api disabled")


def hash_collector_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _known_scopes(raw: Iterable[str] | None) -> frozenset[str]:
    """Keep only scopes in the app vocabulary; ignore unknown DB values."""
    return frozenset(s for s in (raw or ()) if s in ALLOWED_SCOPES)


def effective_scopes(scopes: frozenset[str]) -> frozenset[str]:
    """Resolve the scopes a token actually grants.

    Backcompat: pre-P8.7a tokens default to `{ingest}` only, and the daemon
    both ingests and runs the command lifecycle on that single scope. So
    `ingest` implies `commands` here; without this, already-issued daemon
    tokens would lose queue access the moment command routes require
    `commands`.
    """
    if "ingest" in scopes:
        return scopes | {"commands"}
    return scopes


def authenticate_collector_token(authorization: str | None, settings: Settings) -> CollectorToken:
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or not token.startswith(COLLECTOR_TOKEN_PREFIX):
        raise CollectorAuthError("collector token invalid")
    token_hash = hash_collector_token(token)
    now = datetime.now(UTC)
    with session() as s:
        row = s.execute(
            select(
                collector_tokens.c.token_id,
                collector_tokens.c.user_id,
                collector_tokens.c.node_id,
                collector_tokens.c.scopes,
                collector_tokens.c.device_id,
            ).where(
                collector_tokens.c.token_hash == token_hash,
                collector_tokens.c.node_id == settings.node_id,
                collector_tokens.c.revoked_at.is_(None),
            )
        ).first()
        if not row:
            raise CollectorAuthError("collector token invalid")
        s.execute(
            update(collector_tokens)
            .where(collector_tokens.c.token_id == row.token_id)
            .values(last_used_at=now)
        )
        s.commit()
    return CollectorToken(
        token_id=row.token_id,
        user_id=row.user_id,
        node_id=row.node_id,
        scopes=_known_scopes(row.scopes),
        device_id=row.device_id or "",
    )


def collector_token_auth(authorization: str | None = Header(default=None)) -> CollectorToken:
    """FastAPI dependency: fail-closed enable gate + collector-token auth."""
    settings = get_settings()
    try:
        assert_collector_api_enabled(settings)
    except CollectorDisabledError:
        raise HTTPException(status_code=404, detail="collector api not found") from None
    try:
        return authenticate_collector_token(authorization, settings)
    except CollectorAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None


def require_scope(scope: str) -> Callable[[CollectorToken], CollectorToken]:
    """FastAPI dependency factory: gate a route on one collector-token scope.

    The wrapped token already passed the enable gate + bearer auth. A token
    missing the required (effective) scope gets 403; revoked/absent tokens
    still 401 via `collector_token_auth`.
    """

    def dependency(token: CollectorToken = Depends(collector_token_auth)) -> CollectorToken:
        if scope not in effective_scopes(token.scopes):
            raise HTTPException(status_code=403, detail=f"scope '{scope}' required")
        return token

    return dependency
