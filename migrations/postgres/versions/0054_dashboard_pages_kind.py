"""dashboard pages: kind (memo vs nested page)

자유 메모(아이데이션) 공간을 위해 dashboard_pages에 kind를 추가한다.
kind='memo'는 메모 목록에 보이는 최상위 페이지, 기본 'page'는 본문 안에서
"/페이지"로 만드는 중첩 하위 페이지(블록이 참조)다. 목록은 kind로 거른다.

Revision ID: 0054_dashboard_pages_kind
Revises: 0053_dashboard_pages
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op

revision = "0054_dashboard_pages_kind"
down_revision = "0053_dashboard_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE dashboard_pages ADD COLUMN kind TEXT NOT NULL DEFAULT 'page';"
    )
    op.execute(
        "CREATE INDEX ix_dashboard_pages_kind ON dashboard_pages (node_id, kind, updated_at DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dashboard_pages_kind;")
    op.execute("ALTER TABLE dashboard_pages DROP COLUMN IF EXISTS kind;")
