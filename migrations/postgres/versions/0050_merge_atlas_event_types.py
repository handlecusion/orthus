"""calendar: merge atlas event types into dev/update

팀 일정 카테고리에서 아틀라스 개발/아틀라스 업데이트를 없애고 기존 이벤트를
개발(dev)/업데이트(update)로 흡수한다.

Revision ID: 0050_merge_atlas_event_types
Revises: 0049_support_presentation_time
Create Date: 2026-06-13
"""

from __future__ import annotations

from alembic import op

revision = "0050_merge_atlas_event_types"
down_revision = "0049_support_presentation_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE team_calendar_events SET event_type='dev' WHERE event_type='atlas_dev';"
    )
    op.execute(
        "UPDATE team_calendar_events SET event_type='update' WHERE event_type='atlas_update';"
    )
    # 카테고리 없이 제목으로만 '아틀라스 개발/업데이트'를 기록해 온 기존 일정도
    # 같은 카테고리로 흡수한다 (제목은 그대로 둔다).
    op.execute(
        "UPDATE team_calendar_events SET event_type='dev' "
        "WHERE title ILIKE '%아틀라스 개발%' AND event_type <> 'dev';"
    )
    op.execute(
        "UPDATE team_calendar_events SET event_type='update' "
        "WHERE title ILIKE '%아틀라스 업데이트%' AND event_type <> 'update';"
    )


def downgrade() -> None:
    # 병합은 비가역 — 어떤 행이 아틀라스이었는지 구분할 수 없다.
    pass
