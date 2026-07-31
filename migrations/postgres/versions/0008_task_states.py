"""task_states: local-only overlay for kanban status on notion_rows.

The board reads COALESCE(task_states.status, properties->>'상태') so local drag-
and-drop persists without writing back to Notion. Notion stays authoritative; the
overlay row can be discarded once Notion write-back lands.

# TODO(P2.4): when Notion write-back lands, mirror status to Notion and consider
# deleting the task_states overlay row on round-trip.

Revision ID: 0008_task_states
Revises: 0007_project_overrides
Create Date: 2026-05-28
"""

from alembic import op

revision = "0008_task_states"
down_revision = "0007_project_overrides"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Local kanban status overlay. Notion stays authoritative; this row wins via
    # COALESCE(task_states.status, properties->>'상태') in the board query.
    op.execute(
        "CREATE TABLE task_states ("
        "row_id UUID PRIMARY KEY REFERENCES notion_rows(row_id) ON DELETE CASCADE, "
        "status TEXT NOT NULL, "
        "updated_by UUID NOT NULL REFERENCES users(user_id), "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "schema_version INT NOT NULL DEFAULT 1"
        ");"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS task_states;")
