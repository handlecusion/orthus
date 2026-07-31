"""infra providers (제공처 = group with 잔여량) + service provider_id/price_unit

Revision ID: 0061_infra_providers
Revises: 0060_infra_groups_links
Create Date: 2026-06-15

제공처(provider) becomes a first-class group entity holding the prepaid balance
(잔여량). API/services point at a provider via provider_id and grow a price_unit
(billing basis: per_call/per_1m/monthly/hourly/other) alongside unit_price.

The old self-referential grouping (infra_resources.parent_id) and per-service
balance are left in place but no longer used by the API/service UI.
"""

from __future__ import annotations

from alembic import op

revision = "0061_infra_providers"
down_revision = "0060_infra_groups_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE infra_providers (
          provider_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          name TEXT NOT NULL,
          balance NUMERIC(14, 2),
          link TEXT,
          notes TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (node_id, name)
        );
        """
    )
    op.execute(
        "ALTER TABLE infra_resources ADD COLUMN provider_id UUID "
        "REFERENCES infra_providers(provider_id) ON DELETE SET NULL;"
    )
    op.execute("ALTER TABLE infra_resources ADD COLUMN price_unit TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE infra_resources DROP COLUMN IF EXISTS price_unit;")
    op.execute("ALTER TABLE infra_resources DROP COLUMN IF EXISTS provider_id;")
    op.execute("DROP TABLE IF EXISTS infra_providers;")
