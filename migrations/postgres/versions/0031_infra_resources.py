"""infra resources (GPU / storage / API keys / servers)

Revision ID: 0031_infra_resources
Revises: 0030_meeting_notes
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0031_infra_resources"
down_revision = "0030_meeting_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE infra_resources (
          resource_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'gpu'
            CHECK (kind IN ('gpu', 'storage', 'server', 'api_key', 'other')),
          name TEXT NOT NULL,
          vendor TEXT,
          model TEXT,
          location TEXT,
          status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'idle', 'reserved', 'down', 'expired')),
          capacity NUMERIC(14, 2),
          used NUMERIC(14, 2),
          unit TEXT,
          usage_percent INTEGER,
          owner_member_id UUID REFERENCES team_members(member_id) ON DELETE SET NULL,
          link TEXT,
          notes TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_infra_resources_node_kind ON infra_resources(node_id, kind);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_infra_resources_node_kind;")
    op.execute("DROP TABLE IF EXISTS infra_resources;")
