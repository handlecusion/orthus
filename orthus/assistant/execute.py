"""Read-only execution of a validated SELECT (prompt §6.4). Runs only through a
read-only engine, with a per-statement timeout. Non-JSON cell types are coerced
to str so the result serializes cleanly."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from sqlalchemy import create_engine, text

from orthus.audit import audit
from orthus.db import get_ro_engine
from orthus.settings import get_settings


def _engine_for(dsn: str):
    if dsn == get_settings().pg_dsn_readonly:
        return get_ro_engine()
    return create_engine(
        dsn,
        pool_pre_ping=True,
        future=True,
        execution_options={"postgresql_readonly": True},
    )


def _coerce(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, time, UUID, Decimal)):
        return str(value)
    return str(value)


def execute_readonly(sql: str, dsn: str, timeout_ms: int) -> tuple[list[str], list[list], int]:
    """Execute `sql` read-only. Returns (columns, rows, row_count)."""
    with audit("assistant.execute") as span:
        engine = _engine_for(dsn)
        # statement_timeout takes a literal, not a bind param; timeout_ms is a
        # trusted int from settings (coerced here, never user input).
        timeout = int(timeout_ms)
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = {timeout}"))
            res = conn.execute(text(sql))
            columns = list(res.keys())
            rows = [[_coerce(c) for c in row] for row in res.fetchall()]
        span.add_meta(row_count=len(rows), timeout_ms=timeout_ms)
        return columns, rows, len(rows)
