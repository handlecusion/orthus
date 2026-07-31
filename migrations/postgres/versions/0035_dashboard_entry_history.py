"""dashboard weekly/monthly plan pre-overwrite history snapshot

Before any overwrite that changes weekly/monthly plan content, the upsert path
snapshots the PRIOR row content into `dashboard_entry_history` so an accidental
wipe (e.g. a client that never loaded the row) is recoverable. No FK / cascade —
history must survive project or entry deletion.

Revision ID: 0035_dashboard_entry_history
Revises: 0034_calendar_event_type_widen
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision = "0035_dashboard_entry_history"
down_revision = "0034_calendar_event_type_widen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE dashboard_entry_history (
          history_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          project_id UUID NOT NULL,
          period_kind TEXT NOT NULL,
          period DATE NOT NULL,
          plan_items JSONB NOT NULL DEFAULT '[]'::jsonb,
          retro_items JSONB NOT NULL DEFAULT '[]'::jsonb,
          prev_updated_at TIMESTAMPTZ,
          snapshot_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_dashboard_entry_history_lookup "
        "ON dashboard_entry_history (node_id, period_kind, project_id, period, snapshot_at DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dashboard_entry_history_lookup;")
    op.execute("DROP TABLE IF EXISTS dashboard_entry_history;")
