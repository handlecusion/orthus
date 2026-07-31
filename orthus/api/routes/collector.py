"""P8.2 collector ingestion API + command queue routes.

Every endpoint 404s unless the node is company-kind with
`collector_api_enabled=True`. Document ingest and the queue poll/claim/complete
endpoints authenticate with an owner-scoped `dct_` collector token; queue
create/list endpoints use the browser session + operator gate. The two auth
worlds stay separate: a collector token never reaches a session endpoint and a
session cookie never reaches a token endpoint.

P8.7a: ingest requires the `ingest` scope; the command poll/claim/complete
endpoints require `commands`. `ingest` implies `commands` (see
`effective_scopes`) so pre-P8.7a daemon tokens (default `{ingest}`) still poll.
Session-operator create/list endpoints are unaffected by scopes.

P10.8: the token issue/list/revoke endpoints are member self-service
(`session_member`, no operator role) — the token is always self-bound
(`user_id=current.user_id`) and list/revoke stay strictly self-scoped, so
regular members can onboard the CLI/daemon without owner/admin help.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from orthus.audit import audit
from orthus.api.deps import get_current_user
from orthus.auth import AuthenticatedUser, allowlist_role, require_node_operator
from orthus.collector import (
    CollectorDisabledError,
    CollectorToken,
    assert_collector_api_enabled,
    claim_command,
    compile_personal_documents,
    complete_command,
    create_command,
    ingest_documents,
    list_commands,
    list_pending_commands,
    personal_compile_has_pending_work,
    require_scope,
)
from orthus.agentwork import resolve_agent_task_work_item_result
from orthus.agentwork import stream as agent_stream
from orthus.collector.auth import (
    ALLOWED_SCOPES,
    COLLECTOR_TOKEN_PREFIX,
    CollectorAuthError,
    authenticate_collector_token,
    effective_scopes,
    hash_collector_token,
)
from orthus.collector.ws_registry import WS_REGISTRY
from orthus.collector.commands import CollectorCommandError
from orthus.collector.ingest import ALLOWED_COLLECTOR_SOURCES, CollectorIngestError
from orthus.connectors import (
    ensure_default_connector_account,
    list_connector_manifests,
    register_default_connector_providers,
)
from orthus.connectors.account_config import (
    config_fields_for_slug,
    configure_connector_account,
)
from orthus.connectors.state import delete_connector_account, get_connector_account
from orthus.collector.rate_limit import RateLimitResult, compile_limiter, ingest_limiter
from orthus.collector.retention import (
    delete_personal_collector_document,
    prune_personal_collector_documents,
)
from orthus.db import session
from orthus.schemas.canonical import (
    CollectorCommand,
    CollectorCommandComplete,
    CollectorCommandCreate,
    CollectorCommandList,
    CollectorCommandStatus,
    CollectorWhoamiOut,
    CollectorCompileRequest,
    CollectorCompileResponse,
    CollectorConfigAccount,
    CollectorConfigResponse,
    CollectorEvidenceCompile,
    CollectorEvidenceDocument,
    CollectorEvidenceResponse,
    CollectorEvidenceSource,
    CollectorEvidenceWikiPage,
    CollectorLiveness,
    CollectorIngestRequest,
    CollectorIngestResponse,
    CollectorPersonalDataDeleteResponse,
    CollectorPersonalDataPruneRequest,
    CollectorStatusReport,
    CollectorTokenIssueRequest,
    CollectorTokenIssueResponse,
    CollectorTokenList,
    CollectorTokenRecord,
)
from orthus.api.routes.connectors import (
    ConnectorAccountOut,
    ConnectorConfigFieldOut,
    ConnectorDeleteOut,
    ConnectorManifestOut,
    ConnectorRunOut,
    _find_account,
    _list_accounts,
    _list_runs,
)
from orthus.settings import Settings, get_settings
from orthus.tables import (
    auth_identities,
    collector_commands,
    collector_tokens,
    connector_accounts,
    documents,
)
from orthus.wiki import store

# agent_task command vocabulary (spike). The daemon enforces the same sets;
# these gate central enqueue so an unknown mode/runner never reaches the queue.
AGENT_TASK_MODES = frozenset({"code", "knowledge"})
AGENT_TASK_RUNNERS = frozenset({"claude", "codex", "hermes"})

router = APIRouter(prefix="/collector", tags=["collector"])

_COLLECTOR_CONFIG_KEYS: dict[str, frozenset[str]] = {
    "local_files": frozenset({"roots", "extensions", "max_bytes"}),
    "codex_sessions": frozenset(
        {"roots", "max_bytes", "max_files", "max_messages", "max_message_chars"}
    ),
    "claude_sessions": frozenset(
        {"roots", "max_bytes", "max_files", "max_messages", "max_message_chars"}
    ),
    "gws_gmail": frozenset({"query", "max_messages", "max_body_chars"}),
    "gws_drive": frozenset({"query", "max_files", "max_bytes"}),
    "github": frozenset({"repos", "max_items"}),
}
_LIST_CONFIG_KEYS = frozenset({"roots", "extensions", "repos"})
_INT_CONFIG_KEYS = frozenset(
    {"max_bytes", "max_files", "max_messages", "max_message_chars", "max_body_chars", "max_items"}
)
_EVIDENCE_DOCS_PER_SOURCE = 5
_EVIDENCE_WIKI_LINKS_PER_DOC = 5
_EVIDENCE_COMMAND_LIMIT = 40
_TOKEN_BYTES = 32
_LIVENESS_LIVE_WINDOW = timedelta(minutes=30)
_LIVENESS_STALE_WINDOW = timedelta(hours=2)

# WebSocket close codes (slice 8). Application-defined 4xxx range so the daemon
# can distinguish "surface disabled" from "bad token" from "wrong scope".
_WS_CLOSE_DISABLED = 4404
_WS_CLOSE_UNAUTHORIZED = 4401
_WS_CLOSE_FORBIDDEN = 4403
_WS_PING_INTERVAL_SECONDS = 30.0


@router.websocket("/ws")
async def collector_ws_endpoint(websocket: WebSocket) -> None:
    """1-way notify channel (central->daemon): pushes "command_available".

    Fail-closed gate identical to the rest of the collector surface plus the
    slice-8 flag: company node + collector_api_enabled + collector_ws_enabled, or
    the handshake is closed before accept. Auth is the same collector token used
    by the HTTP endpoints (``Authorization: Bearer`` handshake header — kept out
    of the URL so the token never lands in access logs), validated in a
    threadpool because the lookup is sync DB; the token needs the ``commands``
    scope (which ``ingest`` implies). The daemon's claim/complete stay on HTTP;
    this socket only nudges it to poll-drain immediately. The ``type`` envelope
    field leaves room for future server-push message kinds (e.g. agent output
    streaming) without a new endpoint.
    """
    settings = get_settings()
    if not (
        settings.collector_api_enabled
        and settings.node_kind == "company"
        and settings.collector_ws_enabled
    ):
        await websocket.close(code=_WS_CLOSE_DISABLED)
        return

    authorization = websocket.headers.get("authorization")
    try:
        tok = await run_in_threadpool(authenticate_collector_token, authorization, settings)
    except CollectorAuthError:
        await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
        return
    if "commands" not in effective_scopes(tok.scopes):
        await websocket.close(code=_WS_CLOSE_FORBIDDEN)
        return

    await websocket.accept()
    queue = WS_REGISTRY.register(tok.node_id, tok.user_id, tok.device_id or "")
    try:
        await _run_ws_connection(websocket, queue)
    finally:
        WS_REGISTRY.unregister(tok.node_id, tok.user_id, queue, tok.device_id or "")


async def _run_ws_connection(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Run the writer (notify drain + heartbeat) and reader (disconnect watch).

    The reader exists only to observe client close / pongs so the connection
    tears down promptly; the writer forwards queued notifies and sends a periodic
    ping so an idle socket through a proxy stays alive. Either task finishing (a
    disconnect or a send failure) cancels the other.
    """
    writer = asyncio.create_task(_ws_writer(websocket, queue))
    reader = asyncio.create_task(_ws_reader(websocket))
    done, pending = await asyncio.wait({writer, reader}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except (asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
            pass
    # Surface a non-disconnect error from the finished task for visibility; a
    # normal client disconnect is swallowed.
    for task in done:
        exc = task.exception()
        if exc is not None and not isinstance(exc, WebSocketDisconnect):
            raise exc


async def _ws_writer(websocket: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        try:
            message = await asyncio.wait_for(queue.get(), timeout=_WS_PING_INTERVAL_SECONDS)
        except TimeoutError:
            await websocket.send_json({"type": "ping"})
            continue
        await websocket.send_json(message)


async def _ws_reader(websocket: WebSocket) -> None:
    # Drain inbound frames so a client close raises WebSocketDisconnect and ends the
    # connection. When streaming is enabled, inbound `agent_output`/`turn_complete`
    # frames (daemon -> central on this same socket) are fanned to the per-work SSE
    # subscribers via `agent_stream`. When off, frames are ignored (fail-closed) and
    # this stays the slice-8 drain-only reader. Routing is by work_item_id; the SSE
    # endpoint enforces that the browser caller owns that work item.
    streaming = get_settings().agent_task_streaming_enabled
    while True:
        raw = await websocket.receive_text()
        if not streaming:
            continue
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(frame, dict):
            continue
        if frame.get("type") not in ("agent_output", "turn_complete"):
            continue
        work_id = frame.get("work_item_id")
        if work_id:
            agent_stream.publish(str(work_id), frame)


def _require_enabled(settings: Settings) -> None:
    try:
        assert_collector_api_enabled(settings)
    except CollectorDisabledError:
        raise HTTPException(status_code=404, detail="collector api not found") from None


def _enforce_ingest_limit(owner_id: UUID, body: CollectorIngestRequest, settings: Settings) -> None:
    result = ingest_limiter.check(
        owner_id,
        count=len(body.documents),
        bytes_count=sum(len(doc.content_md.encode("utf-8")) for doc in body.documents),
        count_limit=settings.collector_ingest_owner_docs_limit,
        bytes_limit=settings.collector_ingest_owner_bytes_limit,
        window_seconds=settings.collector_ingest_owner_window_seconds,
        count_reason="docs",
    )
    if not result.allowed:
        _raise_rate_limited(owner_id, kind="ingest", result=result)


def _enforce_compile_limit(owner_id: UUID, settings: Settings, *, kind: str) -> None:
    result = compile_limiter.check(
        owner_id,
        count=1,
        count_limit=1,
        window_seconds=settings.collector_compile_owner_window_seconds,
        count_reason="runs",
    )
    if not result.allowed:
        _raise_rate_limited(owner_id, kind=kind, result=result)


def _raise_rate_limited(owner_id: UUID, *, kind: str, result: RateLimitResult) -> None:
    with audit("collector.rate_limit") as span:
        span.add_meta(
            user_id=str(owner_id),
            kind=kind,
            reason=result.reason,
            retry_after_seconds=result.retry_after_seconds,
        )
    raise HTTPException(
        status_code=429,
        detail=f"collector {kind} rate limited: {result.reason}",
        headers={"Retry-After": str(result.retry_after_seconds)},
    )


def session_operator(current: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    _require_enabled(get_settings())
    require_node_operator(current)
    return current


def session_member(current: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """Any authenticated session user (no operator role required).

    Used only by the collector-token self-service endpoints: the issued token
    is ALWAYS bound to `current.user_id` and list/revoke filter strictly on
    `user_id == current.user_id`, so the boundary is owner-scope by
    construction — a member can never see, mint, or revoke another user's
    token. Scope validation (`_normalize_token_scopes` allowlist) is unchanged.
    """
    _require_enabled(get_settings())
    return current


@router.post("/ingest/documents", response_model=CollectorIngestResponse)
def ingest_documents_endpoint(
    body: CollectorIngestRequest,
    token: CollectorToken = Depends(require_scope("ingest")),
) -> CollectorIngestResponse:
    _enforce_ingest_limit(token.user_id, body, get_settings())
    try:
        return ingest_documents(token, body.documents)
    except CollectorIngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/compile", response_model=CollectorCompileResponse)
def compile_documents_endpoint(
    body: CollectorCompileRequest | None = None,
    token: CollectorToken = Depends(require_scope("ingest")),
) -> CollectorCompileResponse:
    """P8.4 central compile for the token owner's personal documents.

    Compile is part of the ingest flow, so it requires the `ingest` scope (see
    P8.7a). Runs corpus index + wiki authoring for personal docs that the
    collector pushed but that are not yet indexed/authored. Idempotent: a second
    call after a clean compile returns zeros. The collector daemon calls this
    after pushing batches."""
    limit = body.limit if body is not None else None
    if personal_compile_has_pending_work(token.user_id, limit=limit):
        _enforce_compile_limit(token.user_id, get_settings(), kind="compile")
    result = compile_personal_documents(token.user_id, limit=limit)
    return CollectorCompileResponse(
        indexed=result.indexed,
        authored=result.authored,
        skipped=result.skipped,
        failed=result.failed,
    )


@router.post("/compile/retry", response_model=CollectorCompileResponse)
def retry_compile_documents_endpoint(
    body: CollectorCompileRequest | None = None,
    current: AuthenticatedUser = Depends(session_operator),
) -> CollectorCompileResponse:
    """Session-operator manual retry for the signed-in owner's personal documents."""

    limit = body.limit if body is not None else None
    if personal_compile_has_pending_work(current.user_id, limit=limit):
        _enforce_compile_limit(current.user_id, get_settings(), kind="compile_retry")
    result = compile_personal_documents(current.user_id, limit=limit)
    return CollectorCompileResponse(
        indexed=result.indexed,
        authored=result.authored,
        skipped=result.skipped,
        failed=result.failed,
    )


@router.get("/config", response_model=CollectorConfigResponse)
def collector_config_endpoint(
    token: CollectorToken = Depends(require_scope("commands")),
) -> CollectorConfigResponse:
    """Return non-secret owner personal connector settings for a collector daemon."""

    settings = get_settings()
    with session() as s:
        rows = s.execute(
            select(
                connector_accounts.c.connector_slug,
                connector_accounts.c.settings_redacted,
            )
            .where(
                and_(
                    connector_accounts.c.node_id == settings.node_id,
                    connector_accounts.c.account_kind == "personal",
                    connector_accounts.c.scope == "personal",
                    connector_accounts.c.owner_id == token.user_id,
                    # Each device sees only its own accounts (migration 0070).
                    connector_accounts.c.device_id == token.device_id,
                    connector_accounts.c.status == "active",
                    connector_accounts.c.connector_slug.in_(ALLOWED_COLLECTOR_SOURCES),
                )
            )
            .order_by(connector_accounts.c.connector_slug, connector_accounts.c.updated_at)
        ).all()

    return CollectorConfigResponse(
        owner_id=token.user_id,
        accounts=[
            CollectorConfigAccount(
                connector_slug=row.connector_slug,
                settings=_collector_config_settings(row.connector_slug, row.settings_redacted),
            )
            for row in rows
        ],
    )


# --- owner-scoped connector configuration (collector-token, `commands` scope) ---
#
# These four endpoints let the owner's `orthus` CLI manage personal connector
# accounts on the company node, mirroring the session `/connectors` PUT/POST/DELETE
# routes but authenticated by the owner-bound collector token instead of a browser
# session. They are gated by the same fail-closed enable flags as every other
# collector route (`require_scope` runs the enable gate first), use the `commands`
# scope (no new scope), and are strictly owner-bound to `token.user_id` so a token
# can never read or mutate another owner's rows.


class CollectorConnectorConfigBody(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    account_label: str | None = None


class CollectorConnectorsOut(BaseModel):
    node_kind: str
    node_id: str
    manifests: list[ConnectorManifestOut]
    accounts: list[ConnectorAccountOut]
    runs: list[ConnectorRunOut] = Field(default_factory=list)


def _owner_identity(user_id: UUID, settings: Settings) -> tuple[str | None, str | None]:
    """Resolve the token owner's verified email + allowlist role.

    `configure_connector_account` uses these only for the tiered mail-mailbox
    registration guard; for the personal file/AI/GWS/GitHub connectors a collector
    token manages they are inert. We still resolve them so the owner-bound write
    matches the session route's behavior exactly. A missing identity stays None
    (fail-closed for mail slugs)."""

    with session() as s:
        row = s.execute(
            select(auth_identities.c.email)
            .where(auth_identities.c.user_id == user_id)
            .order_by(auth_identities.c.created_at)
            .limit(1)
        ).first()
    email = str(row.email) if row is not None and row.email else None
    role = allowlist_role(email, settings) if email else None
    return email, role


@router.get("/whoami", response_model=CollectorWhoamiOut)
def collector_whoami(
    token: CollectorToken = Depends(require_scope("knowledge")),
) -> CollectorWhoamiOut:
    """Identity + node-local role for the calling knowledge/collector token.

    Lets a delegated/inline agent (and the `orthus whoami` CLI) orient on who it is
    acting as and whether that identity has owner/admin authority on this node.
    Read-only; resolves the owner email + allowlist role with the same helper the
    connector-config guard uses."""
    settings = get_settings()
    email, role = _owner_identity(token.user_id, settings)
    return CollectorWhoamiOut(
        user_id=token.user_id,
        email=email,
        role=role,
        node_id=token.node_id,
        scopes=sorted(effective_scopes(token.scopes)),
    )


@router.get("/connectors", response_model=CollectorConnectorsOut)
def collector_connectors_endpoint(
    token: CollectorToken = Depends(require_scope("commands")),
) -> CollectorConnectorsOut:
    """List personal connector manifests + the owner's accounts/runs/status."""

    settings = get_settings()
    register_default_connector_providers(replace=True)
    # Scope accounts to this device so each machine's CLI only manages its own
    # rows (migration 0070). Runs are keyed by account_id (already
    # device-distinct), so they need no device filter.
    accounts = _list_accounts(token.user_id, "personal", device_id=token.device_id)
    runs = _list_runs(token.user_id, "personal")
    accounts_by_slug = {account.connector_slug: account for account in accounts}
    manifests: list[ConnectorManifestOut] = []
    for manifest in list_connector_manifests():
        if not manifest.supports_account_kind("personal"):
            continue
        account = accounts_by_slug.get(manifest.slug)
        manifests.append(
            ConnectorManifestOut(
                slug=manifest.slug,
                label=manifest.label,
                source_kind=manifest.source_kind,
                account_kinds=list(manifest.account_kinds),
                auth_modes=list(manifest.auth_modes),
                capabilities=list(manifest.capabilities),
                default_interval_seconds=manifest.default_interval_seconds,
                default_daily_budget=manifest.default_daily_budget,
                privacy_class=manifest.privacy_class,
                redaction_profile=manifest.redaction_profile,
                import_mode=manifest.import_mode,
                description=manifest.description,
                settings_keys=list(manifest.settings_keys),
                # Local collection runs on the owner's machine, not central; the
                # CLI ensures/configs here but the daemon executes syncs.
                can_ensure_default=False,
                default_configured=account is not None and account.status == "active",
                config_error=None,
                config_fields=[
                    ConnectorConfigFieldOut(
                        key=field.key,
                        label=field.label,
                        kind=field.kind,
                        required=field.required,
                        placeholder=field.placeholder,
                        default_value=field.default_value,
                    )
                    for field in config_fields_for_slug(manifest.slug, settings)
                ],
            )
        )
    return CollectorConnectorsOut(
        node_kind=settings.node_kind,
        node_id=settings.node_id,
        manifests=manifests,
        accounts=accounts,
        runs=runs,
    )


@router.put("/connectors/{connector_slug}/config", response_model=ConnectorAccountOut)
def collector_configure_connector_endpoint(
    connector_slug: str,
    body: CollectorConnectorConfigBody,
    token: CollectorToken = Depends(require_scope("commands")),
) -> ConnectorAccountOut:
    """Owner-bound personal connector config write (mirrors session PUT config)."""

    settings = get_settings()
    register_default_connector_providers(replace=True)
    owner_email, actor_role = _owner_identity(token.user_id, settings)
    try:
        with audit("connector.command") as span:
            span.add_meta(
                action="config",
                connector_slug=connector_slug,
                node_id=settings.node_id,
                scope="personal",
                user_id=str(token.user_id),
                device_id=token.device_id,
                setting_keys=sorted(body.settings),
                secret_keys=sorted(body.secrets),
            )
            account_id = configure_connector_account(
                connector_slug,
                token.user_id,
                input_settings=body.settings,
                input_secrets=body.secrets,
                account_label=body.account_label,
                settings=settings,
                account_kind="personal",
                owner_email=owner_email,
                actor_role=actor_role,
                device_id=token.device_id,
            )
            span.set_output({"account_id": str(account_id)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    account = _find_account(token.user_id, account_id, "personal")
    if account is None:
        raise HTTPException(status_code=404, detail="connector account not found")
    return account


@router.post("/connectors/{connector_slug}/ensure", response_model=ConnectorAccountOut)
def collector_ensure_connector_endpoint(
    connector_slug: str,
    token: CollectorToken = Depends(require_scope("commands")),
) -> ConnectorAccountOut:
    """Owner-bound personal connector ensure (mirrors session POST ensure)."""

    settings = get_settings()
    register_default_connector_providers(replace=True)
    try:
        with audit("connector.command") as span:
            span.add_meta(
                action="ensure",
                connector_slug=connector_slug,
                node_id=settings.node_id,
                scope="personal",
                user_id=str(token.user_id),
            )
            account_id = ensure_default_connector_account(connector_slug, token.user_id, settings)
            span.set_output({"account_id": str(account_id)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    account = _find_account(token.user_id, account_id, "personal")
    if account is None:
        raise HTTPException(status_code=404, detail="connector account not found")
    return account


@router.delete(
    "/connectors/{connector_slug}/accounts/{account_id}",
    response_model=ConnectorDeleteOut,
)
def collector_delete_connector_endpoint(
    connector_slug: str,
    account_id: UUID,
    token: CollectorToken = Depends(require_scope("commands")),
) -> ConnectorDeleteOut:
    """Owner-bound personal connector account delete.

    The row must be a personal account on this node whose `owner_id` is the token
    owner and whose slug matches; anything else returns 404 so the delete never
    leaks another owner's (or a company) row's existence."""

    settings = get_settings()
    row = get_connector_account(account_id)
    mapping = row._mapping if row is not None else None
    if (
        mapping is None
        or mapping["node_id"] != settings.node_id
        or mapping["connector_slug"] != connector_slug.strip().lower()
        or mapping["account_kind"] != "personal"
        or mapping["owner_id"] != token.user_id
        # Machine A cannot see/delete machine B's account row (migration 0070).
        or (mapping["device_id"] or "") != token.device_id
    ):
        raise HTTPException(status_code=404, detail="connector account not found")
    with audit("connector.command") as span:
        span.add_meta(
            action="delete",
            connector_slug=connector_slug,
            node_id=settings.node_id,
            scope="personal",
            user_id=str(token.user_id),
            account_id=str(account_id),
        )
        deleted = delete_connector_account(account_id)
        span.set_output({"account_id": str(account_id), "deleted": deleted})
    if not deleted:
        raise HTTPException(status_code=404, detail="connector account not found")
    return ConnectorDeleteOut(account_id=account_id, deleted=True)


@router.post("/commands", response_model=CollectorCommand)
def create_command_endpoint(
    body: CollectorCommandCreate,
    current: AuthenticatedUser = Depends(session_operator),
) -> CollectorCommand:
    _validate_command_payload(body)
    return create_command(
        get_settings(),
        user_id=current.user_id,
        created_by=current.user_id,
        kind=body.kind,
        payload=body.payload,
        device_id=body.device_id,
    )


@router.get("/commands", response_model=CollectorCommandList)
def list_commands_endpoint(
    status: CollectorCommandStatus | None = Query(default=None),
    current: AuthenticatedUser = Depends(session_operator),
) -> CollectorCommandList:
    return list_commands(current.user_id, status=status)


@router.get("/tokens", response_model=CollectorTokenList)
def list_tokens_endpoint(
    current: AuthenticatedUser = Depends(session_member),
) -> CollectorTokenList:
    settings = get_settings()
    with session() as s:
        rows = s.execute(
            select(
                collector_tokens.c.token_id,
                collector_tokens.c.node_id,
                collector_tokens.c.name,
                collector_tokens.c.scopes,
                collector_tokens.c.device_id,
                collector_tokens.c.created_at,
                collector_tokens.c.last_used_at,
                collector_tokens.c.last_polled_at,
                collector_tokens.c.last_status_at,
                collector_tokens.c.scheduler_installed,
                collector_tokens.c.scheduler_loaded,
                collector_tokens.c.scheduler_interval_seconds,
                collector_tokens.c.last_status_error,
                collector_tokens.c.revoked_at,
            )
            .where(
                collector_tokens.c.user_id == current.user_id,
                collector_tokens.c.node_id == settings.node_id,
            )
            .order_by(
                collector_tokens.c.revoked_at.is_not(None),
                collector_tokens.c.created_at.desc(),
            )
        ).all()
    items = [_token_record(row) for row in rows]
    return CollectorTokenList(count=len(items), items=items)


@router.post("/tokens", response_model=CollectorTokenIssueResponse)
def issue_token_endpoint(
    body: CollectorTokenIssueRequest,
    current: AuthenticatedUser = Depends(session_member),
) -> CollectorTokenIssueResponse:
    settings = get_settings()
    scopes = _normalize_token_scopes(body.scopes)
    plaintext = f"{COLLECTOR_TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_BYTES)}"
    token_id = uuid.uuid4()
    now = datetime.now(UTC)
    name = body.name.strip()
    token_hash = hash_collector_token(plaintext)
    # Each machine gets its own device_id so a user can run a daemon on the
    # Mac mini and the MacBook at once without command-queue/connector-account
    # collision (migration 0070). The partial unique index is the hard race
    # guard; on collision we regenerate with a random suffix and retry.
    last_exc: IntegrityError | None = None
    device_id = ""
    for attempt in range(3):
        device_id = _generate_device_id(
            current.user_id,
            settings.node_id,
            body.device_label,
            random_suffix=attempt > 0,
        )
        try:
            with session() as s:
                s.execute(
                    insert(collector_tokens).values(
                        token_id=token_id,
                        user_id=current.user_id,
                        node_id=settings.node_id,
                        name=name,
                        token_hash=token_hash,
                        scopes=scopes,
                        device_id=device_id,
                        created_at=now,
                    )
                )
                s.commit()
            break
        except IntegrityError as exc:
            last_exc = exc
    else:
        raise HTTPException(
            status_code=409, detail="could not allocate a unique device id"
        ) from last_exc
    return CollectorTokenIssueResponse(
        token=plaintext,
        item=CollectorTokenRecord(
            token_id=token_id,
            node_id=settings.node_id,
            name=name,
            scopes=scopes,
            device_id=device_id,
            created_at=now,
            last_used_at=None,
            last_polled_at=None,
            last_status_at=None,
            scheduler_installed=None,
            scheduler_loaded=None,
            scheduler_interval_seconds=None,
            last_status_error=None,
            revoked_at=None,
        ),
    )


_DEVICE_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(label: str | None) -> str:
    """ASCII device slug: lowercase, [a-z0-9-], collapsed, stripped."""
    return _DEVICE_SLUG_RE.sub("-", (label or "").strip().lower()).strip("-")


def _generate_device_id(
    user_id: UUID,
    node_id: str,
    label: str | None,
    *,
    random_suffix: bool = False,
) -> str:
    """Pick a device_id unique among the owner's active (user, node) tokens.

    Derives a slug from the human label (default "device") and disambiguates
    with -2/-3/... against existing active device_ids. The partial unique
    index is the real guard; ``random_suffix`` is used on an IntegrityError
    retry to break a concurrent race."""
    base = _slug(label) or "device"
    with session() as s:
        rows = s.execute(
            select(collector_tokens.c.device_id).where(
                collector_tokens.c.user_id == user_id,
                collector_tokens.c.node_id == node_id,
                collector_tokens.c.revoked_at.is_(None),
            )
        ).all()
    taken = {row.device_id for row in rows if row.device_id}
    if random_suffix:
        return f"{base}-{secrets.token_hex(3)}"
    candidate = base
    i = 2
    while candidate in taken:
        candidate = f"{base}-{i}"
        i += 1
    return candidate


@router.post("/tokens/{token_id}/revoke", response_model=CollectorTokenRecord)
def revoke_token_endpoint(
    token_id: UUID,
    current: AuthenticatedUser = Depends(session_member),
) -> CollectorTokenRecord:
    settings = get_settings()
    now = datetime.now(UTC)
    with session() as s:
        row = s.execute(
            select(
                collector_tokens.c.token_id,
                collector_tokens.c.node_id,
                collector_tokens.c.name,
                collector_tokens.c.scopes,
                collector_tokens.c.device_id,
                collector_tokens.c.created_at,
                collector_tokens.c.last_used_at,
                collector_tokens.c.last_polled_at,
                collector_tokens.c.last_status_at,
                collector_tokens.c.scheduler_installed,
                collector_tokens.c.scheduler_loaded,
                collector_tokens.c.scheduler_interval_seconds,
                collector_tokens.c.last_status_error,
                collector_tokens.c.revoked_at,
            ).where(
                collector_tokens.c.token_id == token_id,
                collector_tokens.c.user_id == current.user_id,
                collector_tokens.c.node_id == settings.node_id,
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="collector token not found")
        record = _token_record(row)
        if row.revoked_at is None:
            s.execute(
                update(collector_tokens)
                .where(collector_tokens.c.token_id == token_id)
                .values(revoked_at=now)
            )
            s.commit()
            record.revoked_at = now
    return record


@router.get("/evidence", response_model=CollectorEvidenceResponse)
def collector_evidence_endpoint(
    current: AuthenticatedUser = Depends(session_operator),
) -> CollectorEvidenceResponse:
    """Return owner-local collector push + compile evidence for the signed-in operator."""

    return _collector_evidence(current.user_id)


@router.delete("/documents/{doc_id}", response_model=CollectorPersonalDataDeleteResponse)
def delete_personal_document_endpoint(
    doc_id: UUID,
    current: AuthenticatedUser = Depends(session_operator),
) -> CollectorPersonalDataDeleteResponse:
    """Delete one signed-in owner's central personal collector document + derivatives."""

    return _delete_result(delete_personal_collector_document(current.user_id, doc_id))


@router.post("/personal-data/prune", response_model=CollectorPersonalDataDeleteResponse)
def prune_personal_data_endpoint(
    body: CollectorPersonalDataPruneRequest,
    current: AuthenticatedUser = Depends(session_operator),
) -> CollectorPersonalDataDeleteResponse:
    """Delete old signed-in owner personal collector documents + derivatives."""

    source = body.source.strip() if body.source else None
    if source is not None and source not in ALLOWED_COLLECTOR_SOURCES:
        raise HTTPException(status_code=422, detail=f"unsupported source: {source}")
    cutoff = datetime.now(UTC) - timedelta(days=body.older_than_days)
    return _delete_result(
        prune_personal_collector_documents(current.user_id, cutoff=cutoff, source=source)
    )


@router.get("/commands/pending", response_model=CollectorCommandList)
def pending_commands_endpoint(
    token: CollectorToken = Depends(require_scope("commands")),
) -> CollectorCommandList:
    _stamp_collector_poll(token.token_id)
    return list_pending_commands(token.user_id, device_id=(token.device_id or None))


@router.post("/status", response_model=CollectorLiveness)
def collector_status_endpoint(
    body: CollectorStatusReport | None = None,
    token: CollectorToken = Depends(require_scope("commands")),
) -> CollectorLiveness:
    _record_collector_status(token.token_id, body or CollectorStatusReport())
    return _collector_evidence(token.user_id).liveness


@router.post("/commands/{command_id}/claim", response_model=CollectorCommand)
def claim_command_endpoint(
    command_id: UUID,
    token: CollectorToken = Depends(require_scope("commands")),
) -> CollectorCommand:
    try:
        return claim_command(token.user_id, command_id)
    except CollectorCommandError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _validate_command_payload(body: CollectorCommandCreate) -> None:
    if body.kind == "connector_sync":
        source = str(body.payload.get("connector") or "").strip()
        if source not in ALLOWED_COLLECTOR_SOURCES:
            raise HTTPException(status_code=422, detail=f"unsupported connector: {source}")
    elif body.kind == "raw_repush":
        source = str(body.payload.get("connector") or body.payload.get("source") or "").strip()
        if source not in ALLOWED_COLLECTOR_SOURCES:
            raise HTTPException(status_code=422, detail=f"unsupported source: {source}")
    elif body.kind == "agent_task":
        _validate_agent_task_payload(body.payload)


def _validate_agent_task_payload(payload: dict) -> None:
    # Fail-closed: refuse to enqueue unless an operator has explicitly enabled
    # the agent_task command on the central API process.
    if not get_settings().agent_task_enabled:
        raise HTTPException(status_code=422, detail="agent_task command disabled")
    mode = str(payload.get("mode") or "").strip()
    if mode not in AGENT_TASK_MODES:
        raise HTTPException(status_code=422, detail=f"unsupported agent_task mode: {mode}")
    runner = str(payload.get("runner") or "").strip()
    if runner not in AGENT_TASK_RUNNERS:
        raise HTTPException(status_code=422, detail=f"unsupported agent_task runner: {runner}")
    instruction = payload.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise HTTPException(status_code=422, detail="agent_task instruction required")
    # cwd is optional at enqueue: central does not know a team member's local repo
    # path, so the daemon resolves cwd from payload.cwd or ORTHUS_AGENT_TASK_WORKSPACE
    # and fails the run itself if code mode has neither.
    cwd = payload.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        raise HTTPException(status_code=422, detail="agent_task cwd must be a non-empty string")
    timeout_s = payload.get("timeout_s")
    if timeout_s is not None and not isinstance(timeout_s, int):
        raise HTTPException(status_code=422, detail="agent_task timeout_s must be an integer")
    for key in ("thread_id", "chat_session_id", "runner_session_id"):
        value = payload.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise HTTPException(status_code=422, detail=f"agent_task {key} must be a string")
    resume = payload.get("runner_session_resume")
    if resume is not None and not isinstance(resume, bool):
        raise HTTPException(
            status_code=422, detail="agent_task runner_session_resume must be a boolean"
        )


def _normalize_token_scopes(raw_scopes: list[str]) -> list[str]:
    scopes = [str(scope).strip() for scope in raw_scopes if str(scope).strip()]
    if not scopes:
        raise HTTPException(status_code=422, detail="at least one scope required")
    unknown = [scope for scope in scopes if scope not in ALLOWED_SCOPES]
    if unknown:
        allowed = ",".join(sorted(ALLOWED_SCOPES))
        raise HTTPException(
            status_code=422,
            detail=f"unknown scope(s): {','.join(unknown)}; allowed: {allowed}",
        )
    return list(dict.fromkeys(scopes))


def _stamp_collector_poll(token_id: UUID) -> None:
    with session() as s:
        s.execute(
            update(collector_tokens)
            .where(collector_tokens.c.token_id == token_id)
            .values(last_polled_at=datetime.now(UTC))
        )
        s.commit()


def _record_collector_status(token_id: UUID, body: CollectorStatusReport) -> None:
    values: dict[str, Any] = {"last_status_at": datetime.now(UTC)}
    if body.scheduler_installed is not None:
        values["scheduler_installed"] = body.scheduler_installed
    if body.scheduler_loaded is not None:
        values["scheduler_loaded"] = body.scheduler_loaded
    if body.scheduler_interval_seconds is not None:
        values["scheduler_interval_seconds"] = body.scheduler_interval_seconds
    elif body.scheduler_installed is False:
        values["scheduler_interval_seconds"] = None
    values["last_status_error"] = body.status_error.strip() if body.status_error else None
    # migration 0073: persist the daemon-reported agent_task run folders so the
    # owner's delegation UI can offer their own recent cwds.
    if body.workspaces is not None:
        values["agent_workspaces"] = body.workspaces.model_dump()

    with session() as s:
        s.execute(
            update(collector_tokens).where(collector_tokens.c.token_id == token_id).values(**values)
        )
        s.commit()


def _token_record(row: Any) -> CollectorTokenRecord:
    return CollectorTokenRecord(
        token_id=row.token_id,
        node_id=row.node_id,
        name=row.name or "",
        scopes=list(row.scopes or []),
        device_id=getattr(row, "device_id", "") or "",
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        last_polled_at=row.last_polled_at,
        last_status_at=row.last_status_at,
        scheduler_installed=row.scheduler_installed,
        scheduler_loaded=row.scheduler_loaded,
        scheduler_interval_seconds=row.scheduler_interval_seconds,
        last_status_error=row.last_status_error,
        revoked_at=row.revoked_at,
    )


def _collector_config_settings(slug: str, raw_settings: Any) -> dict[str, str | int | list[str]]:
    if not isinstance(raw_settings, dict):
        return {}
    allowed = _COLLECTOR_CONFIG_KEYS.get(slug, frozenset())
    out: dict[str, str | int | list[str]] = {}
    for key in sorted(allowed):
        if key not in raw_settings:
            continue
        value = _normalize_config_value(key, raw_settings[key])
        if value not in (None, "", []):
            out[key] = value
    return out


def _normalize_config_value(key: str, value: Any) -> str | int | list[str] | None:
    if key in _LIST_CONFIG_KEYS:
        if isinstance(value, list):
            items = [str(item).strip() for item in value]
        else:
            items = [item.strip() for item in str(value).split(",")]
        return [item for item in items if item]
    if key in _INT_CONFIG_KEYS:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
    text = str(value).strip()
    return text or None


def _collector_evidence(user_id: UUID) -> CollectorEvidenceResponse:
    settings = get_settings()
    now = datetime.now(UTC)
    allowed_sources = sorted(ALLOWED_COLLECTOR_SOURCES)
    doc_conditions = [
        documents.c.scope == "personal",
        documents.c.user_id == user_id,
        documents.c.source.in_(allowed_sources),
    ]
    with session() as s:
        count_rows = s.execute(
            select(documents.c.source, func.count().label("document_count"))
            .where(and_(*doc_conditions))
            .group_by(documents.c.source)
        ).all()
        token_rows = s.execute(
            select(
                collector_tokens.c.revoked_at,
                collector_tokens.c.last_used_at,
                collector_tokens.c.last_polled_at,
                collector_tokens.c.last_status_at,
                collector_tokens.c.scheduler_installed,
                collector_tokens.c.scheduler_loaded,
                collector_tokens.c.scheduler_interval_seconds,
                collector_tokens.c.last_status_error,
            ).where(
                collector_tokens.c.user_id == user_id,
                collector_tokens.c.node_id == settings.node_id,
            )
        ).all()
        command_rows = s.execute(
            select(collector_commands)
            .where(
                collector_commands.c.node_id == settings.node_id,
                collector_commands.c.user_id == user_id,
            )
            .order_by(collector_commands.c.created_at.desc())
            .limit(_EVIDENCE_COMMAND_LIMIT)
        ).all()
        commands = [_collector_command_out(row) for row in command_rows]
        command_status_rows = s.execute(
            select(
                collector_commands.c.status,
                func.count().label("command_count"),
                func.min(collector_commands.c.created_at).label("oldest_created_at"),
                func.min(collector_commands.c.claimed_at).label("oldest_claimed_at"),
            )
            .where(
                collector_commands.c.node_id == settings.node_id,
                collector_commands.c.user_id == user_id,
                collector_commands.c.status.in_(["pending", "claimed"]),
            )
            .group_by(collector_commands.c.status)
        ).all()
        liveness = _collector_liveness(now, token_rows, command_status_rows)
        latest_command_by_source: dict[str, CollectorCommand] = {}
        latest_compile_by_source: dict[str, CollectorEvidenceCompile] = {}
        for command in commands:
            source = _collector_command_source(command)
            if source is None:
                continue
            latest_command_by_source.setdefault(source, command)
            compile_counts = _collector_compile_from_result(command.result)
            if compile_counts is not None:
                latest_compile_by_source.setdefault(source, compile_counts)

        counts = {row.source: int(row.document_count) for row in count_rows}
        source_keys = sorted(set(counts) | set(latest_command_by_source))
        doc_rows_by_source: dict[str, list[Any]] = {}
        recent_doc_ids: set[UUID] = set()
        for source in source_keys:
            rows = s.execute(
                select(
                    documents.c.doc_id,
                    documents.c.title,
                    documents.c.source,
                    documents.c.source_account_id,
                    documents.c.source_external_id,
                    documents.c.source_last_edited_at,
                    documents.c.updated_at,
                )
                .where(and_(*doc_conditions, documents.c.source == source))
                .order_by(documents.c.updated_at.desc())
                .limit(_EVIDENCE_DOCS_PER_SOURCE)
            ).all()
            row_mappings = [row._mapping for row in rows]
            doc_rows_by_source[source] = row_mappings
            recent_doc_ids.update(row["doc_id"] for row in row_mappings)

        wiki_links_by_doc = _collector_wiki_links_by_doc(user_id, recent_doc_ids)
        docs_by_source: dict[str, list[CollectorEvidenceDocument]] = {
            source: [_collector_evidence_doc_out(row, wiki_links_by_doc) for row in rows]
            for source, rows in doc_rows_by_source.items()
        }

    return CollectorEvidenceResponse(
        owner_id=user_id,
        liveness=liveness,
        sources=[
            CollectorEvidenceSource(
                source=source,
                document_count=counts.get(source, 0),
                recent_documents=docs_by_source.get(source, []),
                latest_command=latest_command_by_source.get(source),
                latest_compile=latest_compile_by_source.get(source),
            )
            for source in source_keys
        ],
        recent_commands=commands,
    )


def _collector_command_out(row) -> CollectorCommand:
    return CollectorCommand(
        command_id=row.command_id,
        node_id=row.node_id,
        user_id=row.user_id,
        kind=row.kind,
        payload=row.payload or {},
        status=row.status,
        result=row.result,
        created_at=row.created_at,
        claimed_at=row.claimed_at,
        completed_at=row.completed_at,
    )


def _collector_liveness(
    now: datetime,
    token_rows: list[Any],
    command_status_rows: list[Any],
) -> CollectorLiveness:
    active_tokens = [row for row in token_rows if row.revoked_at is None]
    revoked_count = len(token_rows) - len(active_tokens)
    last_polled_at = max(
        (row.last_polled_at for row in active_tokens if row.last_polled_at is not None),
        default=None,
    )
    last_status_at = max(
        (row.last_status_at for row in active_tokens if row.last_status_at is not None),
        default=None,
    )
    last_seen_at = max(
        (
            value
            for row in active_tokens
            for value in (row.last_polled_at, row.last_status_at, row.last_used_at)
            if value is not None
        ),
        default=None,
    )
    scheduler_row = max(
        (row for row in active_tokens if row.last_status_at is not None),
        key=lambda row: row.last_status_at,
        default=None,
    )
    command_status = {row.status: row for row in command_status_rows}
    pending_row = command_status.get("pending")
    claimed_row = command_status.get("claimed")
    pending_count = int(pending_row.command_count) if pending_row else 0
    claimed_count = int(claimed_row.command_count) if claimed_row else 0
    oldest_pending_at = pending_row.oldest_created_at if pending_row else None
    oldest_claimed_at = (
        (claimed_row.oldest_claimed_at or claimed_row.oldest_created_at) if claimed_row else None
    )

    if not active_tokens:
        status = "unconfigured"
        reason = "active collector token 없음"
    elif last_polled_at is None:
        status = "offline"
        reason = "collector command poll 기록 없음"
    else:
        age = now - _as_aware_utc(last_polled_at)
        if age <= _LIVENESS_LIVE_WINDOW:
            status = "live"
            reason = "최근 30분 안에 collector command poll"
        elif age <= _LIVENESS_STALE_WINDOW:
            status = "stale"
            reason = "collector command poll이 30분 넘게 없음"
        else:
            status = "offline"
            reason = "collector command poll이 2시간 넘게 없음"

    if pending_count and status in {"offline", "stale"}:
        reason = f"{reason}; pending command가 collector 실행을 기다림"

    return CollectorLiveness(
        status=status,
        checked_at=now,
        last_seen_at=last_seen_at,
        last_polled_at=last_polled_at,
        last_status_at=last_status_at,
        active_token_count=len(active_tokens),
        revoked_token_count=revoked_count,
        pending_command_count=pending_count,
        claimed_command_count=claimed_count,
        oldest_pending_command_at=oldest_pending_at,
        oldest_claimed_command_at=oldest_claimed_at,
        scheduler_installed=scheduler_row.scheduler_installed if scheduler_row else None,
        scheduler_loaded=scheduler_row.scheduler_loaded if scheduler_row else None,
        scheduler_interval_seconds=scheduler_row.scheduler_interval_seconds
        if scheduler_row
        else None,
        last_status_error=scheduler_row.last_status_error if scheduler_row else None,
        reason=reason,
    )


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _delete_result(result) -> CollectorPersonalDataDeleteResponse:
    return CollectorPersonalDataDeleteResponse(
        owner_id=result.owner_id,
        documents_deleted=result.documents_deleted,
        connector_items_deleted=result.connector_items_deleted,
        structured_rows_deleted=result.structured_rows_deleted,
        corpus_chunks_deleted=result.corpus_chunks_deleted,
        corpus_embeddings_deleted=result.corpus_embeddings_deleted,
        wiki_items_deleted=result.wiki_items_deleted,
    )


def _collector_wiki_links_by_doc(
    user_id: UUID,
    doc_ids: set[UUID],
) -> dict[UUID, tuple[str, list[CollectorEvidenceWikiPage]]]:
    if not doc_ids:
        return {}
    wanted = {str(doc_id): doc_id for doc_id in doc_ids}
    out: dict[UUID, tuple[str, list[CollectorEvidenceWikiPage]]] = {}
    for source_slug in store.list_slugs("source", scope="personal", owner_id=user_id):
        source = store.load_source(source_slug, scope="personal", owner_id=user_id)
        if source is None:
            continue
        doc_id = wanted.get(source.source_ref)
        if doc_id is None:
            continue
        out[doc_id] = (source.slug, _collector_wiki_pages_for_source(user_id, source))
    return out


def _collector_wiki_pages_for_source(user_id: UUID, source) -> list[CollectorEvidenceWikiPage]:
    slugs: list[str] = []
    seen: set[str] = set()

    def add(slug: str) -> None:
        value = str(slug).strip()
        if value and value not in seen:
            seen.add(value)
            slugs.append(value)

    for slug in source.related_pages:
        add(slug)
    for claim_slug in source.key_claims:
        claim = store.load_claim(claim_slug, scope="personal", owner_id=user_id)
        if claim is None:
            continue
        for slug in claim.related_pages:
            add(slug)

    pages: list[CollectorEvidenceWikiPage] = []
    for slug in slugs:
        page = store.load_page(slug, scope="personal", owner_id=user_id)
        if page is None:
            continue
        pages.append(CollectorEvidenceWikiPage(slug=page.slug, title=page.title))
        if len(pages) >= _EVIDENCE_WIKI_LINKS_PER_DOC:
            break
    return pages


def _collector_evidence_doc_out(
    row,
    wiki_links_by_doc: dict[UUID, tuple[str, list[CollectorEvidenceWikiPage]]],
) -> CollectorEvidenceDocument:
    wiki_source_slug = None
    wiki_pages: list[CollectorEvidenceWikiPage] = []
    links = wiki_links_by_doc.get(row["doc_id"])
    if links is not None:
        wiki_source_slug, wiki_pages = links
    return CollectorEvidenceDocument(
        doc_id=row["doc_id"],
        title=row["title"],
        source=row["source"],
        source_account_id=row["source_account_id"],
        source_external_id=row["source_external_id"],
        source_last_edited_at=row["source_last_edited_at"],
        updated_at=row["updated_at"],
        wiki_source_slug=wiki_source_slug,
        wiki_pages=wiki_pages,
    )


def _collector_command_source(command: CollectorCommand) -> str | None:
    source = _source_from_mapping(command.payload)
    if source is None and isinstance(command.result, dict):
        source = _source_from_mapping(command.result)
    return source


def _source_from_mapping(value: dict[str, Any]) -> str | None:
    raw = value.get("connector") or value.get("source")
    source = str(raw).strip() if raw is not None else ""
    return source if source in ALLOWED_COLLECTOR_SOURCES else None


def _collector_compile_from_result(result: dict | None) -> CollectorEvidenceCompile | None:
    if not isinstance(result, dict):
        return None
    keys = ("compile_indexed", "compile_authored", "compile_skipped", "compile_failed")
    if not any(key in result for key in keys):
        return None
    return CollectorEvidenceCompile(
        indexed=_result_int(result, "compile_indexed"),
        authored=_result_int(result, "compile_authored"),
        skipped=_result_int(result, "compile_skipped"),
        failed=_result_int(result, "compile_failed"),
    )


def _result_int(result: dict, key: str) -> int:
    try:
        return int(result.get(key) or 0)
    except (TypeError, ValueError):
        return 0


@router.post("/commands/{command_id}/complete", response_model=CollectorCommand)
def complete_command_endpoint(
    command_id: UUID,
    body: CollectorCommandComplete,
    token: CollectorToken = Depends(require_scope("commands")),
) -> CollectorCommand:
    try:
        command = complete_command(
            token.user_id,
            command_id,
            status=body.status,
            result_payload=body.result,
        )
    except CollectorCommandError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    # An agent_task command carries its originating AgentWork id; reflect the
    # daemon result back onto that work item. A missing work item is skipped
    # silently so the command completion still succeeds.
    if command.kind == "agent_task" and (command.payload or {}).get("work_item_id"):
        resolve_agent_task_work_item_result(
            command.payload,
            command_status=command.status,
            result=command.result,
        )
    return command
