"""support programs: presentation dates, owner, calendar link, aux notes

지원사업 보드 확장: 발표준비 마감일/발표일, 담당자, 발표일의 팀
일정 자동 등록 링크. 보드 아래 인라인 DB(꿀팁/검색 사이트) 대응
support_notes 테이블 추가.

Revision ID: 0048_support_program_extras
Revises: 0047_support_programs
Create Date: 2026-06-12
"""

from __future__ import annotations

from alembic import op

revision = "0048_support_program_extras"
down_revision = "0047_support_programs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE support_programs ADD COLUMN presentation_deadline DATE;")
    op.execute("ALTER TABLE support_programs ADD COLUMN presentation_date DATE;")
    op.execute(
        "ALTER TABLE support_programs ADD COLUMN owner_member_id UUID "
        "REFERENCES team_members(member_id) ON DELETE SET NULL;"
    )
    op.execute("ALTER TABLE support_programs ADD COLUMN calendar_event_id UUID;")
    op.execute(
        """
        CREATE TABLE support_notes (
          note_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'tip',
          title TEXT NOT NULL,
          description TEXT,
          url TEXT,
          body TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX ix_support_notes_kind ON support_notes (node_id, kind, sort_order);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_support_notes_kind;")
    op.execute("DROP TABLE IF EXISTS support_notes;")
    op.execute("ALTER TABLE support_programs DROP COLUMN IF EXISTS calendar_event_id;")
    op.execute("ALTER TABLE support_programs DROP COLUMN IF EXISTS owner_member_id;")
    op.execute("ALTER TABLE support_programs DROP COLUMN IF EXISTS presentation_date;")
    op.execute("ALTER TABLE support_programs DROP COLUMN IF EXISTS presentation_deadline;")
