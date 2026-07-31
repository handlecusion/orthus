"""dashboard projects: rich Notion-style body

프로젝트 상세를 노션 페이지처럼 자유롭게 쓰도록 BlockNote 블록 JSON을 담는
body TEXT 컬럼을 추가한다. 기존 description은 목록의 짧은 요약으로 계속 쓴다
(support_programs/support_notes의 description+body 패턴과 동일).

Revision ID: 0052_project_rich_body
Revises: 0051_support_seed_sort_order
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op

revision = "0052_project_rich_body"
down_revision = "0051_support_seed_sort_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE dashboard_projects ADD COLUMN body TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE dashboard_projects DROP COLUMN IF EXISTS body;")
