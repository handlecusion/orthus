"""personal board channels: 개인/회사 채널 구분 + 회사 공개 업무

내 보드(개인 워크스페이스)의 채널을 개인/회사로 나눈다.

- personal_board_projects.kind: 'personal'(기본, 사적 채널) | 'company'(내가 담당인
  회사 프로젝트와 연결된 채널). company_project_id는 연결된 dashboard_projects.project_id.
  회사 채널은 사용자가 직접 만드는 게 아니라, 담당(project_assignments)이 있으면
  bootstrap에서 자동 생성/연결된다(이름이 같은 기존 개인 채널은 회사 채널로 전환).
- personal_board_tasks.scope: 'personal'(기본, 소유자 전용) | 'company'(회사 프로젝트
  채널 업무 → 회사 구성원 전체가 데이터로 조회). company_project_id로 회사쪽에서
  프로젝트 단위 집계.

backcompat: kind/scope는 NOT NULL DEFAULT 'personal', company_project_id는 NULL이라
기존 row는 전부 사적 채널 / 소유자 전용 업무로 그대로 유지된다(동작 불변). 회사
채널은 회사 노드 + owner-scope에서 담당이 있을 때만 나타난다.

Revision ID: 0077_personal_board_channels
Revises: 0076_personal_board_due
Create Date: 2026-06-27
"""

from __future__ import annotations

from alembic import op

revision = "0077_personal_board_channels"
down_revision = "0076_personal_board_due"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE personal_board_projects "
        "ADD COLUMN kind TEXT NOT NULL DEFAULT 'personal';"
    )
    op.execute("ALTER TABLE personal_board_projects ADD COLUMN company_project_id UUID;")
    op.execute(
        "ALTER TABLE personal_board_tasks ADD COLUMN scope TEXT NOT NULL DEFAULT 'personal';"
    )
    op.execute("ALTER TABLE personal_board_tasks ADD COLUMN company_project_id UUID;")
    # 회사쪽에서 프로젝트별 회사 공개(scope='company') 업무를 모을 때 쓰는 인덱스.
    op.execute(
        "CREATE INDEX ix_personal_board_tasks_company_project "
        "ON personal_board_tasks (company_project_id, scope);"
    )
    # 채널 이름 유니크를 '활성(archived_at IS NULL)' 행으로 한정한다. 기존 전역 UNIQUE는
    # 소프트삭제된 동명 채널까지 덮어, 담당 회사 프로젝트 이름과 충돌하면 삭제된 채널이
    # 되살아나며 그 안의 개인 업무가 회사로 새는 경로가 있었다(채널 동기화 upsert). 활성
    # 행만 유니크로 묶으면 동명이라도 '활성 1개 + 삭제 N개'가 공존해 되살리기를 막는다.
    op.execute(
        "ALTER TABLE personal_board_projects "
        "DROP CONSTRAINT IF EXISTS personal_board_projects_workspace_id_name_key;"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_personal_board_projects_active_name "
        "ON personal_board_projects (workspace_id, name) WHERE archived_at IS NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_personal_board_projects_active_name;")
    op.execute(
        "ALTER TABLE personal_board_projects "
        "ADD CONSTRAINT personal_board_projects_workspace_id_name_key UNIQUE (workspace_id, name);"
    )
    op.execute("DROP INDEX IF EXISTS ix_personal_board_tasks_company_project;")
    op.execute("ALTER TABLE personal_board_tasks DROP COLUMN company_project_id;")
    op.execute("ALTER TABLE personal_board_tasks DROP COLUMN scope;")
    op.execute("ALTER TABLE personal_board_projects DROP COLUMN company_project_id;")
    op.execute("ALTER TABLE personal_board_projects DROP COLUMN kind;")
