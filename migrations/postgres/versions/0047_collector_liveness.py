"""collector liveness and scheduler heartbeat

Revision ID: 0047_collector_liveness
Revises: 0046_email_send_manual_origin
Create Date: 2026-06-12
"""

from __future__ import annotations

from alembic import op

revision = "0047_collector_liveness"
down_revision = "0046_email_send_manual_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE collector_tokens ADD COLUMN last_polled_at TIMESTAMPTZ;")
    op.execute("ALTER TABLE collector_tokens ADD COLUMN last_status_at TIMESTAMPTZ;")
    op.execute("ALTER TABLE collector_tokens ADD COLUMN scheduler_installed BOOLEAN;")
    op.execute("ALTER TABLE collector_tokens ADD COLUMN scheduler_loaded BOOLEAN;")
    op.execute("ALTER TABLE collector_tokens ADD COLUMN scheduler_interval_seconds INTEGER;")
    op.execute("ALTER TABLE collector_tokens ADD COLUMN last_status_error TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE collector_tokens DROP COLUMN last_status_error;")
    op.execute("ALTER TABLE collector_tokens DROP COLUMN scheduler_interval_seconds;")
    op.execute("ALTER TABLE collector_tokens DROP COLUMN scheduler_loaded;")
    op.execute("ALTER TABLE collector_tokens DROP COLUMN scheduler_installed;")
    op.execute("ALTER TABLE collector_tokens DROP COLUMN last_status_at;")
    op.execute("ALTER TABLE collector_tokens DROP COLUMN last_polled_at;")
