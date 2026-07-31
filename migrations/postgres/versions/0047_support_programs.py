"""dashboard support programs (지원사업) board

지원사업 트래킹 보드. Notion '지원사업관련해서' DB 대체: 상태 컬럼 기반 칸반으로
관리하고, project_id로 대시보드 프로젝트에 연결한다.

Revision ID: 0047_support_programs
Revises: 0046_email_send_manual_origin
Create Date: 2026-06-12
"""

from __future__ import annotations

from alembic import op

revision = "0047_support_programs"
down_revision = "0046_email_send_manual_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE support_programs (
          program_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          name TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT '시작 전',
          project_id UUID REFERENCES dashboard_projects(project_id) ON DELETE SET NULL,
          company TEXT,
          deadline DATE,
          task_number TEXT,
          url TEXT,
          follow_up TEXT,
          body TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_support_programs_board ON support_programs (node_id, status, sort_order);"
    )
    op.execute("CREATE INDEX ix_support_programs_project ON support_programs (project_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_support_programs_project;")
    op.execute("DROP INDEX IF EXISTS ix_support_programs_board;")
    op.execute("DROP TABLE IF EXISTS support_programs;")
