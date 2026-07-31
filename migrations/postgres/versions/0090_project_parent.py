"""dashboard_projects: 하위 프로젝트 계층 (parent_project_id)

프로젝트 아래에 하위 프로젝트를 두기 위한 self-FK 컬럼이다. 깊이는 2단계
(루트 → 하위)로 앱 레이어에서 제한한다. 부모 삭제 시 하위도 함께 삭제한다
(기존 delete_project 하드 삭제 규약과 동일).

Revision ID: 0090_project_parent
Revises: 0089_wiki_pages_display_title
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op

revision = "0090_project_parent"
down_revision = "0089_wiki_pages_display_title"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE dashboard_projects "
        "ADD COLUMN IF NOT EXISTS parent_project_id UUID "
        "REFERENCES dashboard_projects(project_id) ON DELETE CASCADE;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dashboard_projects_parent "
        "ON dashboard_projects (parent_project_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dashboard_projects_parent;")
    op.execute("ALTER TABLE dashboard_projects DROP COLUMN IF EXISTS parent_project_id;")
