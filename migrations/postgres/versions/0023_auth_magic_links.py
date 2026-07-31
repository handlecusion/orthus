"""Auth magic link table.

Revision ID: 0023_auth_magic_links
Revises: 0022_data_gaps_context_wiki_slug
Create Date: 2026-06-07
"""

from alembic import op

revision = "0023_auth_magic_links"
down_revision = "0022_data_gaps_context_wiki_slug"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auth_magic_links (
          magic_link_id            UUID PRIMARY KEY,
          token_hash               TEXT NOT NULL UNIQUE,
          node_id                  TEXT NOT NULL,
          email                    TEXT NOT NULL,
          next_path                TEXT NOT NULL,
          issued_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
          expires_at               TIMESTAMPTZ NOT NULL,
          consumed_at              TIMESTAMPTZ,
          request_user_agent_hash  TEXT,
          request_ip_hash          TEXT
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_auth_magic_links_lookup
          ON auth_magic_links(token_hash, node_id)
          WHERE consumed_at IS NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX idx_auth_magic_links_email
          ON auth_magic_links(node_id, email, issued_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_auth_magic_links_email;")
    op.execute("DROP INDEX IF EXISTS idx_auth_magic_links_lookup;")
    op.execute("DROP TABLE IF EXISTS auth_magic_links;")
