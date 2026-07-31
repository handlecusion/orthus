"""Per-owner command queue between central and the personal collector daemon.

central appends commands (session operator path); the collector daemon polls its
OWN owner's pending commands and reports results (collector-token path). Every
read/write is scoped to a single user_id so one owner can never list, claim, or
complete another owner's commands. Lifecycle: pending -> claimed -> done|failed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import insert, or_, select, update

from orthus.audit import audit
from orthus.db import session
from orthus.schemas.canonical import (
    CollectorCommand,
    CollectorCommandList,
    CollectorCommandStatus,
)
from orthus.settings import Settings
from orthus.tables import collector_commands


class CollectorCommandError(RuntimeError):
    """Raised when a command transition is not allowed (missing/wrong owner/state)."""


CLAIM_TIMEOUT_SECONDS = 30 * 60


def create_command(
    settings: Settings,
    *,
    user_id: UUID,
    created_by: UUID,
    kind: str,
    payload: dict,
    device_id: str | None = None,
) -> CollectorCommand:
    command_id = uuid.uuid4()
    now = datetime.now(UTC)
    with audit("collector.command.create") as span:
        span.add_meta(user_id=str(user_id), kind=kind, command_id=str(command_id))
        requeued = requeue_stale_claims(user_id, now=now)
        if requeued:
            span.add_meta(requeued_stale_claims=requeued)
        with session() as s:
            # A device-targeted command must never coalesce with a broadcast
            # one (or another device's), so match device_id exactly — None
            # (broadcast) only coalesces with NULL rows (migration 0070).
            device_match = (
                collector_commands.c.device_id.is_(None)
                if device_id is None
                else collector_commands.c.device_id == device_id
            )
            existing = s.execute(
                select(collector_commands)
                .where(
                    collector_commands.c.node_id == settings.node_id,
                    collector_commands.c.user_id == user_id,
                    collector_commands.c.kind == kind,
                    collector_commands.c.payload == payload,
                    device_match,
                    collector_commands.c.status.in_(["pending", "claimed"]),
                )
                .order_by(collector_commands.c.created_at.desc())
                .limit(1)
            ).first()
            if existing is not None:
                span.add_meta(coalesced=True, existing_command_id=str(existing.command_id))
                # Notify even on coalesce: the daemon poll-drains all pending
                # commands, so a duplicate notify is idempotent and just nudges a
                # connected daemon to pick up the still-pending row immediately.
                _notify_command_available(settings, user_id, existing.command_id, kind)
                return _row_to_command(existing)
            s.execute(
                insert(collector_commands).values(
                    command_id=command_id,
                    node_id=settings.node_id,
                    user_id=user_id,
                    kind=kind,
                    payload=payload,
                    device_id=device_id,
                    status="pending",
                    result=None,
                    created_by=created_by,
                    created_at=now,
                )
            )
            s.commit()
    # Slice 8: push a 1-way "command available" notify to a connected collector
    # daemon (central->daemon WebSocket) so it claims immediately instead of
    # waiting for its poll interval. Fail-closed behind the flag; a no-op when no
    # daemon of this owner is connected (the daemon's poll fallback still works).
    _notify_command_available(settings, user_id, command_id, kind)
    return _load_command(user_id, command_id)


def _notify_command_available(
    settings: Settings, user_id: UUID, command_id: UUID, kind: str
) -> None:
    # Imported lazily / late-bound so this DB-layer module never imports the
    # event-loop registry at import time. get_settings is read fresh because a
    # caller may pass a node-bound settings snapshot.
    from orthus.settings import get_settings

    if not get_settings().collector_ws_enabled:
        return
    from orthus.collector.ws_registry import WS_REGISTRY

    WS_REGISTRY.notify(
        settings.node_id,
        user_id,
        {"type": "command_available", "command_id": str(command_id), "kind": kind},
    )


def list_commands(
    user_id: UUID, *, status: CollectorCommandStatus | None = None
) -> CollectorCommandList:
    clauses = [collector_commands.c.user_id == user_id]
    if status is not None:
        clauses.append(collector_commands.c.status == status)
    with session() as s:
        rows = s.execute(
            select(collector_commands)
            .where(*clauses)
            .order_by(collector_commands.c.created_at.desc())
        ).all()
    items = [_row_to_command(row) for row in rows]
    return CollectorCommandList(count=len(items), items=items)


def list_pending_commands(user_id: UUID, device_id: str | None = None) -> CollectorCommandList:
    requeue_stale_claims(user_id)
    # A token WITHOUT a device (None/"") gets only broadcast (NULL) commands; a
    # device token gets broadcast + its own (migration 0070).
    device_filter = collector_commands.c.device_id.is_(None)
    if device_id:
        device_filter = or_(device_filter, collector_commands.c.device_id == device_id)
    with session() as s:
        rows = s.execute(
            select(collector_commands)
            .where(
                collector_commands.c.user_id == user_id,
                collector_commands.c.status == "pending",
                device_filter,
            )
            .order_by(collector_commands.c.created_at.asc())
        ).all()
    items = [_row_to_command(row) for row in rows]
    return CollectorCommandList(count=len(items), items=items)


def requeue_stale_claims(user_id: UUID, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=CLAIM_TIMEOUT_SECONDS)
    with session() as s:
        updated = s.execute(
            update(collector_commands)
            .where(
                collector_commands.c.user_id == user_id,
                collector_commands.c.status == "claimed",
                collector_commands.c.claimed_at.is_not(None),
                collector_commands.c.claimed_at < cutoff,
            )
            .values(status="pending", claimed_at=None)
        )
        s.commit()
    return int(updated.rowcount or 0)


def claim_command(user_id: UUID, command_id: UUID) -> CollectorCommand:
    now = datetime.now(UTC)
    with audit("collector.command.claim") as span:
        span.add_meta(user_id=str(user_id), command_id=str(command_id))
        with session() as s:
            result = s.execute(
                update(collector_commands)
                .where(
                    collector_commands.c.command_id == command_id,
                    collector_commands.c.user_id == user_id,
                    collector_commands.c.status == "pending",
                )
                .values(status="claimed", claimed_at=now)
            )
            s.commit()
        if result.rowcount != 1:
            raise CollectorCommandError("command not claimable")
        span.add_meta(claimed=True)
    return _load_command(user_id, command_id)


def complete_command(
    user_id: UUID, command_id: UUID, *, status: str, result_payload: dict
) -> CollectorCommand:
    now = datetime.now(UTC)
    with audit("collector.command.complete") as span:
        span.add_meta(user_id=str(user_id), command_id=str(command_id), status=status)
        with session() as s:
            updated = s.execute(
                update(collector_commands)
                .where(
                    collector_commands.c.command_id == command_id,
                    collector_commands.c.user_id == user_id,
                    collector_commands.c.status == "claimed",
                )
                .values(status=status, result=result_payload, completed_at=now)
            )
            s.commit()
        if updated.rowcount != 1:
            raise CollectorCommandError("command not completable")
    return _load_command(user_id, command_id)


def _load_command(user_id: UUID, command_id: UUID) -> CollectorCommand:
    with session() as s:
        row = s.execute(
            select(collector_commands).where(
                collector_commands.c.command_id == command_id,
                collector_commands.c.user_id == user_id,
            )
        ).first()
    if row is None:
        raise CollectorCommandError("command not found")
    return _row_to_command(row)


def _row_to_command(row) -> CollectorCommand:
    return CollectorCommand(
        command_id=row.command_id,
        node_id=row.node_id,
        user_id=row.user_id,
        kind=row.kind,
        payload=row.payload or {},
        device_id=row.device_id,
        status=row.status,
        result=row.result,
        created_at=row.created_at,
        claimed_at=row.claimed_at,
        completed_at=row.completed_at,
    )
