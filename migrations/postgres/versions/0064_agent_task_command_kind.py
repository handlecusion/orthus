"""agent_task collector command kind

Revision ID: 0064_agent_task_command_kind
Revises: 0063_merge_kg_owner_scope_infra
Create Date: 2026-06-19
"""

from __future__ import annotations

from alembic import op

revision = "0064_agent_task_command_kind"
down_revision = "0063_merge_kg_owner_scope_infra"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE collector_commands DROP CONSTRAINT collector_commands_kind_check;")
    op.execute(
        "ALTER TABLE collector_commands ADD CONSTRAINT collector_commands_kind_check "
        "CHECK (kind IN ('connector_sync', 'raw_repush', 'file_fetch', 'agent_task'));"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE collector_commands DROP CONSTRAINT collector_commands_kind_check;")
    op.execute(
        "ALTER TABLE collector_commands ADD CONSTRAINT collector_commands_kind_check "
        "CHECK (kind IN ('connector_sync', 'raw_repush', 'file_fetch'));"
    )
