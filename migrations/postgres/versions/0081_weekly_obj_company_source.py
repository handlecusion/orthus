"""personal_weekly_objectives: 회사 부여 항목 자동 반영 출처 컬럼

회사 주간계획(weekly_entries.plan_items)에서 이 사용자에게 부여된 항목을 개인 주간
플랜에 실제 personal_weekly_objective로 자동 생성(materialize)하기 위한 출처 컬럼이다.
sync_team_events_to_board(team_event_id 기준 멱등 미러)와 같은 패턴을 따른다.

- source_kind: 'manual'(기본, 사용자가 직접 만든 목표) | 'company_plan'(회사 부여 항목에서
  자동 생성된 목표). 백필 기본값이라 기존 row는 전부 'manual'로 동작 불변.
- source_plan_item_id: source_kind='company_plan'일 때 원본 weekly_entries.plan_items[i].id.
  멱등 upsert 키 + 회사쪽 미할당(unassign) 시 stale 제거의 매칭 키. manual 목표는 NULL.

day_allocations(개인이 소유하는 요일 분배)·완료·메모는 이 컬럼들과 무관하게 보존된다 —
재동기화는 제목/프로젝트만 갱신하고 그 외는 건드리지 않는다(미러 코드 책임).

Revision ID: 0081_weekly_obj_company_source
Revises: 0080_personal_board_no_return
Create Date: 2026-06-28
"""

from __future__ import annotations

from alembic import op

revision = "0081_weekly_obj_company_source"
down_revision = "0080_personal_board_no_return"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE personal_weekly_objectives "
        "ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'manual';"
    )
    op.execute(
        "ALTER TABLE personal_weekly_objectives "
        "ADD COLUMN IF NOT EXISTS source_plan_item_id TEXT;"
    )
    # 회사 부여 항목의 멱등 materialize용. 한 워크스페이스에서 같은 원본 plan_item이
    # 두 번 목표로 생기지 않게 partial UNIQUE로 잠근다(미러 코드가 ON CONFLICT DO UPDATE로
    # 재동기화). manual 목표(source_plan_item_id IS NULL)는 제약 밖.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_personal_weekly_objectives_source_item "
        "ON personal_weekly_objectives (workspace_id, source_plan_item_id) "
        "WHERE source_plan_item_id IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_personal_weekly_objectives_source_item;")
    op.execute(
        "ALTER TABLE personal_weekly_objectives DROP COLUMN IF EXISTS source_plan_item_id;"
    )
    op.execute("ALTER TABLE personal_weekly_objectives DROP COLUMN IF EXISTS source_kind;")
