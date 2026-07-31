"""email send log

Revision ID: 0021_email_send_log
Revises: 0020_agent_policy_observations
Create Date: 2026-06-06
"""

from __future__ import annotations

from alembic import op

revision = "0021_email_send_log"
down_revision = "0020_agent_policy_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS email_send_log (
          send_id        UUID PRIMARY KEY,
          work_id        UUID NOT NULL REFERENCES agent_work_items(work_id) ON DELETE CASCADE,
          node_id        TEXT NOT NULL,
          owner_id       UUID NOT NULL,
          recipient_hash TEXT NOT NULL,
          subject_hash   TEXT NOT NULL,
          body_hash      TEXT NOT NULL,
          sender_kind    TEXT NOT NULL,
          status         TEXT NOT NULL CHECK (status IN ('sent', 'failed', 'rate_limited')),
          sent_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
          correlation_id UUID,
          error_message  TEXT
        );
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_email_send_log_node_recipient
          ON email_send_log(node_id, owner_id, recipient_hash, sent_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_email_send_log_work
          ON email_send_log(work_id, status);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_email_send_log_work;")
    op.execute("DROP INDEX IF EXISTS idx_email_send_log_node_recipient;")
    op.execute("DROP TABLE IF EXISTS email_send_log;")
