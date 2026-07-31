"""company KPI (OKR + North Star): dashboard_kpis + checkins

Revision ID: 0070_dashboard_kpis
Revises: 0069_recruiting_status_values
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0070_dashboard_kpis"
down_revision = "0069_recruiting_status_values"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_kpis",
        sa.Column("kpi_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column(
            "parent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dashboard_kpis.kpi_id", ondelete="CASCADE"),
        ),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("cadence", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date()),
        sa.Column("fiscal_year", sa.Integer(), nullable=False),
        sa.Column("quarter", sa.Integer()),
        sa.Column("month", sa.Integer()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("metric_type", sa.Text(), nullable=False, server_default="number"),
        sa.Column("unit", sa.Text()),
        sa.Column("baseline", sa.Numeric(18, 4)),
        sa.Column("target", sa.Numeric(18, 4)),
        sa.Column("current_value", sa.Numeric(18, 4)),
        sa.Column("direction", sa.Text(), nullable=False, server_default="up"),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dashboard_projects.project_id", ondelete="SET NULL"),
        ),
        sa.Column(
            "owner_member_id",
            UUID(as_uuid=True),
            sa.ForeignKey("team_members.member_id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="on_track"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_dashboard_kpis_node", "dashboard_kpis", ["node_id"])
    op.create_index(
        "ix_dashboard_kpis_node_parent", "dashboard_kpis", ["node_id", "parent_id"]
    )
    op.create_index(
        "ix_dashboard_kpis_node_period",
        "dashboard_kpis",
        ["node_id", "cadence", "period_start"],
    )
    op.create_index(
        "ix_dashboard_kpis_node_project", "dashboard_kpis", ["node_id", "project_id"]
    )

    op.create_table(
        "dashboard_kpi_checkins",
        sa.Column("checkin_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column(
            "kpi_id",
            UUID(as_uuid=True),
            sa.ForeignKey("dashboard_kpis.kpi_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("value", sa.Numeric(18, 4)),
        sa.Column("status", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.Column(
            "author_member_id",
            UUID(as_uuid=True),
            sa.ForeignKey("team_members.member_id", ondelete="SET NULL"),
        ),
        sa.Column("checkin_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_kpi_checkins_node_kpi",
        "dashboard_kpi_checkins",
        ["node_id", "kpi_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_kpi_checkins_node_kpi", table_name="dashboard_kpi_checkins")
    op.drop_table("dashboard_kpi_checkins")
    op.drop_index("ix_dashboard_kpis_node_project", table_name="dashboard_kpis")
    op.drop_index("ix_dashboard_kpis_node_period", table_name="dashboard_kpis")
    op.drop_index("ix_dashboard_kpis_node_parent", table_name="dashboard_kpis")
    op.drop_index("ix_dashboard_kpis_node", table_name="dashboard_kpis")
    op.drop_table("dashboard_kpis")
