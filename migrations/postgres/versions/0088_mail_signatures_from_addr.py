"""mail_signatures: per-from address signatures

Revision ID: 0088_mail_signatures_from_addr
Revises: 0087_project_database_files
Create Date: 2026-07-01
"""

from __future__ import annotations

from alembic import op

revision = "0088_mail_signatures_from_addr"
down_revision = "0087_project_database_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE mail_signatures ADD COLUMN IF NOT EXISTS from_addr TEXT NOT NULL DEFAULT '';")
    op.execute("DROP INDEX IF EXISTS uq_mail_signatures_owner;")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_signatures_owner_from "
        "ON mail_signatures (node_id, owner_id, from_addr);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_mail_signatures_owner_from;")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_signatures_owner "
        "ON mail_signatures (node_id, owner_id);"
    )
    op.execute("ALTER TABLE mail_signatures DROP COLUMN IF EXISTS from_addr;")
