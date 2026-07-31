"""infra_resources: add period + 'dashboard' kind

Revision ID: 0059_infra_period_dash
Revises: 0057_board_team_event_unique
Create Date: 2026-06-14

period: free-text usage/lease period (e.g. "2026-06 ~ 2026-12", "7개월 확정").
'dashboard' kind: rows whose `link` is an embeddable Grafana/observability URL,
rendered as live iframes on the infra page.
"""

from __future__ import annotations

from alembic import op

revision = "0059_infra_period_dash"
down_revision = "0057_board_team_event_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE infra_resources ADD COLUMN period TEXT;")
    op.execute("ALTER TABLE infra_resources DROP CONSTRAINT IF EXISTS infra_resources_kind_check;")
    op.execute(
        """
        ALTER TABLE infra_resources ADD CONSTRAINT infra_resources_kind_check
          CHECK (kind IN ('gpu', 'storage', 'server', 'api_key', 'dashboard', 'other'));
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE infra_resources DROP CONSTRAINT IF EXISTS infra_resources_kind_check;")
    op.execute(
        """
        ALTER TABLE infra_resources ADD CONSTRAINT infra_resources_kind_check
          CHECK (kind IN ('gpu', 'storage', 'server', 'api_key', 'other'));
        """
    )
    op.execute("ALTER TABLE infra_resources DROP COLUMN period;")
