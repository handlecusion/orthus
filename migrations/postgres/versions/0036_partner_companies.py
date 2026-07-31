"""partner companies + contacts (dashboard 파트너사 관리)

회사 대시보드의 파트너사(보조출연사/제작사/AI 파운데이션 등)와 각 파트너사 소속
인물(담당자/직원)을 관리한다. org_type/status는 직접 추가를 허용하므로 CHECK 없음.
project_ids는 dashboard_projects.project_id 배열(멀티 프로젝트 태그).

Revision ID: 0036_partner_companies
Revises: 0035_dashboard_entry_history
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision = "0036_partner_companies"
down_revision = "0035_dashboard_entry_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE partner_companies (
          partner_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          name TEXT NOT NULL,
          org_type TEXT,
          address TEXT,
          representative TEXT,
          project_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
          field_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
          status TEXT,
          memo TEXT,
          next_action TEXT,
          last_contact DATE,
          link TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          active BOOLEAN NOT NULL DEFAULT true,
          source TEXT NOT NULL DEFAULT 'manual',
          source_ref TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE partner_contacts (
          contact_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          partner_id UUID NOT NULL
            REFERENCES partner_companies(partner_id) ON DELETE CASCADE,
          name TEXT NOT NULL,
          role TEXT,
          phone TEXT,
          email TEXT,
          channels JSONB NOT NULL DEFAULT '[]'::jsonb,
          link TEXT,
          memo TEXT,
          is_primary BOOLEAN NOT NULL DEFAULT false,
          sort_order INTEGER NOT NULL DEFAULT 0,
          source TEXT NOT NULL DEFAULT 'manual',
          source_ref TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_partner_companies_node ON partner_companies(node_id, sort_order);")
    op.execute(
        "CREATE INDEX idx_partner_contacts_partner ON partner_contacts(partner_id, sort_order);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_partner_contacts_partner;")
    op.execute("DROP INDEX IF EXISTS idx_partner_companies_node;")
    op.execute("DROP TABLE IF EXISTS partner_contacts;")
    op.execute("DROP TABLE IF EXISTS partner_companies;")
