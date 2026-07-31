"""Connector account/run/state persistence.

C0 keeps connector code shared across company and personal nodes. Account rows
carry the policy boundary: account_kind/scope/owner/project/node/auth mode.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.db import session
from orthus.tables import (
    connector_accounts,
    connector_items,
    connector_runs,
    connector_sync_state,
)

AccountKind = Literal["company", "personal"]
ConnectorStatus = Literal["active", "paused", "error", "disabled"]
RunStatus = Literal["running", "succeeded", "failed"]
_ACCOUNT_NS = uuid.UUID("1a65b6d6-5a52-4d19-b13d-4c6edcc637e1")
_UNSET = object()


@dataclass(frozen=True)
class ConnectorAccountSpec:
    connector_slug: str
    account_kind: AccountKind
    node_id: str
    auth_mode: str
    owner_id: UUID | None = None
    project: str | None = None
    account_label: str | None = None
    status: ConnectorStatus = "active"
    settings_redacted: dict = field(default_factory=dict)
    account_id: UUID | None = None
    # P6.7.4 Model B (`docs/p6-unified-mail.md` §12.11): an optional key suffix so
    # one owner can hold multiple rows of the same slug (several mailboxes per
    # domain). Empty (the default for every non-mail connector) keeps the
    # deterministic id byte-identical to before.
    discriminator: str = ""
    # Per-token machine identity (migration 0070). Empty keeps the
    # deterministic id byte-identical to before; a non-empty device_id splits
    # one owner's accounts per machine so two collector daemons don't overwrite
    # each other's settings.
    device_id: str = ""

    @property
    def scope(self) -> str:
        return self.account_kind


@dataclass(frozen=True)
class SyncState:
    account_id: UUID
    cursor_json: dict
    seen_json: dict
    daily_budget_json: dict
    last_seen_id: str | None
    last_sync_at: datetime | None
    last_error: str | None


def _validate_account(spec: ConnectorAccountSpec) -> None:
    if not spec.connector_slug.strip():
        raise ValueError("connector_slug required")
    if not spec.node_id.strip():
        raise ValueError("node_id required")
    if spec.account_kind == "company" and spec.owner_id is not None:
        raise ValueError("company connector account must not set owner_id")
    if spec.account_kind == "personal" and spec.owner_id is None:
        raise ValueError("personal connector account requires owner_id")


def create_connector_account(spec: ConnectorAccountSpec) -> UUID:
    """Create one connector account row and empty sync state."""
    _validate_account(spec)
    account_id = spec.account_id or uuid.uuid4()
    now = datetime.now(UTC)
    with session() as s:
        s.execute(
            insert(connector_accounts).values(
                account_id=account_id,
                connector_slug=spec.connector_slug,
                account_kind=spec.account_kind,
                node_id=spec.node_id,
                scope=spec.scope,
                owner_id=spec.owner_id,
                project=spec.project,
                auth_mode=spec.auth_mode,
                account_label=spec.account_label,
                status=spec.status,
                settings_redacted=spec.settings_redacted,
                created_at=now,
                updated_at=now,
            )
        )
        s.execute(
            insert(connector_sync_state).values(
                account_id=account_id,
                cursor_json={},
                seen_json={},
                daily_budget_json={},
                updated_at=now,
            )
        )
        s.commit()
    return account_id


def deterministic_account_id(spec: ConnectorAccountSpec) -> UUID:
    """Stable account id for node-local default connector accounts."""
    _validate_account(spec)
    key_parts = [
        spec.connector_slug,
        spec.account_kind,
        spec.node_id,
        str(spec.owner_id) if spec.owner_id else "",
        spec.project or "",
        spec.auth_mode,
    ]
    # P6.7.4 Model B (§12.11): only mail slugs set a non-empty discriminator, so
    # an empty one appends nothing and the key stays byte-identical to before.
    if spec.discriminator:
        key_parts.append(spec.discriminator)
    # Per-device split (migration 0070): same guarded pattern. An empty
    # device_id appends nothing, so the key stays byte-identical to before.
    if spec.device_id:
        key_parts.append(spec.device_id)
    key = ":".join(key_parts)
    return uuid.uuid5(_ACCOUNT_NS, key)


def ensure_connector_account(spec: ConnectorAccountSpec) -> UUID:
    """Idempotently create/update a connector account plus empty sync state."""
    _validate_account(spec)
    account_id = spec.account_id or deterministic_account_id(spec)
    now = datetime.now(UTC)
    account_stmt = pg_insert(connector_accounts).values(
        account_id=account_id,
        connector_slug=spec.connector_slug,
        account_kind=spec.account_kind,
        node_id=spec.node_id,
        scope=spec.scope,
        owner_id=spec.owner_id,
        project=spec.project,
        auth_mode=spec.auth_mode,
        account_label=spec.account_label,
        status=spec.status,
        settings_redacted=spec.settings_redacted,
        device_id=spec.device_id,
        created_at=now,
        updated_at=now,
    )
    state_stmt = pg_insert(connector_sync_state).values(
        account_id=account_id,
        cursor_json={},
        seen_json={},
        daily_budget_json={},
        updated_at=now,
    )
    with session() as s:
        s.execute(
            account_stmt.on_conflict_do_update(
                index_elements=[connector_accounts.c.account_id],
                set_={
                    "connector_slug": account_stmt.excluded.connector_slug,
                    "account_kind": account_stmt.excluded.account_kind,
                    "node_id": account_stmt.excluded.node_id,
                    "scope": account_stmt.excluded.scope,
                    "owner_id": account_stmt.excluded.owner_id,
                    "project": account_stmt.excluded.project,
                    "auth_mode": account_stmt.excluded.auth_mode,
                    "account_label": account_stmt.excluded.account_label,
                    "status": account_stmt.excluded.status,
                    "settings_redacted": account_stmt.excluded.settings_redacted,
                    "device_id": account_stmt.excluded.device_id,
                    "updated_at": account_stmt.excluded.updated_at,
                },
            )
        )
        s.execute(
            state_stmt.on_conflict_do_nothing(index_elements=[connector_sync_state.c.account_id])
        )
        s.commit()
    return account_id


def get_connector_account(account_id: UUID):
    """Return SQLAlchemy row for account_id, or None."""
    with session() as s:
        return s.execute(
            select(connector_accounts).where(connector_accounts.c.account_id == account_id)
        ).first()


def get_sync_state(account_id: UUID) -> SyncState | None:
    with session() as s:
        row = s.execute(
            select(connector_sync_state).where(connector_sync_state.c.account_id == account_id)
        ).first()
    if row is None:
        return None
    r = row._mapping
    return SyncState(
        account_id=r["account_id"],
        cursor_json=r["cursor_json"],
        seen_json=r["seen_json"],
        daily_budget_json=r["daily_budget_json"],
        last_seen_id=r["last_seen_id"],
        last_sync_at=r["last_sync_at"],
        last_error=r["last_error"],
    )


def save_sync_state(
    account_id: UUID,
    *,
    cursor_json: dict | None = None,
    seen_json: dict | None = None,
    daily_budget_json: dict | None = None,
    last_seen_id: str | None = None,
    last_sync_at: datetime | None = None,
    last_error: str | None | object = _UNSET,
) -> None:
    """Upsert sync state for one account."""
    values = {
        "account_id": account_id,
        "updated_at": datetime.now(UTC),
    }
    if cursor_json is not None:
        values["cursor_json"] = cursor_json
    if seen_json is not None:
        values["seen_json"] = seen_json
    if daily_budget_json is not None:
        values["daily_budget_json"] = daily_budget_json
    if last_seen_id is not None:
        values["last_seen_id"] = last_seen_id
    if last_sync_at is not None:
        values["last_sync_at"] = last_sync_at
    if last_error is not _UNSET:
        values["last_error"] = last_error

    stmt = pg_insert(connector_sync_state).values(**values)
    update_values = {k: stmt.excluded[k] for k in values if k != "account_id"}
    with session() as s:
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=[connector_sync_state.c.account_id],
                set_=update_values,
            )
        )
        s.commit()


def upsert_connector_item(
    account_id: UUID,
    external_id: str,
    *,
    external_version: str | None = None,
    content_hash: str | None = None,
    doc_id: UUID | None = None,
) -> None:
    """Track account-local external item -> document mapping."""
    stmt = pg_insert(connector_items).values(
        account_id=account_id,
        external_id=external_id,
        external_version=external_version,
        content_hash=content_hash,
        doc_id=doc_id,
        ingested_at=datetime.now(UTC),
    )
    with session() as s:
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=[connector_items.c.account_id, connector_items.c.external_id],
                set_={
                    "external_version": stmt.excluded.external_version,
                    "content_hash": stmt.excluded.content_hash,
                    "doc_id": stmt.excluded.doc_id,
                    "ingested_at": stmt.excluded.ingested_at,
                },
            )
        )
        s.commit()


def start_connector_run(
    *,
    connector_slug: str,
    reason: str,
    account_id: UUID | None = None,
    run_id: UUID | None = None,
) -> UUID:
    rid = run_id or uuid.uuid4()
    with session() as s:
        s.execute(
            insert(connector_runs).values(
                run_id=rid,
                account_id=account_id,
                connector_slug=connector_slug,
                reason=reason,
                status="running",
            )
        )
        s.commit()
    return rid


def finish_connector_run(
    run_id: UUID,
    *,
    status: RunStatus,
    fetched: int = 0,
    created: int = 0,
    updated: int = 0,
    skipped: int = 0,
    errors: int = 0,
    error_message: str | None = None,
) -> None:
    with session() as s:
        s.execute(
            update(connector_runs)
            .where(connector_runs.c.run_id == run_id)
            .values(
                status=status,
                fetched=fetched,
                created=created,
                updated=updated,
                skipped=skipped,
                errors=errors,
                error_message=error_message,
                finished_at=datetime.now(UTC),
            )
        )
        s.commit()


def delete_connector_account(account_id: UUID) -> bool:
    """Delete one connector account row plus its empty sync-state row.

    P6.7.4 (`docs/p6-unified-mail.md` §12.11 개정 2): per-mailbox removal. Returns
    True if an account row was deleted. `connector_runs`/`connector_items` history
    is left intact (no FK cascade); a fresh mailbox under the same address would
    get a new account_id only if its owner_addr differs. Caller enforces
    owner_id==session user before calling this.
    """

    with session() as s:
        result = s.execute(
            delete(connector_accounts).where(connector_accounts.c.account_id == account_id)
        )
        s.execute(
            delete(connector_sync_state).where(connector_sync_state.c.account_id == account_id)
        )
        s.commit()
    return (result.rowcount or 0) > 0
