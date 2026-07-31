"""objective per-day detail: weekday on subtasks and comments

Revision ID: 0043_objective_per_day_detail
Revises: 0042_objective_detail
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op

revision = "0043_objective_per_day_detail"
down_revision = "0042_objective_detail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE personal_objective_subtasks ADD COLUMN weekday INTEGER NOT NULL DEFAULT 0;"
    )
    op.execute(
        "ALTER TABLE personal_objective_comments ADD COLUMN weekday INTEGER NOT NULL DEFAULT 0;"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE personal_objective_comments DROP COLUMN IF EXISTS weekday;")
    op.execute("ALTER TABLE personal_objective_subtasks DROP COLUMN IF EXISTS weekday;")
