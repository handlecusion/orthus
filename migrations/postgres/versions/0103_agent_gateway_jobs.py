"""agent_gateway_jobs — P10 C-7 게이트웨이 잡 정의 central 미러 테이블

데몬 로컬 SQLite(`agent_jobs`)가 실행 SoR이고, 이 테이블은 잡 **정의**
(스케줄·지시·enabled·last_status)를 owner-scoped rows로 미러한 **조회 전용**
사본이다(spec §9). seen-set 항목은 민감 URL일 수 있어 미러하지 않고,
chat_id(텔레그램 식별자)도 조회 가치가 없어 미러하지 않는다. central 편집이
로컬 실행을 바꾸는 인바운드 경로는 만들지 않는다(단방향 push).

Revision ID: 0103_agent_gateway_jobs
Revises: 0102_board_task_delegation_kind
Create Date: 2026-07-17
"""

from __future__ import annotations

from alembic import op

revision = "0103_agent_gateway_jobs"
down_revision = "0102_board_task_delegation_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_gateway_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_user_id UUID NOT NULL,
            job_id TEXT NOT NULL,
            name TEXT NOT NULL,
            instruction TEXT NOT NULL,
            schedule JSONB NOT NULL,
            schedule_human TEXT,
            enabled BOOLEAN NOT NULL DEFAULT true,
            last_run_at TIMESTAMPTZ,
            last_status TEXT,
            last_error TEXT,
            next_run_at TIMESTAMPTZ,
            mirrored_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_agent_gateway_jobs_owner_job UNIQUE (owner_user_id, job_id)
        );
        """
    )
    op.execute("CREATE INDEX idx_agent_gateway_jobs_owner ON agent_gateway_jobs (owner_user_id);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_gateway_jobs;")
