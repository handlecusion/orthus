"""documents 목록 조회용 btree 인덱스

/corpus 목록(scope='company' + updated_at DESC 정렬)과 /editor 목록
(user_id + updated_at DESC)이 문서 수 증가에 따라 seq scan으로 느려지는 것을
막는다. 스키마/데이터 변경 없음.

Revision ID: 0097_documents_list_indexes
Revises: 0096_calendar_return_time
Create Date: 2026-07-06
"""

from __future__ import annotations

from alembic import op

revision = "0097_documents_list_indexes"
down_revision = "0096_calendar_return_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_scope_updated "
        "ON documents (scope, updated_at DESC);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_user_updated "
        "ON documents (user_id, updated_at DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_documents_user_updated;")
    op.execute("DROP INDEX IF EXISTS idx_documents_scope_updated;")
