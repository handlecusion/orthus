"""Alembic environment. Manual migrations (no autogenerate) — the DDL is written
by hand to stay faithful to data-model.md / the P1 prompt (pgvector, CHECK constraints)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from orthus.settings import get_settings

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().pg_dsn)

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
