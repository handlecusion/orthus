"""personal board folders and integrations

Revision ID: 0015_personal_board_ui_state
Revises: 0014_personal_board_fks
Create Date: 2026-06-04
"""

from __future__ import annotations

from alembic import op

revision = "0015_personal_board_ui_state"
down_revision = "0014_personal_board_fks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS personal_board_folders (
          folder_id UUID PRIMARY KEY,
          workspace_id UUID NOT NULL,
          name TEXT NOT NULL,
          kind TEXT NOT NULL CHECK (kind IN ('system', 'custom')),
          order_index INTEGER NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (workspace_id, name)
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_personal_board_folders_workspace'
          ) THEN
            ALTER TABLE personal_board_folders
              ADD CONSTRAINT fk_personal_board_folders_workspace
              FOREIGN KEY (workspace_id) REFERENCES personal_board_workspaces(workspace_id)
              ON DELETE CASCADE;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS personal_board_integrations (
          integration_id UUID PRIMARY KEY,
          workspace_id UUID NOT NULL,
          kind TEXT NOT NULL,
          label TEXT NOT NULL,
          enabled BOOLEAN NOT NULL DEFAULT false,
          has_notification BOOLEAN NOT NULL DEFAULT false,
          order_index INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (workspace_id, kind)
        );
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fk_personal_board_integrations_workspace'
          ) THEN
            ALTER TABLE personal_board_integrations
              ADD CONSTRAINT fk_personal_board_integrations_workspace
              FOREIGN KEY (workspace_id) REFERENCES personal_board_workspaces(workspace_id)
              ON DELETE CASCADE;
          END IF;
        END $$;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_preferences
          ADD COLUMN IF NOT EXISTS active_integration TEXT;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_preferences
          DROP CONSTRAINT IF EXISTS personal_board_preferences_right_panel_check;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_preferences
          ADD CONSTRAINT personal_board_preferences_right_panel_check
          CHECK (right_panel IN ('backlog', 'integrations', 'hidden'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE personal_board_preferences
          DROP CONSTRAINT IF EXISTS personal_board_preferences_right_panel_check;
        """
    )
    op.execute(
        """
        ALTER TABLE personal_board_preferences
          ADD CONSTRAINT personal_board_preferences_right_panel_check
          CHECK (right_panel IN ('backlog', 'notes', 'hidden'));
        """
    )
    op.execute("ALTER TABLE personal_board_preferences DROP COLUMN IF EXISTS active_integration;")
    op.execute(
        "ALTER TABLE personal_board_integrations DROP CONSTRAINT IF EXISTS fk_personal_board_integrations_workspace;"
    )
    op.execute("DROP TABLE IF EXISTS personal_board_integrations;")
    op.execute(
        "ALTER TABLE personal_board_folders DROP CONSTRAINT IF EXISTS fk_personal_board_folders_workspace;"
    )
    op.execute("DROP TABLE IF EXISTS personal_board_folders;")
