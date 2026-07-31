"""team calendar event start/end time

Revision ID: 0024_calendar_event_time
Revises: 0023_company_dashboard
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0024_calendar_event_time"
down_revision = "0023_company_dashboard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events ADD COLUMN start_time TIME;")
    op.execute("ALTER TABLE team_calendar_events ADD COLUMN end_time TIME;")


def downgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS end_time;")
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS start_time;")
