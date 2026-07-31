"""support programs: seed sort_order for non-시작전 columns

시작 전 컬럼은 마감일 순으로 보여주지만, 다른 컬럼(서류제출완/발표준비/합격/
유감/협약취소)은 sort_order(상태변경순 + 드래그 정렬)로 보여준다. 기존 행들은
sort_order가 0으로 몰려 있어 정렬이 무의미하므로, 컬럼별로 현재 마감일 순서를
sort_order에 시드해 전환 시점의 화면 순서를 보존하고 이후 드래그로 조정할 수 있게
한다. status 자체는 TEXT라 협약취소 추가에는 스키마 변경이 필요 없다.

Revision ID: 0051_support_seed_sort_order
Revises: 0050_merge_atlas_event_types
Create Date: 2026-06-13
"""

from __future__ import annotations

from alembic import op

revision = "0051_support_seed_sort_order"
down_revision = "0050_merge_atlas_event_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH ordered AS (
          SELECT program_id,
                 ROW_NUMBER() OVER (
                   PARTITION BY node_id, status
                   ORDER BY deadline ASC NULLS LAST, created_at ASC
                 ) - 1 AS rn
          FROM support_programs
          WHERE status <> '시작 전'
        )
        UPDATE support_programs sp
        SET sort_order = ordered.rn
        FROM ordered
        WHERE sp.program_id = ordered.program_id;
        """
    )


def downgrade() -> None:
    # 정렬 시드는 비가역(원래 sort_order를 보존하지 않는다).
    pass
