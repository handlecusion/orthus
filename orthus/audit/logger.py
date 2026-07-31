"""Per-call audit. `audit()` wraps a unit of work as a span:

- each call gets a fresh `node_run_id` (UUID); a retried call gets a new one.
- `enter` row written immediately (survives a crash mid-span).
- `exit` row on success (with redacted output + meta), `error` row on exception.
- `correlation_id` propagated via contextvars across nested spans.

Used on every 비서 step (compile/validate/execute), RAG retrieval, and embedding
calls (prompt §4.3). `(node_run_id, phase)` is unique (uq_audit_run_phase)."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert

from orthus.audit.redact import redact_pii
from orthus.db import session
from orthus.tables import audit_log

_correlation_id: contextvars.ContextVar[UUID | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def current_correlation_id() -> UUID | None:
    return _correlation_id.get()


class AuditSpan:
    """Handle a node uses to attach output/meta recorded on the `exit` row."""

    def __init__(self, correlation_id: UUID, node_run_id: UUID, node: str):
        self.correlation_id = correlation_id
        self.node_run_id = node_run_id
        self.node = node
        self.output: Any = None
        self.meta: dict[str, Any] = {}

    def set_output(self, output: Any) -> None:
        self.output = output

    def add_meta(self, **kw: Any) -> None:
        self.meta.update(kw)


def _write(
    correlation_id: UUID,
    node_run_id: UUID,
    node: str,
    phase: str,
    *,
    output: Any = None,
    meta: dict | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    with session() as s:
        s.execute(
            insert(audit_log).values(
                correlation_id=correlation_id,
                node_run_id=node_run_id,
                node=node,
                phase=phase,
                output=redact_pii(output) if output is not None else None,
                meta=redact_pii(meta or {}),
                error_class=error_class,
                error_message=error_message,
            )
        )
        s.commit()


@contextmanager
def audit(node: str, *, correlation_id: UUID | None = None) -> Iterator[AuditSpan]:
    cid = correlation_id or _correlation_id.get() or uuid4()
    token = _correlation_id.set(cid)
    run_id = uuid4()
    span = AuditSpan(cid, run_id, node)
    _write(cid, run_id, node, "enter")
    try:
        yield span
    except Exception as e:  # noqa: BLE001 — record then re-raise
        _write(
            cid,
            run_id,
            node,
            "error",
            error_class=type(e).__name__,
            error_message=str(e),
        )
        raise
    else:
        _write(cid, run_id, node, "exit", output=span.output, meta=span.meta)
    finally:
        _correlation_id.reset(token)
