"""KPI OKR 채점 + 주간 신뢰도 체크인

계획·회고 ↔ KPI 운영 루프(달성도 스코어링):

- `dashboard_kpis.grade` 외 3컬럼: 주기말 공식 채점(0~10 정수, NULL=미채점 —
  0은 유효한 점수). 재채점은 덮어쓰기(graded_at/graded_by 갱신)이고 이력
  테이블은 두지 않는다(소규모 팀, grade_note가 근거 담당).
- `dashboard_kpi_confidence`: 진행 중 KR에 대한 주간 자신감 1~10. 주는
  weekly_entries와 같은 일요일 시작으로 정규화하고, (node, kpi, week) 당
  1값 upsert라 스파크라인이 주 단위로 자연히 정렬된다.
- 계획 항목별 달성 점수는 PlanItem JSONB `score` 키로 탑승(assignee_member_id/
  kpi_id 선례) — DDL 불필요.

Revision ID: 0099_kpi_scoring
Revises: 0098_personal_notes
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op

revision = "0099_kpi_scoring"
down_revision = "0098_personal_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE dashboard_kpis ADD COLUMN IF NOT EXISTS grade INT;")
    op.execute("ALTER TABLE dashboard_kpis ADD COLUMN IF NOT EXISTS grade_note TEXT;")
    op.execute("ALTER TABLE dashboard_kpis ADD COLUMN IF NOT EXISTS graded_at TIMESTAMPTZ;")
    op.execute(
        "ALTER TABLE dashboard_kpis ADD COLUMN IF NOT EXISTS graded_by UUID "
        "REFERENCES team_members(member_id) ON DELETE SET NULL;"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_kpi_confidence (
          confidence_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          kpi_id UUID NOT NULL REFERENCES dashboard_kpis(kpi_id) ON DELETE CASCADE,
          week_start DATE NOT NULL,
          confidence INT NOT NULL,
          note TEXT,
          author_member_id UUID REFERENCES team_members(member_id) ON DELETE SET NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (node_id, kpi_id, week_start)
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_kpi_confidence_kpi "
        "ON dashboard_kpi_confidence (node_id, kpi_id, week_start);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dashboard_kpi_confidence;")
    op.execute("ALTER TABLE dashboard_kpis DROP COLUMN IF EXISTS graded_by;")
    op.execute("ALTER TABLE dashboard_kpis DROP COLUMN IF EXISTS graded_at;")
    op.execute("ALTER TABLE dashboard_kpis DROP COLUMN IF EXISTS grade_note;")
    op.execute("ALTER TABLE dashboard_kpis DROP COLUMN IF EXISTS grade;")
