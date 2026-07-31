"""merge dashboard projects/memo work into main

Revision ID: 2a08d980700a
Revises: 0049_kg_entities, 0055_seed_company_dashboard_data
Create Date: 2026-06-14 13:31:35.456682
"""

from alembic import op
import sqlalchemy as sa


revision = '2a08d980700a'
down_revision = ('0049_kg_entities', '0055_seed_company_dashboard_data')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
