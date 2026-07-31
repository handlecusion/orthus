"""merge_kg_branches

Revision ID: fadb806ba302
Revises: 0047_kg_outbox, 0048_kg_query_runs
Create Date: 2026-06-13 00:39:45.504741
"""

from alembic import op
import sqlalchemy as sa


revision = 'fadb806ba302'
down_revision = ('0047_kg_outbox', '0048_kg_query_runs')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
