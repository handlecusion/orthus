"""infra_resources: service grouping (parent_id, unit_price, balance) + project links

Revision ID: 0060_infra_groups_links
Revises: 0059_infra_period_dash
Create Date: 2026-06-15

parent_id: self-FK; sub-services point to a group row (e.g. "APick" group with
  children "APick: 실명인증", "APick: 계좌인증"). ON DELETE SET NULL so deleting a
  group leaves its children as standalone services rather than cascading.
unit_price: per-call/unit price of a (sub-)service (단가, e.g. 30원).
balance: prepaid remaining balance, typically on the group row (잔여량, e.g.
  40,000원).
infra_resource_projects: N:M links between an api_key (sub-)service and a
  dashboard_project. Simple connection, no per-link weight.
"""

from __future__ import annotations

from alembic import op

revision = "0060_infra_groups_links"
down_revision = "0059_infra_period_dash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE infra_resources ADD COLUMN parent_id UUID "
        "REFERENCES infra_resources(resource_id) ON DELETE SET NULL;"
    )
    op.execute("ALTER TABLE infra_resources ADD COLUMN unit_price NUMERIC(14, 2);")
    op.execute("ALTER TABLE infra_resources ADD COLUMN balance NUMERIC(14, 2);")
    op.execute(
        """
        CREATE TABLE infra_resource_projects (
          link_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          resource_id UUID NOT NULL
            REFERENCES infra_resources(resource_id) ON DELETE CASCADE,
          project_id UUID NOT NULL
            REFERENCES dashboard_projects(project_id) ON DELETE CASCADE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (node_id, resource_id, project_id)
        );
        """
    )
    op.execute("CREATE INDEX ix_infra_links_node ON infra_resource_projects (node_id);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS infra_resource_projects;")
    op.execute("ALTER TABLE infra_resources DROP COLUMN IF EXISTS balance;")
    op.execute("ALTER TABLE infra_resources DROP COLUMN IF EXISTS unit_price;")
    op.execute("ALTER TABLE infra_resources DROP COLUMN IF EXISTS parent_id;")
