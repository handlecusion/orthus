"""project database rows cover image — 노션식 행/카드 커버

행(=페이지)에 커버 이미지 URL을 더한다. 칸반 카드 상단 배너 + 행 페이지 머리에
노션처럼 커버를 보여준다. 기존 행은 cover 가 NULL 이며 호환된다.

Revision ID: 0086_project_database_row_cover
Revises: 0085_project_database_row_pages
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0086_project_database_row_cover"
down_revision = "0085_project_database_row_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_database_rows", sa.Column("cover", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("project_database_rows", "cover")
