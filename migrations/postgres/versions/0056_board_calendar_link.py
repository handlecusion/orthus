"""personal board task <-> team calendar event bidirectional link

Revision ID: 0056_board_calendar_link
Revises: 0058_rekey_personal_board_sunday
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op

revision = "0056_board_calendar_link"
down_revision = "0058_rekey_personal_board_sunday"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # personal board task -> mirrored team calendar event
    op.execute(
        "ALTER TABLE personal_board_tasks ADD COLUMN IF NOT EXISTS team_event_id UUID;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_personal_board_tasks_team_event "
        "ON personal_board_tasks (team_event_id) WHERE team_event_id IS NOT NULL;"
    )
    # team calendar event -> originating personal board task
    op.execute(
        "ALTER TABLE team_calendar_events ADD COLUMN IF NOT EXISTS source_task_id UUID;"
    )
    op.execute("ALTER TABLE team_calendar_events ADD COLUMN IF NOT EXISTS source TEXT;")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_team_calendar_events_source_task "
        "ON team_calendar_events (source_task_id) WHERE source_task_id IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_team_calendar_events_source_task;")
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS source;")
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS source_task_id;")
    op.execute("DROP INDEX IF EXISTS idx_personal_board_tasks_team_event;")
    op.execute("ALTER TABLE personal_board_tasks DROP COLUMN IF EXISTS team_event_id;")
