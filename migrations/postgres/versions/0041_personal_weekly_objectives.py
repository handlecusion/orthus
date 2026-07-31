"""personal weekly objectives

Revision ID: 0041_personal_weekly_objectives
Revises: 0040_weekly_meeting_aggregate
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision = "0041_personal_weekly_objectives"
down_revision = "0040_weekly_meeting_aggregate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE personal_weekly_objectives (
          objective_id UUID PRIMARY KEY,
          workspace_id UUID NOT NULL,
          week_start DATE NOT NULL,
          title TEXT NOT NULL,
          project_id UUID,
          day_allocations JSONB NOT NULL DEFAULT '[]',
          completed BOOLEAN NOT NULL DEFAULT false,
          order_index INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT fk_personal_weekly_objectives_workspace
            FOREIGN KEY (workspace_id) REFERENCES personal_board_workspaces(workspace_id)
            ON DELETE CASCADE,
          CONSTRAINT fk_personal_weekly_objectives_project
            FOREIGN KEY (project_id) REFERENCES personal_board_projects(project_id)
            ON DELETE SET NULL
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_personal_weekly_objectives_ws_week
          ON personal_weekly_objectives(workspace_id, week_start, order_index);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_personal_weekly_objectives_ws_week;")
    op.execute("DROP TABLE IF EXISTS personal_weekly_objectives;")
