"""Connector registry, account, and manual sync API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.api.deps import get_current_user, get_user_id
from orthus.auth import AuthenticatedUser, require_node_operator
from orthus.audit.logger import audit
from orthus.connectors import (
    NotionConnector,
    adopt_legacy_notion_documents,
    can_ensure_default_account,
    default_account_config_status,
    ensure_default_connector_account,
    ensure_notion_account,
    list_connector_manifests,
    register_default_connector_providers,
    run_connector_account_sync,
    run_import,
)
from orthus.connectors.account_config import (
    config_fields_for_slug,
    configure_connector_account,
)
from orthus.connectors.state import (
    AccountKind,
    delete_connector_account,
    get_connector_account,
    start_connector_run,
)
from orthus.secrets import SecretStoreError
from orthus.settings import get_settings
from orthus.db import session
from orthus.tables import (
    connector_accounts,
    connector_runs,
    connector_sync_state,
    documents,
    users,
)

router = APIRouter(prefix="/connectors", tags=["connectors"])
ConnectorScope = Literal["company", "personal"]


class NotionImportIn(BaseModel):
    since: datetime | None = None  # incremental cutoff; None = full seed import


class ImportOut(BaseModel):
    created: int
    updated: int
    skipped: int
    doc_ids: list[UUID]


class ConnectorManifestOut(BaseModel):
    slug: str
    label: str
    source_kind: str
    account_kinds: list[str]
    auth_modes: list[str]
    capabilities: list[str]
    default_interval_seconds: int | None
    default_daily_budget: int | None
    privacy_class: str
    redaction_profile: str
    import_mode: str
    description: str
    settings_keys: list[str]
    can_ensure_default: bool
    default_configured: bool
    config_error: str | None = None
    config_fields: list["ConnectorConfigFieldOut"] = Field(default_factory=list)


class ConnectorConfigFieldOut(BaseModel):
    key: str
    label: str
    kind: str
    required: bool
    placeholder: str
    default_value: str | int | list[str] | None = None


class ConnectorAccountOut(BaseModel):
    account_id: UUID
    connector_slug: str
    account_kind: str
    node_id: str
    scope: str
    owner_id: UUID | None
    project: str | None
    auth_mode: str
    account_label: str | None
    status: str
    settings_redacted: dict
    device_id: str = ""
    created_at: datetime | None
    updated_at: datetime | None
    last_sync_at: datetime | None = None
    last_error: str | None = None


class ConnectorRunOut(BaseModel):
    run_id: UUID
    account_id: UUID
    connector_slug: str
    reason: str
    status: str
    fetched: int
    created: int
    updated: int
    skipped: int
    errors: int
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None


class ConnectorDocumentOut(BaseModel):
    doc_id: UUID
    account_id: UUID | None
    connector_slug: str
    title: str
    source: str
    source_external_id: str | None
    source_last_edited_at: datetime | None
    updated_at: datetime | None


class ConnectorsOut(BaseModel):
    node_kind: str
    node_id: str
    manifests: list[ConnectorManifestOut]
    accounts: list[ConnectorAccountOut]
    runs: list[ConnectorRunOut] = Field(default_factory=list)
    documents: list[ConnectorDocumentOut] = Field(default_factory=list)


class ConnectorSyncIn(BaseModel):
    concurrency: int = 8
    background: bool = False


class ConnectorConfigureIn(BaseModel):
    account_label: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    token: str | None = None


class ConnectorReportOut(BaseModel):
    created: int
    updated: int
    skipped: int
    errors: int
    total: int
    doc_ids: list[UUID]


class ConnectorSyncOut(BaseModel):
    account_id: UUID
    connector_slug: str
    run_id: UUID
    status: str
    report: ConnectorReportOut | None = None
    error_message: str | None = None


class ConnectorDeleteOut(BaseModel):
    account_id: UUID
    deleted: bool


@router.get("", response_model=ConnectorsOut)
def list_connectors(
    scope: ConnectorScope | None = Query(default=None),
    user_id: UUID = Depends(get_user_id),
) -> ConnectorsOut:
    settings = get_settings()
    account_kind = _resolve_account_kind(scope, settings)
    register_default_connector_providers(replace=True)
    accounts = _list_accounts(user_id, account_kind)
    runs = _list_runs(user_id, account_kind)
    docs = _list_recent_documents(user_id, account_kind)
    accounts_by_slug = {account.connector_slug: account for account in accounts}
    manifests = []
    for m in list_connector_manifests():
        if not m.supports_account_kind(account_kind):
            continue
        default_configured, config_error = _default_account_status(m.slug, account_kind, settings)
        account = accounts_by_slug.get(m.slug)
        if account is not None and account.status == "active":
            default_configured = True
            config_error = None
        manifests.append(
            ConnectorManifestOut(
                slug=m.slug,
                label=m.label,
                source_kind=m.source_kind,
                account_kinds=list(m.account_kinds),
                auth_modes=list(m.auth_modes),
                capabilities=list(m.capabilities),
                default_interval_seconds=m.default_interval_seconds,
                default_daily_budget=m.default_daily_budget,
                privacy_class=m.privacy_class,
                redaction_profile=m.redaction_profile,
                import_mode=m.import_mode,
                description=m.description,
                settings_keys=list(m.settings_keys),
                can_ensure_default=can_ensure_default_account(m.slug)
                and _can_run_connector_commands(account_kind, settings),
                default_configured=default_configured,
                config_error=config_error,
                config_fields=[
                    ConnectorConfigFieldOut(
                        key=field.key,
                        label=field.label,
                        kind=field.kind,
                        required=field.required,
                        placeholder=field.placeholder,
                        default_value=field.default_value,
                    )
                    for field in config_fields_for_slug(m.slug, settings)
                ],
            )
        )
    return ConnectorsOut(
        node_kind=settings.node_kind,
        node_id=settings.node_id,
        manifests=manifests,
        accounts=accounts,
        runs=runs,
        documents=docs,
    )


@router.post("/{connector_slug}/ensure", response_model=ConnectorAccountOut)
def ensure_connector(
    connector_slug: str,
    current: AuthenticatedUser = Depends(get_current_user),
) -> ConnectorAccountOut:
    settings = get_settings()
    require_node_operator(current)
    user_id = current.user_id
    register_default_connector_providers(replace=True)
    try:
        with audit("connector.command") as span:
            span.add_meta(
                action="ensure",
                connector_slug=connector_slug,
                node_id=settings.node_id,
                user_id=str(user_id),
            )
            _ensure_demo_user_row(user_id)
            account_id = ensure_default_connector_account(connector_slug, user_id, settings)
            span.set_output({"account_id": str(account_id)})
    except (SecretStoreError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    account = _find_account(user_id, account_id, _resolve_account_kind(None, settings))
    if account is None:
        raise HTTPException(status_code=404, detail="connector account not found")
    return account


@router.put("/{connector_slug}/config", response_model=ConnectorAccountOut)
def configure_connector(
    connector_slug: str,
    body: ConnectorConfigureIn,
    scope: ConnectorScope | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> ConnectorAccountOut:
    settings = get_settings()
    account_kind = _resolve_account_kind(scope, settings)
    require_node_operator(current)
    user_id = current.user_id
    register_default_connector_providers(replace=True)
    secrets = dict(body.secrets)
    if body.token is not None and "token" not in secrets:
        secrets["token"] = body.token
    try:
        with audit("connector.command") as span:
            span.add_meta(
                action="config",
                connector_slug=connector_slug,
                node_id=settings.node_id,
                scope=account_kind,
                user_id=str(user_id),
                setting_keys=sorted(body.settings),
                secret_keys=sorted(secrets),
            )
            _ensure_demo_user_row(user_id)
            account_id = configure_connector_account(
                connector_slug,
                user_id,
                input_settings=body.settings,
                input_secrets=secrets,
                account_label=body.account_label,
                settings=settings,
                account_kind=account_kind,
                # P6.7.4 tiered registration auth (§12.11 개정 3): owner/admin may
                # register any company address; a member only their verified email;
                # non-company domain / None email fails closed. Role decides the tier.
                owner_email=current.email,
                actor_role=current.role,
            )
            span.set_output({"account_id": str(account_id)})
    except (SecretStoreError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    account = _find_account(user_id, account_id, account_kind)
    if account is None:
        raise HTTPException(status_code=404, detail="connector account not found")
    return account


@router.delete("/{connector_slug}/accounts/{account_id}", response_model=ConnectorDeleteOut)
def delete_connector_account_route(
    connector_slug: str,
    account_id: UUID,
    scope: ConnectorScope | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> ConnectorDeleteOut:
    """Delete one connector account row the session user owns (P6.7.4 §12.11 개정 2).

    A user can only delete their own rows: the target must be a personal row whose
    `owner_id` equals the session user. Foreign / company rows return 404 so the
    delete never leaks existence. Mail mailboxes are the primary use (per-address
    removal), but the owner check applies to any personal slug.
    """

    settings = get_settings()
    account_kind = _resolve_account_kind(scope, settings)
    require_node_operator(current)
    user_id = current.user_id
    row = get_connector_account(account_id)
    mapping = row._mapping if row is not None else None
    if (
        mapping is None
        or mapping["node_id"] != settings.node_id
        or mapping["connector_slug"] != connector_slug.strip().lower()
        or mapping["account_kind"] != "personal"
        or mapping["owner_id"] != user_id
    ):
        raise HTTPException(status_code=404, detail="connector account not found")
    with audit("connector.command") as span:
        span.add_meta(
            action="delete",
            connector_slug=connector_slug,
            node_id=settings.node_id,
            scope=account_kind,
            user_id=str(user_id),
            account_id=str(account_id),
        )
        deleted = delete_connector_account(account_id)
        span.set_output({"account_id": str(account_id), "deleted": deleted})
    if not deleted:
        raise HTTPException(status_code=404, detail="connector account not found")
    return ConnectorDeleteOut(account_id=account_id, deleted=True)


@router.post("/{connector_slug}/sync", response_model=ConnectorSyncOut)
def sync_connector(
    connector_slug: str,
    background_tasks: BackgroundTasks,
    body: ConnectorSyncIn | None = None,
    scope: ConnectorScope | None = Query(default=None),
    account_id: UUID | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_current_user),
) -> ConnectorSyncOut:
    settings = get_settings()
    account_kind = _resolve_account_kind(scope, settings)
    require_node_operator(current)
    if account_kind == "personal" and not _can_run_connector_commands(account_kind, settings):
        raise HTTPException(
            status_code=400,
            detail="personal connector sync is handled by the desktop collector",
        )
    user_id = current.user_id
    register_default_connector_providers(replace=True)
    try:
        with audit("connector.command") as span:
            span.add_meta(
                action="sync",
                connector_slug=connector_slug,
                node_id=settings.node_id,
                scope=account_kind,
                user_id=str(user_id),
                background=bool(body.background) if body else False,
            )
            _ensure_demo_user_row(user_id)
            # P6.7.4 (§12.11): an explicit account_id targets one mailbox row the
            # session user owns. Without it, fall back to the slug's latest row.
            if account_id is not None:
                target = _owned_account_id(user_id, account_id, connector_slug, settings)
                if target is None:
                    raise HTTPException(status_code=404, detail="connector account not found")
                account_id = target
            else:
                existing = _find_account_for_slug(user_id, connector_slug, account_kind)
                account_id = (
                    existing.account_id
                    if existing is not None
                    else ensure_default_connector_account(connector_slug, user_id, settings)
                )
            span.set_output({"account_id": str(account_id)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    concurrency = body.concurrency if body else 8
    if body is not None and body.background:
        run_id = start_connector_run(
            account_id=account_id,
            connector_slug=connector_slug,
            reason="manual",
        )
        background_tasks.add_task(
            _run_sync_background,
            account_id,
            user_id,
            run_id,
            concurrency,
        )
        return ConnectorSyncOut(
            account_id=account_id,
            connector_slug=connector_slug,
            run_id=run_id,
            status="running",
            report=None,
            error_message=None,
        )

    result = run_connector_account_sync(
        account_id,
        user_id,
        reason="manual",
        continue_on_error=True,
        concurrency=concurrency,
    )
    report = result.report
    return ConnectorSyncOut(
        account_id=result.account_id,
        connector_slug=result.connector_slug,
        run_id=result.run_id,
        status=result.status,
        report=ConnectorReportOut(
            created=report.created,
            updated=report.updated,
            skipped=report.skipped,
            errors=report.errors,
            total=len(report.doc_ids),
            doc_ids=report.doc_ids,
        )
        if report is not None
        else None,
        error_message=result.error_message,
    )


@router.post("/notion/import", response_model=ImportOut)
def notion_import(
    body: NotionImportIn,
    current: AuthenticatedUser = Depends(get_current_user),
) -> ImportOut:
    settings = get_settings()
    require_node_operator(current)
    user_id = current.user_id
    token = settings.conn_notion_token
    if not token:
        raise HTTPException(
            status_code=400,
            detail="ORTHUS_CONN_NOTION_TOKEN not set",
        )
    with audit("connector.command") as span:
        span.add_meta(
            action="legacy_notion_import",
            connector_slug="notion",
            node_id=settings.node_id,
            user_id=str(user_id),
        )
        _ensure_demo_user_row(user_id)
        account_id = ensure_notion_account(user_id, settings)
        adopt_legacy_notion_documents(account_id)
        report = run_import(
            NotionConnector(token),
            user_id,
            since=body.since,
            scope=settings.node_kind,
            source_account_id=account_id,
        )
        span.set_output(
            {
                "account_id": str(account_id),
                "created": report.created,
                "updated": report.updated,
                "skipped": report.skipped,
                "doc_count": len(report.doc_ids),
            }
        )
    return ImportOut(
        created=report.created,
        updated=report.updated,
        skipped=report.skipped,
        doc_ids=report.doc_ids,
    )


def _run_sync_background(
    account_id: UUID,
    user_id: UUID,
    run_id: UUID,
    concurrency: int,
) -> None:
    run_connector_account_sync(
        account_id,
        user_id,
        reason="manual",
        run_id=run_id,
        continue_on_error=True,
        concurrency=concurrency,
    )


def _ensure_demo_user_row(user_id: UUID) -> None:
    """The FE demo header can point at an otherwise empty node DB."""
    with session() as s:
        s.execute(
            pg_insert(users)
            .values(
                user_id=user_id,
                external_id=f"demo:{user_id}",
                display_name="Demo User",
                preferred_timezone="Asia/Seoul",
            )
            .on_conflict_do_nothing(index_elements=[users.c.user_id])
        )
        s.commit()


def _resolve_account_kind(scope: ConnectorScope | None, settings) -> AccountKind:
    runtime_kind: AccountKind = "personal" if settings.node_kind == "personal" else "company"
    if scope is None:
        return runtime_kind
    if scope == "company":
        if settings.node_kind == "company":
            return "company"
        raise HTTPException(status_code=404, detail="company connectors are unavailable here")
    if settings.node_kind == "personal":
        return "personal"
    if settings.node_kind == "company" and settings.owner_scope_enabled:
        return "personal"
    raise HTTPException(status_code=404, detail="personal connectors are unavailable here")


def _can_run_connector_commands(account_kind: AccountKind, settings) -> bool:
    return account_kind == ("personal" if settings.node_kind == "personal" else "company")


def _default_account_status(
    connector_slug: str,
    account_kind: AccountKind,
    settings,
) -> tuple[bool, str | None]:
    if _can_run_connector_commands(account_kind, settings):
        config = default_account_config_status(connector_slug, settings)
        return config.configured, config.error
    return False, None


def _list_accounts(
    user_id: UUID, account_kind: AccountKind, device_id: str | None = None
) -> list[ConnectorAccountOut]:
    settings = get_settings()
    conditions = [
        connector_accounts.c.node_id == settings.node_id,
        connector_accounts.c.connector_slug.in_(_supported_connector_slugs(account_kind)),
        connector_accounts.c.account_kind == account_kind,
    ]
    if account_kind == "personal":
        conditions.append(connector_accounts.c.owner_id == user_id)
    # device_id is None on the session FE path (no device filter, current
    # behavior preserved). A collector token passes its own device_id so each
    # machine sees only its own accounts (migration 0070).
    if device_id is not None:
        conditions.append(connector_accounts.c.device_id == device_id)

    with session() as s:
        rows = s.execute(
            select(
                connector_accounts,
                connector_sync_state.c.last_sync_at,
                connector_sync_state.c.last_error,
            )
            .select_from(
                connector_accounts.outerjoin(
                    connector_sync_state,
                    connector_accounts.c.account_id == connector_sync_state.c.account_id,
                )
            )
            .where(and_(*conditions))
            .order_by(connector_accounts.c.connector_slug, connector_accounts.c.created_at)
        ).all()
    return [_account_out(row._mapping) for row in rows]


def _find_account(
    user_id: UUID,
    account_id: UUID,
    account_kind: AccountKind,
) -> ConnectorAccountOut | None:
    settings = get_settings()
    conditions = [
        connector_accounts.c.account_id == account_id,
        connector_accounts.c.node_id == settings.node_id,
        connector_accounts.c.connector_slug.in_(_supported_connector_slugs(account_kind)),
        connector_accounts.c.account_kind == account_kind,
    ]
    if account_kind == "personal":
        conditions.append(connector_accounts.c.owner_id == user_id)

    with session() as s:
        row = s.execute(
            select(
                connector_accounts,
                connector_sync_state.c.last_sync_at,
                connector_sync_state.c.last_error,
            )
            .select_from(
                connector_accounts.outerjoin(
                    connector_sync_state,
                    connector_accounts.c.account_id == connector_sync_state.c.account_id,
                )
            )
            .where(and_(*conditions))
        ).first()
    return _account_out(row._mapping) if row is not None else None


def _owned_account_id(
    user_id: UUID,
    account_id: UUID,
    connector_slug: str,
    settings,
) -> UUID | None:
    """Return account_id if it is a personal row on this node the user owns.

    Used by per-mailbox sync targeting (P6.7.4). Returns None for foreign/company
    rows or a slug mismatch so the caller surfaces 404 without leaking existence.
    """

    row = get_connector_account(account_id)
    mapping = row._mapping if row is not None else None
    if (
        mapping is None
        or mapping["node_id"] != settings.node_id
        or mapping["connector_slug"] != connector_slug.strip().lower()
        or mapping["account_kind"] != "personal"
        or mapping["owner_id"] != user_id
    ):
        return None
    return account_id


def _find_account_for_slug(
    user_id: UUID,
    connector_slug: str,
    account_kind: AccountKind,
) -> ConnectorAccountOut | None:
    settings = get_settings()
    conditions = [
        connector_accounts.c.connector_slug == connector_slug.strip().lower(),
        connector_accounts.c.node_id == settings.node_id,
        connector_accounts.c.status == "active",
        connector_accounts.c.connector_slug.in_(_supported_connector_slugs(account_kind)),
        connector_accounts.c.account_kind == account_kind,
    ]
    if account_kind == "personal":
        conditions.append(connector_accounts.c.owner_id == user_id)

    with session() as s:
        row = s.execute(
            select(
                connector_accounts,
                connector_sync_state.c.last_sync_at,
                connector_sync_state.c.last_error,
            )
            .select_from(
                connector_accounts.outerjoin(
                    connector_sync_state,
                    connector_accounts.c.account_id == connector_sync_state.c.account_id,
                )
            )
            .where(and_(*conditions))
            .order_by(connector_accounts.c.updated_at.desc())
        ).first()
    return _account_out(row._mapping) if row is not None else None


def _list_runs(
    user_id: UUID,
    account_kind: AccountKind,
    *,
    limit: int = 50,
) -> list[ConnectorRunOut]:
    settings = get_settings()
    conditions = [
        connector_accounts.c.node_id == settings.node_id,
        connector_accounts.c.connector_slug.in_(_supported_connector_slugs(account_kind)),
        connector_accounts.c.account_kind == account_kind,
    ]
    if account_kind == "personal":
        conditions.append(connector_accounts.c.owner_id == user_id)

    with session() as s:
        rows = s.execute(
            select(connector_runs)
            .select_from(
                connector_runs.join(
                    connector_accounts,
                    connector_runs.c.account_id == connector_accounts.c.account_id,
                )
            )
            .where(and_(*conditions))
            .order_by(connector_runs.c.started_at.desc())
            .limit(limit)
        ).all()
    return [_run_out(row._mapping) for row in rows]


def _list_recent_documents(
    user_id: UUID,
    account_kind: AccountKind,
    *,
    limit: int = 80,
) -> list[ConnectorDocumentOut]:
    settings = get_settings()
    conditions = [
        documents.c.user_id == user_id,
        documents.c.source_account_id.is_not(None),
        connector_accounts.c.node_id == settings.node_id,
        connector_accounts.c.connector_slug.in_(_supported_connector_slugs(account_kind)),
        connector_accounts.c.account_kind == account_kind,
    ]
    if account_kind == "personal":
        conditions.append(connector_accounts.c.owner_id == user_id)

    with session() as s:
        rows = s.execute(
            select(
                documents.c.doc_id,
                documents.c.source_account_id.label("account_id"),
                connector_accounts.c.connector_slug,
                documents.c.title,
                documents.c.source,
                documents.c.source_external_id,
                documents.c.source_last_edited_at,
                documents.c.updated_at,
            )
            .select_from(
                documents.join(
                    connector_accounts,
                    documents.c.source_account_id == connector_accounts.c.account_id,
                )
            )
            .where(and_(*conditions))
            .order_by(documents.c.updated_at.desc())
            .limit(limit)
        ).all()
        out = [_document_out(row._mapping) for row in rows]
        if account_kind == "personal":
            collector_rows = s.execute(
                select(
                    documents.c.doc_id,
                    documents.c.source_account_id.label("account_id"),
                    documents.c.source.label("connector_slug"),
                    documents.c.title,
                    documents.c.source,
                    documents.c.source_external_id,
                    documents.c.source_last_edited_at,
                    documents.c.updated_at,
                )
                .where(
                    documents.c.user_id == user_id,
                    documents.c.scope == "personal",
                    documents.c.source_account_id.is_(None),
                    documents.c.source.in_(_supported_connector_slugs(account_kind)),
                )
                .order_by(documents.c.updated_at.desc())
                .limit(limit)
            ).all()
            out.extend(_document_out(row._mapping) for row in collector_rows)
    out.sort(key=lambda doc: doc.updated_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return out[:limit]


def _supported_connector_slugs(account_kind: AccountKind) -> set[str]:
    return {
        manifest.slug
        for manifest in list_connector_manifests()
        if manifest.supports_account_kind(account_kind)
    }


def _account_out(row) -> ConnectorAccountOut:
    return ConnectorAccountOut(
        account_id=row["account_id"],
        connector_slug=row["connector_slug"],
        account_kind=row["account_kind"],
        node_id=row["node_id"],
        scope=row["scope"],
        owner_id=row["owner_id"],
        project=row["project"],
        auth_mode=row["auth_mode"],
        account_label=row["account_label"],
        status=row["status"],
        settings_redacted=row["settings_redacted"],
        device_id=row["device_id"] or "",
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_sync_at=row["last_sync_at"],
        last_error=row["last_error"],
    )


def _run_out(row) -> ConnectorRunOut:
    return ConnectorRunOut(
        run_id=row["run_id"],
        account_id=row["account_id"],
        connector_slug=row["connector_slug"],
        reason=row["reason"],
        status=row["status"],
        fetched=row["fetched"],
        created=row["created"],
        updated=row["updated"],
        skipped=row["skipped"],
        errors=row["errors"],
        error_message=row["error_message"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _document_out(row) -> ConnectorDocumentOut:
    return ConnectorDocumentOut(
        doc_id=row["doc_id"],
        account_id=row["account_id"],
        connector_slug=row["connector_slug"],
        title=row["title"],
        source=row["source"],
        source_external_id=row["source_external_id"],
        source_last_edited_at=row["source_last_edited_at"],
        updated_at=row["updated_at"],
    )
