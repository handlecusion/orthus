"""project role options — 노션식 선택 속성처럼 역할 옵션 추가/이름변경/색/삭제

대시보드 프로젝트의 멤버 역할(project_assignments.role)은 자유 텍스트였고, 드롭다운
후보는 FE 하드코딩 목록이었다. 이제 노드별 역할 옵션을 DB로 관리한다(이름·색·정렬).
이름을 바꾸면 그 역할을 쓰던 배정에 cascade 반영하고, 삭제하면 해당 배정의 role을
비운다(서비스 레이어에서 처리).

backcompat: project_assignments.role은 그대로 자유 텍스트다. project_roles는 드롭다운
옵션의 SoR일 뿐이고, 기존 배정/역할 값은 건드리지 않는다. 첫 list 시 기존 FE 기본
역할이 노드에 시드된다.

Revision ID: 0078_project_roles
Revises: 0077_personal_board_channels
Create Date: 2026-06-27
"""

from __future__ import annotations

from alembic import op

revision = "0078_project_roles"
down_revision = "0077_personal_board_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE project_roles (
          role_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          name TEXT NOT NULL,
          color TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (node_id, name)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_roles;")
