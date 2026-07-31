"""P8.2 collector tokens + command queue

Revision ID: 0044_collector_tokens_commands
Revises: 0043_objective_per_day_detail
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op

revision = "0044_collector_tokens_commands"
down_revision = "0043_objective_per_day_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE collector_tokens (
            token_id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            node_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            token_hash TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at TIMESTAMPTZ NULL,
            revoked_at TIMESTAMPTZ NULL
        );
        """
    )
    op.execute("CREATE INDEX idx_collector_tokens_owner ON collector_tokens (node_id, user_id);")
    op.execute(
        """
        CREATE TABLE collector_commands (
            command_id UUID PRIMARY KEY,
            node_id TEXT NOT NULL,
            user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('connector_sync', 'raw_repush', 'file_fetch')),
            payload JSONB NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'claimed', 'done', 'failed')),
            result JSONB NULL,
            created_by UUID NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            claimed_at TIMESTAMPTZ NULL,
            completed_at TIMESTAMPTZ NULL
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_collector_commands_poll "
        "ON collector_commands (node_id, user_id, status, created_at);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS collector_commands;")
    op.execute("DROP TABLE IF EXISTS collector_tokens;")
