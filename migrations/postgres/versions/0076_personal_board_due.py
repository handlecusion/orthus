"""personal board task due date — optional deadline + countdown

내 보드(개인 워크스페이스) 태스크에 마감일(due date)을 추가한다. `scheduled_date`는
"언제 작업할지"(달력 배치)이고, `due_date`는 "언제까지 끝내야 하는지"(기한)로 의미가
다르다. `due_time`은 선택값으로, 설정되면 카드에서 "몇시간 남음"까지 보여줄 수 있고
비어 있으면 그 날 하루 단위 기한("몇일 남음")으로 본다.

backcompat: 두 컬럼 모두 NULL 허용이라 기존 row는 마감일 없음으로 그대로 유지된다.
`due_time`은 `due_date`가 있을 때만 채워지며(앱 레벨 가드), 둘 다 NULL이 기본이다.

Revision ID: 0076_personal_board_due
Revises: 0075_agent_task_threads
Create Date: 2026-06-27
"""

from __future__ import annotations

from alembic import op

revision = "0076_personal_board_due"
down_revision = "0075_agent_task_threads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE personal_board_tasks ADD COLUMN due_date DATE;")
    op.execute("ALTER TABLE personal_board_tasks ADD COLUMN due_time TIME;")


def downgrade() -> None:
    op.execute("ALTER TABLE personal_board_tasks DROP COLUMN due_time;")
    op.execute("ALTER TABLE personal_board_tasks DROP COLUMN due_date;")
