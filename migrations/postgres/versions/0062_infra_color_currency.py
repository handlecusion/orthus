"""infra: per-service line color + currency (KRW/USD)

Revision ID: 0062_infra_color_currency
Revises: 0061_infra_providers
Create Date: 2026-06-15

color: hex line color for a service's connections in the graph.
currency: 'KRW' | 'USD' for unit_price (service) and balance (provider).
"""

from __future__ import annotations

from alembic import op

revision = "0062_infra_color_currency"
down_revision = "0061_infra_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE infra_resources ADD COLUMN color TEXT;")
    op.execute("ALTER TABLE infra_resources ADD COLUMN currency TEXT;")
    op.execute("ALTER TABLE infra_providers ADD COLUMN currency TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE infra_providers DROP COLUMN IF EXISTS currency;")
    op.execute("ALTER TABLE infra_resources DROP COLUMN IF EXISTS currency;")
    op.execute("ALTER TABLE infra_resources DROP COLUMN IF EXISTS color;")
