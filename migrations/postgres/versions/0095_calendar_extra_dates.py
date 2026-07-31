"""팀 캘린더 추가 날짜(같은 일정을 다른 날짜에도)

반복 규칙(repeat_freq)과 별개로, 한 일정을 임의의 다른 날짜에도 그대로
붙이는 extra_dates 컬럼을 추가한다. 마스터 행 하나만 저장하고 list_calendar가
event_date + 반복 회차 + extra_dates를 합쳐 조회 윈도 안의 회차로 펼친다.

- extra_dates: 추가 시작일(ISO date 문자열) 목록. 각 날짜는 event_date와 같은
  기간(end_date - event_date)만큼의 회차를 만든다. repeat_freq 유무와 무관하게
  동작한다.

Revision ID: 0095_calendar_extra_dates
Revises: 0094_project_tracking
Create Date: 2026-07-05
"""

from __future__ import annotations

from alembic import op

revision = "0095_calendar_extra_dates"
down_revision = "0094_project_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE team_calendar_events "
        "ADD COLUMN IF NOT EXISTS extra_dates JSONB NOT NULL DEFAULT '[]';"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS extra_dates;")
