"""agent work reviewer decisions

Revision ID: 0019_agent_work_decisions
Revises: 0018_agent_work_items
Create Date: 2026-06-05
"""

from __future__ import annotations

from alembic import op

revision = "0019_agent_work_decisions"
down_revision = "0018_agent_work_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_work_decisions (
          decision_id    UUID PRIMARY KEY,
          work_id        UUID NOT NULL REFERENCES agent_work_items(work_id) ON DELETE CASCADE,
          node_id        TEXT NOT NULL,
          reviewer_id    UUID NOT NULL,
          decision       TEXT NOT NULL CHECK (
            decision IN ('approve', 'dismiss', 'request_more_data')
          ),
          note           TEXT,
          from_state     TEXT NOT NULL CHECK (
            from_state IN (
              'pending',
              'classified',
              'auto_execute',
              'draft_for_review',
              'request_more_data',
              'rejected',
              'resolved',
              'dismissed'
            )
          ),
          to_state       TEXT NOT NULL CHECK (
            to_state IN (
              'pending',
              'classified',
              'auto_execute',
              'draft_for_review',
              'request_more_data',
              'rejected',
              'resolved',
              'dismissed'
            )
          ),
          correlation_id UUID,
          node_run_id    UUID,
          decided_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (work_id)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_work_decisions_node_decided
          ON agent_work_decisions(node_id, decided_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_work_decisions_node_decided;")
    op.execute("DROP TABLE IF EXISTS agent_work_decisions;")
