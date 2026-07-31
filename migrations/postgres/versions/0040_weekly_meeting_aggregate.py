"""drop per-project auto meetings (now aggregated per week/month)

계획·회고 자동 내부 회의를 프로젝트별 1행에서 주/월 단위 집계 1행으로 바꾼다.
옛 형식 행은 source_ref가 '{project_uuid}:{date}' 라 ':'를 포함한다. 집계 행은
source_ref가 날짜뿐이다. 옛 행만 제거하고, 집계 행은 다음 계획·회고 저장 시
(또는 resync_internal_meetings) 재생성된다.

Revision ID: 0040_weekly_meeting_aggregate
Revises: 0039_meeting_kind_source
Create Date: 2026-06-09
"""

from __future__ import annotations

from alembic import op

revision = "0040_weekly_meeting_aggregate"
down_revision = "0039_meeting_kind_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM meeting_notes "
        "WHERE source IN ('weekly_plan', 'monthly_plan') AND source_ref LIKE '%:%';"
    )


def downgrade() -> None:
    # one-way cleanup; aggregate rows are derived from plan/retro entries.
    pass
