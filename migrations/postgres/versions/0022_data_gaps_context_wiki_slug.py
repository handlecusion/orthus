"""data gaps wiki page context

Revision ID: 0022_data_gaps_context_wiki_slug
Revises: 0021_email_send_log
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0022_data_gaps_context_wiki_slug"
down_revision = "0021_email_send_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE data_gaps ADD COLUMN IF NOT EXISTS context_wiki_slug TEXT;")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_data_gaps_scope_owner_context_slug
          ON data_gaps(
            scope,
            COALESCE(owner_id, '00000000-0000-0000-0000-000000000000'::uuid),
            context_wiki_slug
          )
          WHERE context_wiki_slug IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_data_gaps_scope_owner_context_slug;")
    op.execute("ALTER TABLE data_gaps DROP COLUMN IF EXISTS context_wiki_slug;")
