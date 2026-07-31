"""collector agent_workspaces — daemon-reported agent run folders

A collector daemon that runs delegated `agent_task` commands reports the
directories it actually used (the resolved `cwd`s) plus its configured default
workspace. Central stores that report per-token so the owner's Agent-chat
delegation UI can offer their own recent run folders as cwd suggestions.

additive + nullable: a NULL `agent_workspaces` means the daemon has not reported
any folders yet. No backfill — pre-existing rows stay NULL until the next status
report. The owner-folders read path is strictly owner-scoped (collector_tokens
filtered by user_id) and fail-closed behind `agent_task_enabled`.

Revision ID: 0073_collector_agent_workspaces
Revises: 0072_collector_device_id
Create Date: 2026-06-21
"""

from __future__ import annotations

from alembic import op

revision = "0073_collector_agent_workspaces"
down_revision = "0072_collector_device_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE collector_tokens ADD COLUMN agent_workspaces JSONB NULL;")


def downgrade() -> None:
    op.execute("ALTER TABLE collector_tokens DROP COLUMN agent_workspaces;")
