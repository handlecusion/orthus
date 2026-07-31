"""P8.7a collector token scopes

Revision ID: 0045_collector_tokens_scopes
Revises: 0044_collector_tokens_commands
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op

revision = "0045_collector_tokens_scopes"
down_revision = "0044_collector_tokens_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE collector_tokens "
        "ADD COLUMN scopes TEXT[] NOT NULL DEFAULT ARRAY['ingest']::TEXT[];"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE collector_tokens DROP COLUMN scopes;")
