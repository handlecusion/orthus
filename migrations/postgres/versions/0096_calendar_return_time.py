"""팀 캘린더 복귀 시간(복귀 불가 대신 복귀 시각 지정)

"복귀 불가"(no_return, 담당자가 이 일정 뒤 복귀하지 않음)와 별개로, 복귀하는
경우의 복귀 시각을 적을 수 있게 return_time 컬럼을 추가한다. 둘은 상호 배타적이다
— no_return이 true면 return_time은 NULL로 강제한다(앱 레이어에서 정리).

- return_time: 담당자가 복귀하는 시각(TIME, NULL=미지정). no_return과 동시에
  값을 갖지 않는다. 복귀 불가는 UI에서 집 이모지(🏠)로 표시한다.

Revision ID: 0096_calendar_return_time
Revises: 0095_calendar_extra_dates
Create Date: 2026-07-06
"""

from __future__ import annotations

from alembic import op

revision = "0096_calendar_return_time"
down_revision = "0095_calendar_extra_dates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE team_calendar_events ADD COLUMN IF NOT EXISTS return_time TIME;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN IF EXISTS return_time;")
