"""team calendar events: location

Revision ID: 0029_calendar_event_location
Revises: 0028_team_member_bank_account
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0029_calendar_event_location"
down_revision = "0028_team_member_bank_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events ADD COLUMN location TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS location;")
