"""structured(PG) backend: notion_rows JSONB row store; drop external data_sources

The 비서 NL→SQL gate is repurposed (P2.2a, docs/architecture-v2.md §1/§5) to query
our own Postgres JSONB row store of Notion DB rows (`notion_rows`) instead of
external DSNs. So the external-DSN registry (`data_sources`) is dropped and
`query_runs.source_id` becomes a nullable audit column (structured runs set NULL).

Revision ID: 0005_structured_store
Revises: 0004_tenant_scope
Create Date: 2026-05-27
"""

from alembic import op

revision = "0005_structured_store"
down_revision = "0004_tenant_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # notion_rows — generic JSONB row store; one row per Notion DB row.
    op.execute(
        """
        CREATE TABLE notion_rows (
          row_id      UUID PRIMARY KEY,                 -- deterministic Notion row page id
          db_id       TEXT NOT NULL,
          db_name     TEXT NOT NULL,
          properties  JSONB NOT NULL DEFAULT '{}'::jsonb,
          scope       TEXT NOT NULL DEFAULT 'company' CHECK (scope IN ('company','personal')),
          owner_id    UUID,
          user_id     UUID NOT NULL,
          updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_notion_rows_properties ON notion_rows USING gin (properties);")
    op.execute("CREATE INDEX idx_notion_rows_dbname_scope ON notion_rows(db_name, scope);")
    op.execute("CREATE INDEX idx_notion_rows_user_scope ON notion_rows(user_id, scope);")

    # Remove the external-DSN framing. query_runs keeps source_id for audit, but
    # structured runs have no external source, so drop the FK + NOT NULL.
    op.execute("ALTER TABLE query_runs DROP CONSTRAINT IF EXISTS query_runs_source_id_fkey;")
    op.execute("ALTER TABLE query_runs ALTER COLUMN source_id DROP NOT NULL;")
    op.execute("DROP TABLE IF EXISTS data_sources;")


def downgrade() -> None:
    # Recreate data_sources (mirror 0002's definition) and restore the FK/NOT NULL.
    op.execute(
        """
        CREATE TABLE data_sources (
          source_id       UUID PRIMARY KEY,
          name            TEXT NOT NULL UNIQUE,
          dialect         TEXT NOT NULL,
          dsn_secret_key  TEXT NOT NULL,
          catalog         JSONB NOT NULL,
          read_only       BOOLEAN NOT NULL DEFAULT true,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    # source_id must be non-null to restore the FK; structured runs wrote NULL, so
    # this downgrade is only safe when no structured-only runs exist (test roundtrip
    # runs on an empty query_runs).
    op.execute("ALTER TABLE query_runs ALTER COLUMN source_id SET NOT NULL;")
    op.execute(
        "ALTER TABLE query_runs ADD CONSTRAINT query_runs_source_id_fkey "
        "FOREIGN KEY (source_id) REFERENCES data_sources(source_id);"
    )

    op.execute("DROP TABLE IF EXISTS notion_rows;")
