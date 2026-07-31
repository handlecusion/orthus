"""personal board task no_return flag — 복귀불가(팀 일정 종료 시각 옆 부가 선택)

내 보드(개인 워크스페이스) 태스크에 선택적 boolean 플래그 `no_return`(복귀불가)을
추가한다. 팀 스케줄 종료 시각(`scheduled_end_time`) 피커에서 곁들여 고를 수 있는 부가
선택이며, 켜지면 카드에 "복귀불가" 배지가 떠 그 사람이 돌아오지 않음을 팀에 알린다.

`scheduled_end_time`과 같은 시간 편집 UI에 살지만 값으로는 독립적이다 — 종료 시각이
있든 없든 켤 수 있다. board 태스크가 팀 일정 이벤트(`team_calendar_events`)와 양방향으로
미러되므로, 배지가 그 왕복 동기화에서 살아남도록 같은 컬럼을 양쪽 테이블에 둔다.

backcompat: NOT NULL + server_default 'false'라 기존 row는 모두 복귀불가 아님(false)으로
그대로 채워진다.

Revision ID: 0080_personal_board_no_return
Revises: 9352b9615419
Create Date: 2026-06-28

(revision id는 alembic_version.version_num VARCHAR(32) 한도 안이다 — 29자.)
"""

from __future__ import annotations

from alembic import op

revision = "0080_personal_board_no_return"
down_revision = "9352b9615419"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE personal_board_tasks ADD COLUMN no_return BOOLEAN NOT NULL DEFAULT false;"
    )
    op.execute(
        "ALTER TABLE team_calendar_events ADD COLUMN no_return BOOLEAN NOT NULL DEFAULT false;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE team_calendar_events DROP COLUMN no_return;")
    op.execute("ALTER TABLE personal_board_tasks DROP COLUMN no_return;")
