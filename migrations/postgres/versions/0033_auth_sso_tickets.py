"""auth cross-node SSO ticket replay guard

Single-use ticket ids consumed by relying-party nodes during cross-node
session handoff. Only the random jti is stored (not a secret), so a leaked
ticket cannot be replayed within its short TTL.

Revision ID: 0033_auth_sso_tickets
Revises: 0032_seed_dashboard_data
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision = "0033_auth_sso_tickets"
down_revision = "0032_seed_dashboard_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auth_sso_tickets (
          jti TEXT PRIMARY KEY,
          node_id TEXT NOT NULL,
          consumed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth_sso_tickets;")
