"""personal workspace board

Revision ID: 0013_personal_board
Revises: 0012_document_canonical_source
Create Date: 2026-06-04
"""

from __future__ import annotations

from alembic import op

revision = "0013_personal_board"
down_revision = "0012_document_canonical_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE personal_board_workspaces (
          workspace_id UUID PRIMARY KEY,
          user_id UUID NOT NULL,
          node_id TEXT NOT NULL,
          name TEXT NOT NULL,
          timezone TEXT NOT NULL DEFAULT 'Asia/Seoul',
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (user_id, node_id)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE personal_board_projects (
          project_id UUID PRIMARY KEY,
          workspace_id UUID NOT NULL,
          name TEXT NOT NULL,
          color TEXT,
          archived_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (workspace_id, name)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE personal_board_backlog_buckets (
          bucket_id UUID PRIMARY KEY,
          workspace_id UUID NOT NULL,
          key TEXT NOT NULL,
          label TEXT NOT NULL,
          badge TEXT NOT NULL,
          color TEXT NOT NULL,
          order_index INTEGER NOT NULL,
          collapsed BOOLEAN NOT NULL DEFAULT false,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (workspace_id, key)
        );
        """
    )
    op.execute(
        """
        CREATE TABLE personal_board_tasks (
          task_id UUID PRIMARY KEY,
          workspace_id UUID NOT NULL,
          user_id UUID NOT NULL,
          project_id UUID,
          backlog_bucket_id UUID,
          title TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'done', 'archived')),
          priority TEXT NOT NULL DEFAULT 'normal'
            CHECK (priority IN ('urgent', 'priority', 'normal', 'low')),
          scheduled_date DATE,
          scheduled_time TIME,
          order_index INTEGER NOT NULL DEFAULT 0,
          source_kind TEXT NOT NULL DEFAULT 'manual'
            CHECK (source_kind IN ('manual', 'github', 'gmail', 'notion', 'ai_session', 'calendar')),
          source_label TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CHECK (
            (scheduled_date IS NOT NULL AND backlog_bucket_id IS NULL)
            OR (scheduled_date IS NULL AND backlog_bucket_id IS NOT NULL)
          )
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_personal_board_tasks_date_order
          ON personal_board_tasks(workspace_id, scheduled_date, order_index)
          WHERE scheduled_date IS NOT NULL AND status <> 'archived';
        """
    )
    op.execute(
        """
        CREATE INDEX idx_personal_board_tasks_backlog_order
          ON personal_board_tasks(workspace_id, backlog_bucket_id, order_index)
          WHERE backlog_bucket_id IS NOT NULL AND status <> 'archived';
        """
    )
    op.execute(
        """
        CREATE TABLE personal_board_subtasks (
          subtask_id UUID PRIMARY KEY,
          task_id UUID NOT NULL,
          title TEXT NOT NULL,
          completed BOOLEAN NOT NULL DEFAULT false,
          order_index INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_personal_board_subtasks_task_order
          ON personal_board_subtasks(task_id, order_index);
        """
    )
    op.execute(
        """
        CREATE TABLE personal_board_fixed_events (
          event_id UUID PRIMARY KEY,
          workspace_id UUID NOT NULL,
          user_id UUID NOT NULL,
          project_id UUID,
          title TEXT NOT NULL,
          starts_at TIMESTAMPTZ NOT NULL,
          ends_at TIMESTAMPTZ NOT NULL,
          source_kind TEXT NOT NULL DEFAULT 'manual'
            CHECK (source_kind IN ('manual', 'calendar', 'ai_session')),
          source_label TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CHECK (ends_at > starts_at)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_personal_board_fixed_events_time
          ON personal_board_fixed_events(workspace_id, starts_at, ends_at);
        """
    )
    op.execute(
        """
        CREATE TABLE personal_board_notes (
          note_id UUID PRIMARY KEY,
          workspace_id UUID NOT NULL,
          user_id UUID NOT NULL,
          note_date DATE NOT NULL,
          kind TEXT NOT NULL DEFAULT 'note'
            CHECK (kind IN ('note', 'incident', 'decision')),
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          order_index INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_personal_board_notes_date_order
          ON personal_board_notes(workspace_id, note_date, order_index);
        """
    )
    op.execute(
        """
        CREATE TABLE personal_board_preferences (
          workspace_id UUID NOT NULL,
          user_id UUID NOT NULL,
          selected_date DATE,
          right_panel TEXT NOT NULL DEFAULT 'backlog'
            CHECK (right_panel IN ('backlog', 'notes', 'hidden')),
          filter_mode TEXT NOT NULL DEFAULT 'all'
            CHECK (filter_mode IN ('all', 'open', 'done', 'high_priority')),
          sort_mode TEXT NOT NULL DEFAULT 'manual'
            CHECK (sort_mode IN ('manual', 'priority')),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (workspace_id, user_id)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS personal_board_preferences;")
    op.execute("DROP INDEX IF EXISTS idx_personal_board_notes_date_order;")
    op.execute("DROP TABLE IF EXISTS personal_board_notes;")
    op.execute("DROP INDEX IF EXISTS idx_personal_board_fixed_events_time;")
    op.execute("DROP TABLE IF EXISTS personal_board_fixed_events;")
    op.execute("DROP INDEX IF EXISTS idx_personal_board_subtasks_task_order;")
    op.execute("DROP TABLE IF EXISTS personal_board_subtasks;")
    op.execute("DROP INDEX IF EXISTS idx_personal_board_tasks_backlog_order;")
    op.execute("DROP INDEX IF EXISTS idx_personal_board_tasks_date_order;")
    op.execute("DROP TABLE IF EXISTS personal_board_tasks;")
    op.execute("DROP TABLE IF EXISTS personal_board_backlog_buckets;")
    op.execute("DROP TABLE IF EXISTS personal_board_projects;")
    op.execute("DROP TABLE IF EXISTS personal_board_workspaces;")
