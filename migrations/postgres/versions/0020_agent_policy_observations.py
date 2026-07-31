"""agent policy memory observations

Revision ID: 0020_agent_policy_observations
Revises: 0019_agent_work_decisions
Create Date: 2026-06-06
"""

from __future__ import annotations

from alembic import op

revision = "0020_agent_policy_observations"
down_revision = "0019_agent_work_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_policy_observations (
          observation_id    UUID PRIMARY KEY,
          node_id           TEXT NOT NULL,
          node_kind         TEXT NOT NULL CHECK (node_kind IN ('company','personal')),
          owner_id          UUID,
          work_id           UUID NOT NULL REFERENCES agent_work_items(work_id) ON DELETE CASCADE,
          decision_id       UUID NOT NULL REFERENCES agent_work_decisions(decision_id) ON DELETE CASCADE,
          reviewer_id       UUID NOT NULL,
          source_kind       TEXT NOT NULL,
          source_ref_id     TEXT NOT NULL,
          action_family     TEXT NOT NULL,
          policy_outcome    TEXT,
          reason_codes      JSONB NOT NULL DEFAULT '[]'::jsonb,
          reviewer_decision TEXT NOT NULL CHECK (
            reviewer_decision IN ('approve', 'dismiss', 'request_more_data')
          ),
          from_state        TEXT NOT NULL,
          to_state          TEXT NOT NULL,
          note_present      BOOLEAN NOT NULL DEFAULT false,
          bucket_key        TEXT NOT NULL,
          meta              JSONB NOT NULL DEFAULT '{}'::jsonb,
          observed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (decision_id)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_policy_obs_node_bucket
          ON agent_policy_observations(node_id, bucket_key, observed_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_policy_obs_node_observed
          ON agent_policy_observations(node_id, observed_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_agent_policy_obs_node_observed;")
    op.execute("DROP INDEX IF EXISTS idx_agent_policy_obs_node_bucket;")
    op.execute("DROP TABLE IF EXISTS agent_policy_observations;")
