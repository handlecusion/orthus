"""K7.1 — merge kg owner-scope head with main board head.

동시 브랜치로 두 head가 생겼다: owner-scope `0057_kg_outbox_owner_scope`와 main의
board 체인 head `0057_board_team_event_unique`가 둘 다 `0056_rekey_weekly_sunday`에서
갈라졌다. 스키마 변경 없는 no-op merge로 단일 head로 합친다(alembic merge heads 동형).

Revision ID: 0059_merge_kg_owner_scope_board
Revises: 0057_kg_outbox_owner_scope, 0057_board_team_event_unique
"""

from __future__ import annotations

revision = "0059_merge_kg_owner_scope_board"
down_revision = ("0057_kg_outbox_owner_scope", "0057_board_team_event_unique")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
