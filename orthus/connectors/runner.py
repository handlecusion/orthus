"""Account-scoped connector runner.

OpenHuman uses one periodic pass over active connections. C2 mirrors that
shape without adding a daemon yet: caller invokes one tick, runner finds due
accounts, dispatches registered providers, and records run/state rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, select

from orthus.connectors.base import ImportReport, run_import, run_parallel_import
from orthus.connectors.registry import get_connector_provider, registered_connector_slugs
from orthus.connectors.state import (
    finish_connector_run,
    get_sync_state,
    save_sync_state,
    start_connector_run,
)
from orthus.db import session
from orthus.models.base import ChatModel
from orthus.tables import connector_accounts, connector_sync_state

SyncReason = Literal["manual", "periodic", "connection_created"]


@dataclass(frozen=True)
class ConnectorRunResult:
    account_id: UUID
    connector_slug: str
    run_id: UUID
    status: Literal["succeeded", "failed"]
    report: ImportReport | None = None
    error_message: str | None = None


def due_connector_accounts(
    *,
    now: datetime | None = None,
    node_id: str | None = None,
    reason: SyncReason = "periodic",
) -> list[dict]:
    """Return active account rows whose registered manifest is due."""
    if reason != "periodic":
        return []

    slugs = registered_connector_slugs()
    if not slugs:
        return []

    conditions = [
        connector_accounts.c.status == "active",
        connector_accounts.c.connector_slug.in_(slugs),
    ]
    if node_id is not None:
        conditions.append(connector_accounts.c.node_id == node_id)

    with session() as s:
        rows = s.execute(
            select(connector_accounts, connector_sync_state.c.last_sync_at)
            .select_from(
                connector_accounts.outerjoin(
                    connector_sync_state,
                    connector_accounts.c.account_id == connector_sync_state.c.account_id,
                )
            )
            .where(and_(*conditions))
            .order_by(connector_accounts.c.connector_slug, connector_accounts.c.created_at)
        ).all()

    current = now or datetime.now(UTC)
    due: list[dict] = []
    for row in rows:
        data = dict(row._mapping)
        provider = get_connector_provider(data["connector_slug"])
        if provider is None:
            continue
        manifest = provider.manifest
        if not manifest.can_periodic_sync():
            continue
        if not manifest.supports_account_kind(data["account_kind"]):
            continue
        if not manifest.supports_auth_mode(data["auth_mode"]):
            continue

        last_sync_at = data.get("last_sync_at")
        if last_sync_at is None:
            due.append(data)
            continue
        elapsed = (current - last_sync_at).total_seconds()
        if elapsed >= (manifest.default_interval_seconds or 0):
            due.append(data)
    return due


def run_connector_account_sync(
    account_id: UUID,
    user_id: UUID,
    *,
    reason: SyncReason = "manual",
    run_id: UUID | None = None,
    continue_on_error: bool = True,
    concurrency: int = 8,
    chat_model: ChatModel | None = None,
) -> ConnectorRunResult:
    """Sync one connector account through its registered provider."""
    account = _load_account(account_id)
    connector_slug = str(account["connector_slug"])
    provider = get_connector_provider(connector_slug)
    if provider is None:
        raise KeyError(f"connector provider not registered: {connector_slug}")

    manifest = provider.manifest
    if not manifest.supports_account_kind(str(account["account_kind"])):
        raise ValueError(
            f"{connector_slug} does not support account_kind={account['account_kind']}"
        )
    if not manifest.supports_auth_mode(str(account["auth_mode"])):
        raise ValueError(f"{connector_slug} does not support auth_mode={account['auth_mode']}")
    if account["account_kind"] == "personal" and account["owner_id"] != user_id:
        raise ValueError("personal connector account owner mismatch")

    state = get_sync_state(account_id)
    if run_id is None:
        run_id = start_connector_run(
            account_id=account_id,
            connector_slug=connector_slug,
            reason=reason,
        )

    try:
        connector = provider.build_connector(account, state)
        since = state.last_sync_at if state is not None else None
        scope = str(account["scope"])
        kwargs = {
            "scope": scope,
            "source_account_id": account_id,
            "continue_on_error": continue_on_error,
        }
        if manifest.import_mode == "parallel":
            report = run_parallel_import(
                connector,
                user_id,
                since=since,
                owner_id=account["owner_id"],
                concurrency=concurrency,
                chat_model=chat_model,
                **kwargs,
            )
        else:
            report = run_import(connector, user_id, since=since, **kwargs)

        finish_connector_run(
            run_id,
            status="succeeded",
            fetched=len(report.doc_ids) + report.errors,
            created=report.created,
            updated=report.updated,
            skipped=report.skipped,
            errors=report.errors,
        )
        save_sync_state(account_id, last_sync_at=datetime.now(UTC), last_error=None)
        return ConnectorRunResult(
            account_id=account_id,
            connector_slug=connector_slug,
            run_id=run_id,
            status="succeeded",
            report=report,
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        finish_connector_run(
            run_id,
            status="failed",
            errors=1,
            error_message=message,
        )
        save_sync_state(account_id, last_error=message)
        return ConnectorRunResult(
            account_id=account_id,
            connector_slug=connector_slug,
            run_id=run_id,
            status="failed",
            error_message=message,
        )


def run_due_connector_syncs(
    user_id: UUID,
    *,
    now: datetime | None = None,
    node_id: str | None = None,
    continue_on_error: bool = True,
    concurrency: int = 8,
    chat_model: ChatModel | None = None,
) -> list[ConnectorRunResult]:
    """Run one periodic tick across due active accounts."""
    results: list[ConnectorRunResult] = []
    for account in due_connector_accounts(now=now, node_id=node_id, reason="periodic"):
        results.append(
            run_connector_account_sync(
                account["account_id"],
                user_id,
                reason="periodic",
                continue_on_error=continue_on_error,
                concurrency=concurrency,
                chat_model=chat_model,
            )
        )
    return results


def _load_account(account_id: UUID) -> dict:
    with session() as s:
        row = s.execute(
            select(connector_accounts).where(connector_accounts.c.account_id == account_id)
        ).first()
    if row is None:
        raise KeyError(f"connector account not found: {account_id}")
    return dict(row._mapping)
