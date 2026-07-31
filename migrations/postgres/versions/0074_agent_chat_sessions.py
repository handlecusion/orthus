"""agent_chat_sessions — DB-backed Agent-chat conversation threads

The Agent chat (`/agent-work` page) kept its messages in browser sessionStorage
as a flat list — lost on restart, and HISTORY counted messages not
conversations. This adds owner-scoped, restart-safe conversation sessions: each
session is a thread of messages. Pure storage (Slice A) — no policy gate, no LLM
calls, no central write paths.

additive + node-local + owner-scoped. Every read/write filters
(owner_id, node_id). The (owner_id, node_id, updated_at) index serves the
newest-first session list; (session_id, created_at) serves in-order message
fetch.

Revision ID: 0074_agent_chat_sessions
Revises: 0073_collector_agent_workspaces
Create Date: 2026-06-21
"""

from __future__ import annotations

from alembic import op

revision = "0074_agent_chat_sessions"
down_revision = "0073_collector_agent_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_chat_sessions (
            session_id UUID PRIMARY KEY,
            node_id TEXT NOT NULL,
            owner_id UUID NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_chat_sessions_owner_node_updated "
        "ON agent_chat_sessions (owner_id, node_id, updated_at);"
    )
    op.execute(
        """
        CREATE TABLE agent_chat_messages (
            message_id UUID PRIMARY KEY,
            session_id UUID NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            work_id UUID,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_chat_messages_session_created "
        "ON agent_chat_messages (session_id, created_at);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE agent_chat_messages;")
    op.execute("DROP TABLE agent_chat_sessions;")
