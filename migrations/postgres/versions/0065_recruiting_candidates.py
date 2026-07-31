"""recruiting candidates (인재영입 후보 리스트)

Revision ID: 0065_recruiting_candidates
Revises: 0064_agent_task_command_kind
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0065_recruiting_candidates"
down_revision = "0064_agent_task_command_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recruiting_candidates",
        sa.Column("candidate_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text()),
        sa.Column("education", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("linkedin", sa.Text()),
        sa.Column("link", sa.Text()),
        sa.Column("send_status", sa.Text(), nullable=False, server_default="미발송"),
        sa.Column("note", sa.Text()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_recruiting_candidates_node",
        "recruiting_candidates",
        ["node_id", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_recruiting_candidates_node", table_name="recruiting_candidates")
    op.drop_table("recruiting_candidates")
