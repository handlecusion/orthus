"""objective detail: note, subtasks, comments

Revision ID: 0042_objective_detail
Revises: 0041_personal_weekly_objectives
Create Date: 2026-06-09
"""

from __future__ import annotations

from alembic import op

revision = "0042_objective_detail"
down_revision = "0041_personal_weekly_objectives"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE personal_weekly_objectives ADD COLUMN note TEXT;")
    op.execute(
        """
        CREATE TABLE personal_objective_subtasks (
          subtask_id UUID PRIMARY KEY,
          objective_id UUID NOT NULL,
          title TEXT NOT NULL,
          completed BOOLEAN NOT NULL DEFAULT false,
          order_index INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT fk_personal_objective_subtasks_objective
            FOREIGN KEY (objective_id) REFERENCES personal_weekly_objectives(objective_id)
            ON DELETE CASCADE
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_personal_objective_subtasks_obj_order
          ON personal_objective_subtasks(objective_id, order_index);
        """
    )
    op.execute(
        """
        CREATE TABLE personal_objective_comments (
          comment_id UUID PRIMARY KEY,
          objective_id UUID NOT NULL,
          user_id UUID NOT NULL,
          body TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT fk_personal_objective_comments_objective
            FOREIGN KEY (objective_id) REFERENCES personal_weekly_objectives(objective_id)
            ON DELETE CASCADE
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_personal_objective_comments_obj_time
          ON personal_objective_comments(objective_id, created_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_personal_objective_comments_obj_time;")
    op.execute("DROP TABLE IF EXISTS personal_objective_comments;")
    op.execute("DROP INDEX IF EXISTS idx_personal_objective_subtasks_obj_order;")
    op.execute("DROP TABLE IF EXISTS personal_objective_subtasks;")
    op.execute("ALTER TABLE personal_weekly_objectives DROP COLUMN IF EXISTS note;")
