"""agent_task_threads — native runner session mapping for delegated chat turns

Agent-task delegation used to dispatch every turn as a fresh runner invocation.
This stores a central logical thread per owner chat + assignee + runner + mode +
cwd, plus the runner-native session id when known, so later turns can resume the
same Claude/Codex/Hermes conversation.

Revision ID: 0075_agent_task_threads
Revises: 0074_agent_chat_sessions
Create Date: 2026-06-22
"""

from __future__ import annotations

from alembic import op

revision = "0075_agent_task_threads"
down_revision = "0074_agent_chat_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_task_threads (
            thread_id UUID PRIMARY KEY,
            node_id TEXT NOT NULL,
            owner_id UUID NOT NULL,
            chat_session_id UUID NOT NULL,
            assignee_user_id UUID NOT NULL,
            runner TEXT NOT NULL,
            mode TEXT NOT NULL,
            cwd TEXT NOT NULL DEFAULT '',
            runner_session_id TEXT,
            runner_session_ready BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_task_threads_identity "
        "ON agent_task_threads "
        "(node_id, owner_id, chat_session_id, assignee_user_id, runner, mode, cwd);"
    )
    op.execute(
        "CREATE INDEX idx_agent_task_threads_chat "
        "ON agent_task_threads (owner_id, node_id, chat_session_id);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE agent_task_threads;")
