"""프로젝트 시스템 엔지니어링(SE) 레이어: 단계 + 요구조건 대장

SE-Seminar(V-모델, SRR→SDR→PDR→CDR 리뷰, 요구조건=제약조건+목표, 상위→하위
flow-down)를 프로젝트/하위 프로젝트 관리에 적용하기 위한 스키마다.

- dashboard_projects.se_stage: 현재 SE 단계 (srr|sdr|pdr|cdr|vnv|ops). NULL=미적용.
- project_requirements: 프로젝트별 번호 붙은 요구조건 대장.
  kind='constraint'(외부 제약조건) | 'goal'(프로젝트 목표/요구조건).
  status='open'(미충족)|'verifying'(검증 중)|'satisfied'(충족)|'failed'(실패).
  parent_requirement_id: 상위 프로젝트 요구조건에서 flow-down된 경우 원본 링크
  (SE의 시스템 요구조건 → 서브시스템 요구조건 분해).

Revision ID: 0091_project_se_requirements
Revises: 0090_project_parent
Create Date: 2026-07-02
"""

from __future__ import annotations

from alembic import op

revision = "0091_project_se_requirements"
down_revision = "0090_project_parent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE dashboard_projects ADD COLUMN IF NOT EXISTS se_stage TEXT;")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS project_requirements (
            requirement_id UUID PRIMARY KEY,
            node_id TEXT NOT NULL,
            project_id UUID NOT NULL REFERENCES dashboard_projects(project_id) ON DELETE CASCADE,
            num INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'goal',
            text TEXT NOT NULL,
            verify_method TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            parent_requirement_id UUID REFERENCES project_requirements(requirement_id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_project_requirements_project "
        "ON project_requirements (project_id);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_project_requirements_parent "
        "ON project_requirements (parent_requirement_id);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS project_requirements;")
    op.execute("ALTER TABLE dashboard_projects DROP COLUMN IF EXISTS se_stage;")
