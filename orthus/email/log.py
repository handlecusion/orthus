"""Shared hash-only email send log helpers.

P3 agent-work auto-send and P6.3 manual compose/send both write to
`email_send_log` with hashes only — never raw recipient/subject/body. The
rate-limit window (one send per recipient_hash per node/owner per hour) and the
sha256 recipient/subject/body hashing live here so both paths stay
behavior-identical.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import insert, select

from orthus.db import session
from orthus.tables import email_send_log


def email_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def email_send_rate_limited(
    *,
    node_id: str,
    owner_id: UUID | None,
    recipient_hash: str,
    now: datetime,
) -> bool:
    """True when a sent row already exists for this recipient in the last hour."""

    if owner_id is None:
        return True
    cutoff = now - timedelta(hours=1)
    with session() as s:
        return (
            s.execute(
                select(email_send_log.c.send_id)
                .where(
                    email_send_log.c.node_id == node_id,
                    email_send_log.c.owner_id == owner_id,
                    email_send_log.c.recipient_hash == recipient_hash,
                    email_send_log.c.status == "sent",
                    email_send_log.c.sent_at >= cutoff,
                )
                .limit(1)
            ).first()
            is not None
        )


def email_send_log_exists(work_id: UUID | None) -> bool:
    """True when any email_send_log row already exists for this work_id.

    Reply auto-send idempotency: a second approve/send for the same work item must
    not re-send. Distinct from the per-recipient rate limit (which only matches
    status='sent' in the last hour)."""

    if work_id is None:
        return False
    with session() as s:
        return (
            s.execute(
                select(email_send_log.c.send_id).where(email_send_log.c.work_id == work_id).limit(1)
            ).first()
            is not None
        )


def insert_email_send_log(
    *,
    node_id: str,
    owner_id: UUID | None,
    recipient_hash: str,
    subject_hash: str,
    body_hash: str,
    sender_kind: str,
    status: str,
    sent_at: datetime,
    origin: str = "agent_work",
    work_id: UUID | None = None,
    correlation_id: UUID | None = None,
    error_message: str | None = None,
) -> UUID:
    if owner_id is None:
        raise ValueError("email send log requires owner_id")
    send_id = uuid4()
    with session() as s:
        s.execute(
            insert(email_send_log).values(
                send_id=send_id,
                work_id=work_id,
                node_id=node_id,
                owner_id=owner_id,
                recipient_hash=recipient_hash,
                subject_hash=subject_hash,
                body_hash=body_hash,
                sender_kind=sender_kind,
                status=status,
                origin=origin,
                sent_at=sent_at,
                correlation_id=correlation_id,
                error_message=error_message,
            )
        )
        s.commit()
    return send_id
