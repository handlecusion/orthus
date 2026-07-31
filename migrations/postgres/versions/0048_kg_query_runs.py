"""KG read-gate run log (K4)

Revision ID: 0048_kg_query_runs
Revises: 0047_collector_liveness
Create Date: 2026-06-12

K4 읽기 게이트의 모든 실행(reject 포함)을 기록한다 — structured `query_runs`와
동형이지만 별도 테이블이다(기존 query_runs는 확장하지 않는다, docs/kg-model.md §4).
"""

from __future__ import annotations

from alembic import op

revision = "0048_kg_query_runs"
down_revision = "0047_collector_liveness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE kg_query_runs (
          run_id          UUID PRIMARY KEY,
          template_name   TEXT NOT NULL,
          params_redacted JSONB NOT NULL DEFAULT '{}'::jsonb,
          status          TEXT NOT NULL CHECK (status IN ('ok','rejected','timeout','error')),
          reject_reason   TEXT NULL,
          duration_ms     INT NULL,
          result_count    INT NULL,
          user_id         UUID NULL,
          correlation_id  UUID NULL,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_kg_query_runs_created ON kg_query_runs (created_at);")
    op.execute("CREATE INDEX idx_kg_query_runs_template ON kg_query_runs (template_name);")


def downgrade() -> None:
    op.execute("DROP TABLE kg_query_runs;")
