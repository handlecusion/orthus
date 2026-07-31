"""Postgres catalog introspection + DSN resolution. `resolve_dsn` backs the
optional external-DSN path of the validation gate; `introspect_postgres` reads
live `information_schema` columns. The structured(PG) backend builds its own
logical catalog in `orthus.structured.query` (P2.2a)."""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from orthus.settings import get_settings

_RO_DSN_KEY = "ORTHUS_PG_DSN_READONLY"


def resolve_dsn(dsn_secret_key: str) -> str:
    """Resolve a DSN from the env var named by `dsn_secret_key`. Secrets live in
    env only (operations §2). The well-known read-only key falls back to the
    configured `pg_dsn_readonly` when unset."""
    value = os.environ.get(dsn_secret_key)
    if value:
        return value
    if dsn_secret_key == _RO_DSN_KEY:
        return get_settings().pg_dsn_readonly
    raise KeyError(f"DSN secret not set in env: {dsn_secret_key}")


@lru_cache
def _ro_engine_for(dsn: str) -> Engine:
    return create_engine(
        dsn,
        pool_pre_ping=True,
        future=True,
        execution_options={"postgresql_readonly": True},
    )


def introspect_postgres(dsn: str) -> dict[str, dict[str, dict[str, str]]]:
    """Read `information_schema.columns` (schema `public`) over a READ-ONLY
    connection. Returns `{table: {"columns": {col: data_type}}}`."""
    stmt = text(
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' "
        "ORDER BY table_name, ordinal_position"
    )
    tables: dict[str, dict[str, dict[str, str]]] = {}
    engine = _ro_engine_for(dsn)
    with engine.connect() as conn:
        for table_name, column_name, data_type in conn.execute(stmt):
            tables.setdefault(table_name, {"columns": {}})["columns"][column_name] = data_type
    return tables
