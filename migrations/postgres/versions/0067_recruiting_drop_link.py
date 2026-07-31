"""recruiting candidates: drop separate link column (folded into note)

Revision ID: 0067_recruiting_drop_link
Revises: 0066_recruiting_comments
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067_recruiting_drop_link"
down_revision = "0066_recruiting_comments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("recruiting_candidates", "link")


def downgrade() -> None:
    op.add_column("recruiting_candidates", sa.Column("link", sa.Text()))
