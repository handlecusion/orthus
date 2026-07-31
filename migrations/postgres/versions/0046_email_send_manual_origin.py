"""email_send_log manual origin

Revision ID: 0046_email_send_manual_origin
Revises: 0045_collector_tokens_scopes
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op

revision = "0046_email_send_manual_origin"
down_revision = "0045_collector_tokens_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # P6.3 manual mail sends have no agent-work row, so work_id must be nullable.
    op.execute("ALTER TABLE email_send_log ALTER COLUMN work_id DROP NOT NULL;")
    op.execute("ALTER TABLE email_send_log ADD COLUMN origin TEXT NOT NULL DEFAULT 'agent_work';")


def downgrade() -> None:
    op.execute("ALTER TABLE email_send_log DROP COLUMN origin;")
    op.execute("ALTER TABLE email_send_log ALTER COLUMN work_id SET NOT NULL;")
