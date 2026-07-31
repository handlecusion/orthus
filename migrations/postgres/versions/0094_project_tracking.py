"""프로젝트 트래킹 필드 + 활동 로그.

세계급 프로젝트 관리(기한/오너/건강 신호 + 누가-뭘-언제)의 토대:
- `dashboard_projects`에 start_date/target_date(기간), owner_member_id(DRI),
  health(on_track|at_risk|off_track), updated_at을 추가한다.
- `project_activity`는 프로젝트 필드 변경·요구조건·배정·보드 행 생성/삭제를
  append-only로 남기는 활동 피드다(에이전트/사람 공통, actor는 users.user_id).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0094_project_tracking"
down_revision = "0093_personal_task_note_comments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dashboard_projects", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("dashboard_projects", sa.Column("target_date", sa.Date(), nullable=True))
    op.add_column(
        "dashboard_projects",
        sa.Column("owner_member_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("dashboard_projects", sa.Column("health", sa.Text(), nullable=True))
    op.add_column(
        "dashboard_projects",
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    op.create_table(
        "project_activity",
        sa.Column("activity_id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=True),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_project_activity_project",
        "project_activity",
        ["node_id", "project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_activity_project", table_name="project_activity")
    op.drop_table("project_activity")
    op.drop_column("dashboard_projects", "updated_at")
    op.drop_column("dashboard_projects", "health")
    op.drop_column("dashboard_projects", "owner_member_id")
    op.drop_column("dashboard_projects", "target_date")
    op.drop_column("dashboard_projects", "start_date")
