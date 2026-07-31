"""support programs: presentation time

지원사업 발표일에 발표 시간을 함께 기록하고, 자동 등록되는 팀 일정
이벤트에도 시간이 들어가도록 presentation_time TIME 컬럼을 추가한다.

Revision ID: 0049_support_presentation_time
Revises: 0048_support_program_extras
Create Date: 2026-06-12
"""

from __future__ import annotations

from alembic import op

revision = "0049_support_presentation_time"
down_revision = "0048_support_program_extras"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE support_programs ADD COLUMN presentation_time TIME;")


def downgrade() -> None:
    op.execute("ALTER TABLE support_programs DROP COLUMN IF EXISTS presentation_time;")
