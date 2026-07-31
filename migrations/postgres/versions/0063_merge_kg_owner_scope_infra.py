"""K7.1 — merge kg owner-scope head with main infra-dashboard head.

main이 분기 후 16커밋 전진하며 infra 대시보드 마이그레이션 체인
(`0059_infra_period_dash`→`0060`→`0061`→`0062_infra_color_currency`)을 추가했다.
K7.1 브랜치는 자체 head `0059_merge_kg_owner_scope_board`를 들고 있어 origin/main 머지
후 head가 둘이 된다. 스키마 변경 없는 no-op merge로 단일 head로 합친다
(alembic merge heads 동형 — 0059_merge가 board 체인을 합쳤던 것과 같은 패턴).

Revision ID: 0063_merge_kg_owner_scope_infra
Revises: 0059_merge_kg_owner_scope_board, 0062_infra_color_currency
"""

from __future__ import annotations

revision = "0063_merge_kg_owner_scope_infra"
down_revision = ("0059_merge_kg_owner_scope_board", "0062_infra_color_currency")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
