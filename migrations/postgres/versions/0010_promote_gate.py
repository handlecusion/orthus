"""personal to central promote gate.

Revision ID: 0010_promote_gate
Revises: 0009_connector_substrate
Create Date: 2026-05-31
"""

from alembic import op

revision = "0010_promote_gate"
down_revision = "0009_connector_substrate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE promote_staging (
          stage_id             UUID PRIMARY KEY,
          source_node_id       TEXT NOT NULL,
          source_doc_id        UUID NOT NULL,
          source_owner_id      UUID,
          source_scope         TEXT NOT NULL CHECK (source_scope = 'personal'),
          source_title         TEXT NOT NULL,
          sanitized_title      TEXT NOT NULL,
          sanitized_markdown   TEXT NOT NULL,
          source_meta          JSONB NOT NULL DEFAULT '{}'::jsonb,
          status               TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','approved','rejected')),
          created_by           UUID NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
          approved_by          UUID REFERENCES users(user_id) ON DELETE RESTRICT,
          promoted_doc_id      UUID REFERENCES documents(doc_id) ON DELETE SET NULL,
          created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
          decided_at           TIMESTAMPTZ
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_promote_staging_status_created
          ON promote_staging(status, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX idx_promote_staging_source
          ON promote_staging(source_node_id, source_doc_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_promote_staging_source;")
    op.execute("DROP INDEX IF EXISTS idx_promote_staging_status_created;")
    op.execute("DROP TABLE IF EXISTS promote_staging;")
