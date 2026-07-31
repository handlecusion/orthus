"""SQLAlchemy engines + session factories.

Two engines on purpose:
- `engine` / `Session`         — full role. App writes (documents, corpus, audit, query_runs).
- `ro_engine` / `ROSession`    — read-only role. The 비서 executes compiled SELECTs ONLY here.
  This is the DB-level half of the double defense (operations §6, prompt §6).
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from orthus.settings import get_settings


def _pool_kwargs() -> dict[str, object]:
    s = get_settings()
    return {
        "pool_size": s.pg_pool_size,
        "max_overflow": s.pg_max_overflow,
        "pool_timeout": s.pg_pool_timeout,
        "pool_recycle": s.pg_pool_recycle,
    }


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().pg_dsn, pool_pre_ping=True, future=True, **_pool_kwargs())


@lru_cache
def get_ro_engine() -> Engine:
    # The role itself enforces read-only (default_transaction_read_only=on),
    # but we also flag the engine so any accidental write fails loudly.
    return create_engine(
        get_settings().pg_dsn_readonly,
        pool_pre_ping=True,
        future=True,
        execution_options={"postgresql_readonly": True},
        **_pool_kwargs(),
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@lru_cache
def _ro_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_ro_engine(), expire_on_commit=False, future=True)


def session() -> Session:
    """A new full-privilege session (caller manages commit/close)."""
    return _session_factory()()


def ro_session() -> Session:
    """A new read-only session for 비서 query execution."""
    return _ro_session_factory()()
