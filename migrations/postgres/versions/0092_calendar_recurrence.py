"""팀 캘린더 반복 일정(루틴)

team_calendar_events에 반복 규칙 컬럼을 추가한다. 반복 일정은 마스터 행
하나만 저장하고, 조회(list_calendar) 시 요청 윈도 안의 회차로 펼친다.

- repeat_freq: NULL(반복 없음) | 'daily' | 'weekly' | 'biweekly' | 'monthly'
- repeat_weekdays: weekly/biweekly에서 반복할 요일 목록.
  Python date.weekday() 규약(0=월 … 6=일)으로 저장한다.
- repeat_until: 반복 종료일(포함). NULL = 무기한.

Revision ID: 0092_calendar_recurrence
Revises: 0091_project_se_requirements
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op

revision = "0092_calendar_recurrence"
down_revision = "0091_project_se_requirements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events ADD COLUMN IF NOT EXISTS repeat_freq TEXT;")
    op.execute(
        "ALTER TABLE team_calendar_events "
        "ADD COLUMN IF NOT EXISTS repeat_weekdays JSONB NOT NULL DEFAULT '[]';"
    )
    op.execute("ALTER TABLE team_calendar_events ADD COLUMN IF NOT EXISTS repeat_until DATE;")


def downgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS repeat_until;")
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS repeat_weekdays;")
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS repeat_freq;")
