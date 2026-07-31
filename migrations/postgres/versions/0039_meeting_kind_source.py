"""meeting notes: internal/external kind, partner link, plan-sync source

회의록을 내부/외부 회의로 분리하고, 외부 회의에 파트너사를 연결한다. 주간/월간
계획·회고 저장 시 내부 회의가 자동 생성되도록 source/source_ref 멱등 키를 둔다.

Revision ID: 0039_meeting_kind_source
Revises: 0038_project_assignments
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision = "0039_meeting_kind_source"
down_revision = "0038_project_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE meeting_notes ADD COLUMN IF NOT EXISTS partner_id UUID "
        "REFERENCES partner_companies(partner_id) ON DELETE SET NULL;"
    )
    op.execute(
        "ALTER TABLE meeting_notes ADD COLUMN IF NOT EXISTS meeting_kind TEXT NOT NULL "
        "DEFAULT 'internal';"
    )
    op.execute("ALTER TABLE meeting_notes DROP CONSTRAINT IF EXISTS meeting_notes_kind_check;")
    op.execute(
        "ALTER TABLE meeting_notes ADD CONSTRAINT meeting_notes_kind_check "
        "CHECK (meeting_kind IN ('internal', 'external'));"
    )
    op.execute(
        "ALTER TABLE meeting_notes ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';"
    )
    op.execute("ALTER TABLE meeting_notes ADD COLUMN IF NOT EXISTS source_ref TEXT;")
    # idempotency for auto-synced plan/retro meetings; manual rows keep
    # source_ref NULL and NULLs are distinct, so they are unconstrained.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_meeting_notes_source "
        "ON meeting_notes(node_id, source, source_ref);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_meeting_notes_source;")
    op.execute("ALTER TABLE meeting_notes DROP CONSTRAINT IF EXISTS meeting_notes_kind_check;")
    op.execute("ALTER TABLE meeting_notes DROP COLUMN IF EXISTS source_ref;")
    op.execute("ALTER TABLE meeting_notes DROP COLUMN IF EXISTS source;")
    op.execute("ALTER TABLE meeting_notes DROP COLUMN IF EXISTS meeting_kind;")
    op.execute("ALTER TABLE meeting_notes DROP COLUMN IF EXISTS partner_id;")
