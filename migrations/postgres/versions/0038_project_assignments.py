"""project member-role assignments + project description/status

팀원에게 프로젝트를 배정하고 프로젝트별 역할을 부여한다. 프로젝트 관리 화면에서
프로젝트별 멤버/역할을 보고, 팀원 화면에서 각자 맡은 프로젝트를 본다.
dashboard_projects에 관리용 description/status 컬럼을 추가한다.

Revision ID: 0038_project_assignments
Revises: 0037_seed_partners
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision = "0038_project_assignments"
down_revision = "0037_seed_partners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE dashboard_projects ADD COLUMN IF NOT EXISTS description TEXT;")
    op.execute("ALTER TABLE dashboard_projects ADD COLUMN IF NOT EXISTS status TEXT;")
    op.execute(
        """
        CREATE TABLE project_assignments (
          assignment_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          project_id UUID NOT NULL
            REFERENCES dashboard_projects(project_id) ON DELETE CASCADE,
          member_id UUID NOT NULL
            REFERENCES team_members(member_id) ON DELETE CASCADE,
          role TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (project_id, member_id)
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_project_assignments_project ON project_assignments(project_id, sort_order);"
    )
    op.execute("CREATE INDEX idx_project_assignments_member ON project_assignments(member_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_project_assignments_member;")
    op.execute("DROP INDEX IF EXISTS idx_project_assignments_project;")
    op.execute("DROP TABLE IF EXISTS project_assignments;")
    op.execute("ALTER TABLE dashboard_projects DROP COLUMN IF EXISTS status;")
    op.execute("ALTER TABLE dashboard_projects DROP COLUMN IF EXISTS description;")
