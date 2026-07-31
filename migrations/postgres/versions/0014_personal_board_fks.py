"""personal board foreign keys

Revision ID: 0014_personal_board_fks
Revises: 0013_personal_board
Create Date: 2026-06-04
"""

from __future__ import annotations

from alembic import op

revision = "0014_personal_board_fks"
down_revision = "0013_personal_board"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE personal_board_workspaces
          ADD CONSTRAINT fk_personal_board_workspaces_user
          FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_projects
          ADD CONSTRAINT fk_personal_board_projects_workspace
          FOREIGN KEY (workspace_id) REFERENCES personal_board_workspaces(workspace_id)
          ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_backlog_buckets
          ADD CONSTRAINT fk_personal_board_backlog_buckets_workspace
          FOREIGN KEY (workspace_id) REFERENCES personal_board_workspaces(workspace_id)
          ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_tasks
          ADD CONSTRAINT fk_personal_board_tasks_workspace
          FOREIGN KEY (workspace_id) REFERENCES personal_board_workspaces(workspace_id)
          ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_tasks
          ADD CONSTRAINT fk_personal_board_tasks_user
          FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_tasks
          ADD CONSTRAINT fk_personal_board_tasks_project
          FOREIGN KEY (project_id) REFERENCES personal_board_projects(project_id)
          ON DELETE SET NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_tasks
          ADD CONSTRAINT fk_personal_board_tasks_backlog_bucket
          FOREIGN KEY (backlog_bucket_id) REFERENCES personal_board_backlog_buckets(bucket_id);
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_subtasks
          ADD CONSTRAINT fk_personal_board_subtasks_task
          FOREIGN KEY (task_id) REFERENCES personal_board_tasks(task_id) ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_fixed_events
          ADD CONSTRAINT fk_personal_board_fixed_events_workspace
          FOREIGN KEY (workspace_id) REFERENCES personal_board_workspaces(workspace_id)
          ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_fixed_events
          ADD CONSTRAINT fk_personal_board_fixed_events_user
          FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_fixed_events
          ADD CONSTRAINT fk_personal_board_fixed_events_project
          FOREIGN KEY (project_id) REFERENCES personal_board_projects(project_id)
          ON DELETE SET NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_notes
          ADD CONSTRAINT fk_personal_board_notes_workspace
          FOREIGN KEY (workspace_id) REFERENCES personal_board_workspaces(workspace_id)
          ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_notes
          ADD CONSTRAINT fk_personal_board_notes_user
          FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_preferences
          ADD CONSTRAINT fk_personal_board_preferences_workspace
          FOREIGN KEY (workspace_id) REFERENCES personal_board_workspaces(workspace_id)
          ON DELETE CASCADE;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_preferences
          ADD CONSTRAINT fk_personal_board_preferences_user
          FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE personal_board_preferences DROP CONSTRAINT IF EXISTS fk_personal_board_preferences_user;"
    )
    op.execute(
        "ALTER TABLE personal_board_preferences DROP CONSTRAINT IF EXISTS fk_personal_board_preferences_workspace;"
    )
    op.execute(
        "ALTER TABLE personal_board_notes DROP CONSTRAINT IF EXISTS fk_personal_board_notes_user;"
    )
    op.execute(
        "ALTER TABLE personal_board_notes DROP CONSTRAINT IF EXISTS fk_personal_board_notes_workspace;"
    )
    op.execute(
        "ALTER TABLE personal_board_fixed_events DROP CONSTRAINT IF EXISTS fk_personal_board_fixed_events_project;"
    )
    op.execute(
        "ALTER TABLE personal_board_fixed_events DROP CONSTRAINT IF EXISTS fk_personal_board_fixed_events_user;"
    )
    op.execute(
        "ALTER TABLE personal_board_fixed_events DROP CONSTRAINT IF EXISTS fk_personal_board_fixed_events_workspace;"
    )
    op.execute(
        "ALTER TABLE personal_board_subtasks DROP CONSTRAINT IF EXISTS fk_personal_board_subtasks_task;"
    )
    op.execute(
        "ALTER TABLE personal_board_tasks DROP CONSTRAINT IF EXISTS fk_personal_board_tasks_backlog_bucket;"
    )
    op.execute(
        "ALTER TABLE personal_board_tasks DROP CONSTRAINT IF EXISTS fk_personal_board_tasks_project;"
    )
    op.execute(
        "ALTER TABLE personal_board_tasks DROP CONSTRAINT IF EXISTS fk_personal_board_tasks_user;"
    )
    op.execute(
        "ALTER TABLE personal_board_tasks DROP CONSTRAINT IF EXISTS fk_personal_board_tasks_workspace;"
    )
    op.execute(
        "ALTER TABLE personal_board_backlog_buckets DROP CONSTRAINT IF EXISTS fk_personal_board_backlog_buckets_workspace;"
    )
    op.execute(
        "ALTER TABLE personal_board_projects DROP CONSTRAINT IF EXISTS fk_personal_board_projects_workspace;"
    )
    op.execute(
        "ALTER TABLE personal_board_workspaces DROP CONSTRAINT IF EXISTS fk_personal_board_workspaces_user;"
    )
