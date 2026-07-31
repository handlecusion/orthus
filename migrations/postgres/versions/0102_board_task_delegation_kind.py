"""personal_board_tasks.source_kind CHECK에 'delegation' 추가

위임 agent_task dispatch가 assignee 개인 보드 backlog에 수신 위임 티켓을
system-created row로 남긴다(source_kind='delegation', calendar materialize
동형). 0013의 inline CHECK가 허용 목록을 고정하고 있어 값만 추가한다.

additive + backward-compatible: 기존 값은 전부 그대로 유효하다.

Revision ID: 0102_board_task_delegation_kind
Revises: 0101_agent_chat_message_kind
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op

revision = "0102_board_task_delegation_kind"
down_revision = "0101_agent_chat_message_kind"
branch_labels = None
depends_on = None

_KINDS_WITH_DELEGATION = (
    "('manual', 'github', 'gmail', 'notion', 'ai_session', 'calendar', 'delegation')"
)
_KINDS_ORIGINAL = "('manual', 'github', 'gmail', 'notion', 'ai_session', 'calendar')"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE personal_board_tasks"
        " DROP CONSTRAINT IF EXISTS personal_board_tasks_source_kind_check;"
    )
    op.execute(
        "ALTER TABLE personal_board_tasks"
        " ADD CONSTRAINT personal_board_tasks_source_kind_check"
        f" CHECK (source_kind IN {_KINDS_WITH_DELEGATION});"
    )


def downgrade() -> None:
    op.execute("DELETE FROM personal_board_tasks WHERE source_kind = 'delegation';")
    op.execute(
        "ALTER TABLE personal_board_tasks"
        " DROP CONSTRAINT IF EXISTS personal_board_tasks_source_kind_check;"
    )
    op.execute(
        "ALTER TABLE personal_board_tasks"
        " ADD CONSTRAINT personal_board_tasks_source_kind_check"
        f" CHECK (source_kind IN {_KINDS_ORIGINAL});"
    )
