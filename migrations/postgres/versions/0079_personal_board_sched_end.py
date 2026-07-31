"""personal board task scheduled end time — 시간 블록(시작→종료) + 시간순 정렬 기본값

내 보드(개인 워크스페이스) 태스크에 선택적 종료 시각(`scheduled_end_time`)을 추가한다.
지금까지 태스크는 시작 시각(`scheduled_time`) 하나만 가졌고, 이제 시작→종료의 시간
블록으로 표현할 수 있다. Sunsama식 하루 일정 패널과 시간순 정렬을 떠받친다.

종료 시각은 시작 시각이 있을 때만 유효하고(앱 레벨 가드), 같은 날 기준 시작보다
엄밀히 커야 한다. backcompat: 컬럼은 NULL 허용이라 기존 row는 종료 시각 없음으로
그대로 유지된다.

함께 정렬 기본값을 'time'으로 옮긴다. FE가 시간순 정렬 모드를 추가하면서 이를 기본으로
삼기 때문에, 아직 직전 기본값('manual')에 머무른 사용자만 'time'으로 넘긴다(명시적으로
다른 모드를 고른 사용자는 건드리지 않는다).

Revision ID: 0079_personal_board_sched_end
Revises: 0078_project_roles
Create Date: 2026-06-27

(revision id는 alembic_version.version_num VARCHAR(32) 한도에 맞춰 'sched'로 줄였다.)
"""

from __future__ import annotations

from alembic import op

revision = "0079_personal_board_sched_end"
down_revision = "0078_project_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE personal_board_tasks ADD COLUMN scheduled_end_time TIME;")
    # 'time' 정렬 모드를 CHECK 허용 목록에 추가(기존 CHECK는 manual/priority만 허용).
    # UPDATE 전에 제약을 넓혀야 한다(아니면 'time'으로 바꾸는 UPDATE가 옛 제약에 걸린다).
    op.execute(
        "ALTER TABLE personal_board_preferences "
        "DROP CONSTRAINT IF EXISTS personal_board_preferences_sort_mode_check;"
    )
    op.execute(
        "ALTER TABLE personal_board_preferences "
        "ADD CONSTRAINT personal_board_preferences_sort_mode_check "
        "CHECK (sort_mode IN ('manual', 'priority', 'time'));"
    )
    op.execute("UPDATE personal_board_preferences SET sort_mode='time' WHERE sort_mode='manual';")
    # 'schedule' 우측 패널(썬사마식 당일 타임라인)을 CHECK 허용 목록에 추가.
    op.execute(
        "ALTER TABLE personal_board_preferences "
        "DROP CONSTRAINT IF EXISTS personal_board_preferences_right_panel_check;"
    )
    op.execute(
        "ALTER TABLE personal_board_preferences "
        "ADD CONSTRAINT personal_board_preferences_right_panel_check "
        "CHECK (right_panel IN ('backlog', 'integrations', 'schedule', 'hidden'));"
    )


def downgrade() -> None:
    # right_panel: 좁히기 전에 'schedule' row를 hidden으로 되돌린다.
    op.execute("UPDATE personal_board_preferences SET right_panel='hidden' WHERE right_panel='schedule';")
    op.execute(
        "ALTER TABLE personal_board_preferences "
        "DROP CONSTRAINT IF EXISTS personal_board_preferences_right_panel_check;"
    )
    op.execute(
        "ALTER TABLE personal_board_preferences "
        "ADD CONSTRAINT personal_board_preferences_right_panel_check "
        "CHECK (right_panel IN ('backlog', 'integrations', 'hidden'));"
    )
    # 좁히기 전에 'time' row를 manual로 되돌려야 한다(아니면 옛 제약 재추가가 실패).
    op.execute("UPDATE personal_board_preferences SET sort_mode='manual' WHERE sort_mode='time';")
    op.execute(
        "ALTER TABLE personal_board_preferences "
        "DROP CONSTRAINT IF EXISTS personal_board_preferences_sort_mode_check;"
    )
    op.execute(
        "ALTER TABLE personal_board_preferences "
        "ADD CONSTRAINT personal_board_preferences_sort_mode_check "
        "CHECK (sort_mode IN ('manual', 'priority'));"
    )
    op.execute("ALTER TABLE personal_board_tasks DROP COLUMN scheduled_end_time;")
