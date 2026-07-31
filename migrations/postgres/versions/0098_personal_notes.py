"""개인 메모(personal notes): owner-scope + 팀 공유 + 프로젝트 링크

기존 회사 공용 노트패드(`dashboard_pages`, kind='memo')를 애플 메모장식 개인
메모로 확장한다.

- `dashboard_pages.owner_id`: 메모 소유자 user_id. NULL = 레거시 회사 공용(보존).
  신규 메모는 생성자 user_id를 채운다. 접근 게이트는 최상위 메모 단위이고,
  본문 안 중첩 subpage(kind='page')는 기존처럼 node 스코프로 둔다(부모 메모를
  열 수 있어야만 도달 가능).
- `note_shares`: 메모를 특정 팀원(team_members.user_id)에게 view/edit로 공유하고,
  수신자가 "얼라인 확인"(acknowledged_at)을 남길 수 있게 한다.
- `note_project_links`: 메모 ↔ 회사 프로젝트(dashboard_projects) N:N 링크.

Revision ID: 0098_personal_notes
Revises: 0097_documents_list_indexes
Create Date: 2026-07-06

주의: 원 브랜치(feat/plans-live-collab)에서는 유동 마이그레이션 회피용으로 안정
head 0092에 체인했으나, main 클린 릴리스로 옮기며 현재 main head인
0097_documents_list_indexes 위로 재체인해 단일 head를 유지한다(alembic merge 불필요).
"""

from __future__ import annotations

from alembic import op

revision = "0098_personal_notes"
down_revision = "0097_documents_list_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE dashboard_pages ADD COLUMN IF NOT EXISTS owner_id UUID;"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dashboard_pages_owner "
        "ON dashboard_pages (node_id, kind, owner_id);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS note_shares (
          share_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          page_id UUID NOT NULL,
          shared_with UUID NOT NULL,
          permission TEXT NOT NULL DEFAULT 'view',
          created_by UUID NOT NULL,
          acknowledged_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (page_id, shared_with)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_note_shares_target "
        "ON note_shares (node_id, shared_with);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_note_shares_page ON note_shares (page_id);"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS note_project_links (
          page_id UUID NOT NULL,
          project_id UUID NOT NULL,
          node_id TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (page_id, project_id)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_note_project_links_project "
        "ON note_project_links (project_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_note_project_links_project;")
    op.execute("DROP TABLE IF EXISTS note_project_links;")
    op.execute("DROP INDEX IF EXISTS ix_note_shares_page;")
    op.execute("DROP INDEX IF EXISTS ix_note_shares_target;")
    op.execute("DROP TABLE IF EXISTS note_shares;")
    op.execute("DROP INDEX IF EXISTS ix_dashboard_pages_owner;")
    op.execute("ALTER TABLE dashboard_pages DROP COLUMN IF EXISTS owner_id;")
