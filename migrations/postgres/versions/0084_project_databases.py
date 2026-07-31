"""project inline databases — 노션식 데이터베이스 + 칸반 보드

회사 프로젝트가 노션 페이지처럼 동작하도록, 프로젝트(또는 페이지) 본문에 임베드되는
인라인 데이터베이스를 추가한다. 한 데이터베이스는 타입 있는 속성(properties)과 뷰(views,
표/보드)를 가지며, 칸반 보드는 select/status 속성으로 group_by 한 board view다(표와 같은
데이터의 다른 뷰). 행(project_database_rows)은 노션의 한 페이지에 해당하고 props는
property id → 값 맵이다.

Revision ID: 0084_project_databases
Revises: 0083_mail_tracking
Create Date: 2026-06-28
"""

from __future__ import annotations

from alembic import op

revision = "0084_project_databases"
down_revision = "0083_mail_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE project_databases (
          database_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          project_id UUID,
          title TEXT NOT NULL DEFAULT '데이터베이스',
          icon TEXT,
          properties JSONB NOT NULL DEFAULT '[]'::jsonb,
          views JSONB NOT NULL DEFAULT '[]'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_project_databases_project ON project_databases (node_id, project_id);"
    )
    op.execute(
        """
        CREATE TABLE project_database_rows (
          row_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          database_id UUID NOT NULL,
          props JSONB NOT NULL DEFAULT '{}'::jsonb,
          sort_order DOUBLE PRECISION NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_project_database_rows_db ON project_database_rows (database_id);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_database_rows;")
    op.execute("DROP TABLE IF EXISTS project_databases;")
