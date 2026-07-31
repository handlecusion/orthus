"""project database file attachments

Store Notion-style files property bytes outside row JSONB while keeping row
props as the property-value layer. Forward-only successor to
0086_project_database_row_cover (no parallel head / merge migration).

Revision ID: 0087_project_database_files
Revises: 0086_project_database_row_cover
Create Date: 2026-06-30
"""

from __future__ import annotations

from alembic import op

revision = "0087_project_database_files"
down_revision = "0086_project_database_row_cover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE project_database_files (
          file_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          database_id UUID NOT NULL,
          row_id UUID NOT NULL,
          filename TEXT NOT NULL,
          mime_type TEXT,
          kind TEXT NOT NULL,
          size_bytes BIGINT NOT NULL,
          data BYTEA NOT NULL,
          created_by UUID,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_project_database_files_row "
        "ON project_database_files (node_id, database_id, row_id);"
    )
    op.execute(
        "CREATE INDEX ix_project_database_files_db ON project_database_files (database_id);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_database_files;")
