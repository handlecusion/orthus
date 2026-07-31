"""data gaps backlog (ask answer insufficiency detection)

Revision ID: 0017_data_gaps
Revises: 0016_structured_rows
Create Date: 2026-06-05
"""

from __future__ import annotations

from alembic import op

revision = "0017_data_gaps"
down_revision = "0016_structured_rows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS data_gaps (
          gap_id              UUID PRIMARY KEY,
          scope               TEXT NOT NULL DEFAULT 'company' CHECK (scope IN ('company','personal')),
          owner_id            UUID,
          node_id             TEXT NOT NULL,
          question_norm       TEXT NOT NULL,
          question            TEXT NOT NULL,
          reason              TEXT NOT NULL,
          top_score           DOUBLE PRECISION,
          suggested_target    TEXT,
          suggested_connector TEXT,
          suggested_fields    JSONB NOT NULL DEFAULT '[]'::jsonb,
          suggestion_status   TEXT NOT NULL DEFAULT 'pending',
          hit_count           INTEGER NOT NULL DEFAULT 1,
          status              TEXT NOT NULL DEFAULT 'open',
          source              TEXT NOT NULL DEFAULT 'auto',
          created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    # Dedup key: one open backlog row per (scope, owner, normalized question).
    # owner_id is NULL for company scope, so COALESCE to a sentinel for uniqueness.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_data_gaps_scope_owner_question
          ON data_gaps(scope, COALESCE(owner_id, '00000000-0000-0000-0000-000000000000'::uuid), question_norm);
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_data_gaps_scope_status "
        "ON data_gaps(scope, status, hit_count DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_data_gaps_scope_status;")
    op.execute("DROP INDEX IF EXISTS uq_data_gaps_scope_owner_question;")
    op.execute("DROP TABLE IF EXISTS data_gaps;")
