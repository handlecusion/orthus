"""team calendar events: project tag

Revision ID: 0027_calendar_event_project
Revises: 0026_calendar_event_members
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0027_calendar_event_project"
down_revision = "0026_calendar_event_members"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE team_calendar_events ADD COLUMN project_id UUID "
        "REFERENCES dashboard_projects(project_id) ON DELETE SET NULL;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS project_id;")
