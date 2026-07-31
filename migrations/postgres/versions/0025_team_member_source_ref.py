"""team_members source ref for notion sync dedupe

Revision ID: 0025_team_member_source_ref
Revises: 0024_calendar_event_time
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0025_team_member_source_ref"
down_revision = "0024_calendar_event_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE team_members ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';")
    op.execute("ALTER TABLE team_members ADD COLUMN source_ref TEXT;")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_team_members_source_ref
          ON team_members(node_id, source_ref)
          WHERE source_ref IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_team_members_source_ref;")
    op.execute("ALTER TABLE team_members DROP COLUMN IF EXISTS source_ref;")
    op.execute("ALTER TABLE team_members DROP COLUMN IF EXISTS source;")
