"""re-key weekly_entries: Monday-start week → Sunday-start week

계획·회고 FE가 주 시작을 월요일에서 일요일로 바꿨다(주 라벨 "일~토", 회사 일요일주
↔ 개인 월요일주 보정). 기존 weekly_entries는 모두 월요일 키라 새 일요일 키 조회에
매칭되지 않아 UI에서 사라진다(삭제는 아님). 이미 적용·배포된 0055를 수정하지 않고
별도 마이그레이션으로 기존 행 키를 하루 앞(일요일)으로 옮긴다.

- 월요일(EXTRACT DOW = 1) 행만 -1일 시프트 → 같은 달력 주의 일요일.
- UNIQUE(node_id, project_id, week_start) 충돌 없음: 시프트 전엔 일요일 행이
  없고, 모든 월요일이 고유 일요일로 일대일 이동한다.
- monthly_entries(month, 매월 1일)는 요일 기반이 아니라 무관 — 건드리지 않는다.

참고: weekly_plan 집계 internal meeting note(`sync_weekly_meeting`)는 옛 월요일
기준으로 만들어진 게 남을 수 있다. 다음 계획 저장 시 자동 재생성되며, 즉시 정리는
운영자가 `orthus.dashboard.resync_internal_meetings(node_id)`로 돌릴 수 있다.

Revision ID: 0056_rekey_weekly_sunday
Revises: 2a08d980700a
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op

revision = "0056_rekey_weekly_sunday"
# 현재 단일 head는 0049/0055를 합친 merge migration이다. 0055가 아니라 그 위에 매단다.
down_revision = "2a08d980700a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 월요일 키 → 일요일 키 (-1일). 일요일 행은 건드리지 않아 멱등하다.
    op.execute(
        "UPDATE weekly_entries "
        "SET week_start = week_start - INTERVAL '1 day' "
        "WHERE EXTRACT(DOW FROM week_start) = 1;"
    )


def downgrade() -> None:
    # 일요일 키 → 월요일 키 (+1일).
    op.execute(
        "UPDATE weekly_entries "
        "SET week_start = week_start + INTERVAL '1 day' "
        "WHERE EXTRACT(DOW FROM week_start) = 0;"
    )
