"""(sanitized) demo-data seed removed for the competition build.

이 리비전은 원래 회사 내부 데모 데이터를 시드했다. 대회용 공개 빌드에서는
내부 데이터를 배포하지 않으므로 데이터 시드를 제거하고 리비전 체인만 유지한다.
스키마 변경은 없다.

Revision ID: 0037_seed_partners
Revises: 0036_partner_companies
"""

from __future__ import annotations

revision = "0037_seed_partners"
down_revision = "0036_partner_companies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
