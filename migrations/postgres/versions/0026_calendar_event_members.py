"""team calendar events: multiple members

Revision ID: 0026_calendar_event_members
Revises: 0025_team_member_source_ref
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0026_calendar_event_members"
down_revision = "0025_team_member_source_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE team_calendar_events ADD COLUMN member_ids JSONB NOT NULL DEFAULT '[]'::jsonb;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS member_ids;")
