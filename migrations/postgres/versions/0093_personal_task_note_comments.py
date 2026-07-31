"""내 보드 티켓 노트 영속화 + 티켓 댓글 테이블.

티켓 상세 팝업의 Notes/Comment는 지금까지 FE 메모리 상태로만 존재해 새로고침/
앱 재시작 시 사라졌다(#126부터). note는 task 행 컬럼으로, 댓글은 append 테이블로
서버에 저장한다.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0093_personal_task_note_comments"
down_revision = "0092_calendar_recurrence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("personal_board_tasks", sa.Column("note", sa.Text(), nullable=True))
    op.create_table(
        "personal_board_task_comments",
        sa.Column("comment_id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["personal_board_tasks.task_id"],
            name="fk_personal_board_task_comments_task",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_personal_board_task_comments_task",
        "personal_board_task_comments",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_personal_board_task_comments_task", table_name="personal_board_task_comments")
    op.drop_table("personal_board_task_comments")
    op.drop_column("personal_board_tasks", "note")
