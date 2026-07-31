"""project database rows as pages — 행 자체가 노션 페이지(icon + 본문)

데이터베이스의 한 행을 노션처럼 페이지로 열 수 있게 icon + 자유 본문(body)을 더한다.
기존 행은 icon/body 가 NULL 이며 호환된다.

Revision ID: 0085_project_database_row_pages
Revises: 0084_project_databases
Create Date: 2026-06-28
"""

from __future__ import annotations

from alembic import op

revision = "0085_project_database_row_pages"
down_revision = "0084_project_databases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE project_database_rows ADD COLUMN IF NOT EXISTS icon TEXT;")
    op.execute("ALTER TABLE project_database_rows ADD COLUMN IF NOT EXISTS body TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE project_database_rows DROP COLUMN IF EXISTS body;")
    op.execute("ALTER TABLE project_database_rows DROP COLUMN IF EXISTS icon;")
