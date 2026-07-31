"""agent work items substrate

Revision ID: 0018_agent_work_items
Revises: 0017_data_gaps
Create Date: 2026-06-05
"""

from __future__ import annotations

from alembic import op

revision = "0018_agent_work_items"
down_revision = "0017_data_gaps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_work_items (
          work_id        UUID PRIMARY KEY,
          node_id        TEXT NOT NULL,
          node_kind      TEXT NOT NULL CHECK (node_kind IN ('company','personal')),
          owner_id       UUID,
          source_kind    TEXT NOT NULL,
          source_ref_id  TEXT NOT NULL,
          action_family  TEXT NOT NULL,
          title          TEXT NOT NULL,
          payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
          state          TEXT NOT NULL DEFAULT 'pending' CHECK (
            state IN (
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
          policy_outcome TEXT CHECK (
            policy_outcome IS NULL OR policy_outcome IN (
              'auto_execute',
              'draft_for_review',
              'request_more_data',
              'reject'
            )
          ),
          policy_reason  TEXT,
          reason_codes   JSONB NOT NULL DEFAULT '[]'::jsonb,
          evidence       JSONB NOT NULL DEFAULT '[]'::jsonb,
          correlation_id UUID,
          last_run_id    UUID,
          created_by     UUID,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_work_node_source
          ON agent_work_items(node_id, source_kind, source_ref_id);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_work_node_state
          ON agent_work_items(node_id, state, updated_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_work_node_outcome
          ON agent_work_items(node_id, policy_outcome, updated_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_work_node_outcome;")
    op.execute("DROP INDEX IF EXISTS idx_agent_work_node_state;")
    op.execute("DROP INDEX IF EXISTS uq_agent_work_node_source;")
    op.execute("DROP TABLE IF EXISTS agent_work_items;")
