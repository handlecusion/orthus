"""dashboard pages: nested Notion-style sub-pages

프로젝트 본문 등 노션식 에디터 안에서 "/하위 페이지"로 만드는 중첩 페이지를
담는 테이블. 페이지는 BlockNote 블록 JSON 본문(body)을 가지며, subpage 블록이
page_id로 참조한다. 페이지 본문 안에 또 subpage 블록을 넣어 무한 중첩이 가능하다.
부모 링크는 참조하는 블록에 암시적으로 존재하므로 별도 FK는 두지 않는다.

Revision ID: 0053_dashboard_pages
Revises: 0052_project_rich_body
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op

revision = "0053_dashboard_pages"
down_revision = "0052_project_rich_body"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dashboard_pages (
          page_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          title TEXT NOT NULL DEFAULT '새 페이지',
          icon TEXT,
          body TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX ix_dashboard_pages_node ON dashboard_pages (node_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dashboard_pages_node;")
    op.execute("DROP TABLE IF EXISTS dashboard_pages;")
