"""merge personal_board(channels/roles/sched) and ask_cache/ask_event branches

Revision ID: 9352b9615419
Revises: 0079_ask_event_jobs, 0079_personal_board_sched_end
Create Date: 2026-06-27 21:33:52.105898
"""

from alembic import op
import sqlalchemy as sa


revision = '9352b9615419'
down_revision = ('0079_ask_event_jobs', '0079_personal_board_sched_end')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
