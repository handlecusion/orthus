"""recruiting candidate team comments (후보별 팀원 메모)

Revision ID: 0066_recruiting_comments
Revises: 0065_recruiting_candidates
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0066_recruiting_comments"
down_revision = "0065_recruiting_candidates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recruiting_candidate_comments",
        sa.Column("comment_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column(
            "candidate_id",
            UUID(as_uuid=True),
            sa.ForeignKey("recruiting_candidates.candidate_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author_member_id", UUID(as_uuid=True)),
        sa.Column("author_name", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_recruiting_comment_candidate",
        "recruiting_candidate_comments",
        ["candidate_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recruiting_comment_candidate", table_name="recruiting_candidate_comments"
    )
    op.drop_table("recruiting_candidate_comments")
