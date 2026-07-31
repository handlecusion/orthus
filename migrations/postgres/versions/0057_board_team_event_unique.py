"""dedupe mirrored team-event board tasks + enforce uniqueness

Race fix: sync_team_events_to_board did a check-then-insert without a unique
constraint, so two near-simultaneous syncs (bootstrap + list_days on page load)
could both insert the same team event as a board task. Observed: "모자이크 미팅"
duplicated ~6ms apart. This collapses existing duplicates (keep the task the
calendar event points to via source_task_id, else the earliest), then replaces
the non-unique partial index with a UNIQUE one on (workspace_id, team_event_id)
so the DB rejects concurrent double-inserts. The sync code switches to
ON CONFLICT DO NOTHING on this index.

Revision ID: 0057_board_team_event_unique
Revises: 0056_board_calendar_link
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op

revision = "0057_board_team_event_unique"
down_revision = "0056_board_calendar_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Collapse duplicate mirrors. For each (workspace_id, team_event_id) keep a
    #    single survivor: prefer the task the calendar event references
    #    (source_task_id), otherwise the earliest-created row. Delete the rest.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                t.task_id,
                row_number() OVER (
                    PARTITION BY t.workspace_id, t.team_event_id
                    ORDER BY
                        CASE WHEN e.source_task_id = t.task_id THEN 0 ELSE 1 END,
                        t.created_at,
                        t.task_id
                ) AS rn
            FROM personal_board_tasks t
            LEFT JOIN team_calendar_events e
                ON e.event_id = t.team_event_id
            WHERE t.team_event_id IS NOT NULL
        )
        DELETE FROM personal_board_tasks
        WHERE task_id IN (SELECT task_id FROM ranked WHERE rn > 1);
        """
    )

    # 2) Replace the non-unique partial index with a UNIQUE one scoped per
    #    workspace (one mirror of a given team event per personal board).
    op.execute("DROP INDEX IF EXISTS idx_personal_board_tasks_team_event;")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_personal_board_tasks_team_event "
        "ON personal_board_tasks (workspace_id, team_event_id) "
        "WHERE team_event_id IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_personal_board_tasks_team_event;")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_personal_board_tasks_team_event "
        "ON personal_board_tasks (team_event_id) WHERE team_event_id IS NOT NULL;"
    )
