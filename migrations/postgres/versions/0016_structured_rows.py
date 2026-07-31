"""generic structured rows for non-Notion sources

Revision ID: 0016_structured_rows
Revises: 0015_personal_board_ui_state
Create Date: 2026-06-04
"""

from __future__ import annotations

from alembic import op

revision = "0016_structured_rows"
down_revision = "0015_personal_board_ui_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS structured_rows (
          row_id             UUID PRIMARY KEY,
          source             TEXT NOT NULL,
          record_type        TEXT NOT NULL,
          source_doc_id      UUID REFERENCES documents(doc_id) ON DELETE CASCADE,
          source_external_id TEXT,
          source_account_id  UUID,
          record_key         TEXT NOT NULL,
          properties         JSONB NOT NULL DEFAULT '{}'::jsonb,
          evidence           TEXT,
          confidence         TEXT NOT NULL DEFAULT 'medium',
          scope              TEXT NOT NULL DEFAULT 'company' CHECK (scope IN ('company','personal')),
          project            TEXT NOT NULL DEFAULT 'atlas',
          owner_id           UUID,
          user_id            UUID NOT NULL,
          updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_structured_rows_source_doc_type_key
          ON structured_rows(source_doc_id, record_type, record_key)
          WHERE source_doc_id IS NOT NULL;
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_structured_rows_source_type_scope "
        "ON structured_rows(source, record_type, scope);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_structured_rows_properties "
        "ON structured_rows USING gin (properties);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_structured_rows_project_scope "
        "ON structured_rows(project, scope);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_structured_rows_project_scope;")
    op.execute("DROP INDEX IF EXISTS idx_structured_rows_properties;")
    op.execute("DROP INDEX IF EXISTS idx_structured_rows_source_type_scope;")
    op.execute("DROP INDEX IF EXISTS uq_structured_rows_source_doc_type_key;")
    op.execute("DROP TABLE IF EXISTS structured_rows;")
