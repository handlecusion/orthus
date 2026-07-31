"""team_members bank account (personal info)

Revision ID: 0028_team_member_bank_account
Revises: 0027_calendar_event_project
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0028_team_member_bank_account"
down_revision = "0027_calendar_event_project"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE team_members ADD COLUMN bank_account TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE team_members DROP COLUMN IF EXISTS bank_account;")
