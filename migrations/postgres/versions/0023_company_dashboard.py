"""company dashboard: team, finance, plans/retros, calendar, culture

Revision ID: 0023_company_dashboard
Revises: 0023_auth_magic_links
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0023_company_dashboard"
down_revision = "0023_auth_magic_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- 팀 정보 / 개인정보 ---
    op.execute(
        """
        CREATE TABLE team_members (
          member_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          user_id UUID REFERENCES users(user_id),
          name TEXT NOT NULL,
          title TEXT,
          department TEXT,
          email TEXT,
          phone TEXT,
          join_date DATE,
          birthday DATE,
          address TEXT,
          emergency_contact TEXT,
          color TEXT,
          bio TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          active BOOLEAN NOT NULL DEFAULT true,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_team_members_node ON team_members(node_id, active, sort_order);"
    )

    # --- 계획/회고용 프로젝트 마스터 ---
    op.execute(
        """
        CREATE TABLE dashboard_projects (
          project_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          name TEXT NOT NULL,
          color TEXT,
          sort_order INTEGER NOT NULL DEFAULT 0,
          active BOOLEAN NOT NULL DEFAULT true,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (node_id, name)
        );
        """
    )

    # --- 주간 계획 + 회고 (월~일, week_start = 월요일) ---
    op.execute(
        """
        CREATE TABLE weekly_entries (
          entry_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          project_id UUID NOT NULL REFERENCES dashboard_projects(project_id) ON DELETE CASCADE,
          week_start DATE NOT NULL,
          plan_items JSONB NOT NULL DEFAULT '[]'::jsonb,
          retro_items JSONB NOT NULL DEFAULT '[]'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (node_id, project_id, week_start)
        );
        """
    )

    # --- 월간 계획 + 회고 (month = 해당 월 1일) ---
    op.execute(
        """
        CREATE TABLE monthly_entries (
          entry_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          project_id UUID NOT NULL REFERENCES dashboard_projects(project_id) ON DELETE CASCADE,
          month DATE NOT NULL,
          plan_items JSONB NOT NULL DEFAULT '[]'::jsonb,
          retro_items JSONB NOT NULL DEFAULT '[]'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (node_id, project_id, month)
        );
        """
    )

    # --- 팀 일정 (팀원별 일정을 한 달력에) ---
    op.execute(
        """
        CREATE TABLE team_calendar_events (
          event_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          member_id UUID REFERENCES team_members(member_id) ON DELETE SET NULL,
          title TEXT NOT NULL,
          description TEXT,
          all_day BOOLEAN NOT NULL DEFAULT true,
          event_date DATE NOT NULL,
          end_date DATE,
          starts_at TIMESTAMPTZ,
          ends_at TIMESTAMPTZ,
          event_type TEXT NOT NULL DEFAULT 'event'
            CHECK (event_type IN ('event', 'meeting', 'vacation', 'wfh', 'holiday', 'deadline')),
          color TEXT,
          created_by UUID,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_team_calendar_events_range ON team_calendar_events(node_id, event_date);"
    )

    # --- 자금: 구독 ---
    op.execute(
        """
        CREATE TABLE finance_subscriptions (
          sub_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          name TEXT NOT NULL,
          vendor TEXT,
          plan TEXT,
          billing_cycle TEXT NOT NULL DEFAULT 'monthly'
            CHECK (billing_cycle IN ('monthly', 'yearly', 'one_time')),
          amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
          currency TEXT NOT NULL DEFAULT 'KRW',
          next_billing_date DATE,
          status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'trial', 'paused', 'canceled')),
          category TEXT,
          owner_member_id UUID REFERENCES team_members(member_id) ON DELETE SET NULL,
          masked_account TEXT,
          notes TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # --- 자금: API 키 (메타데이터만, 실제 값 저장 안 함) ---
    op.execute(
        """
        CREATE TABLE finance_api_keys (
          key_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          service_name TEXT NOT NULL,
          label TEXT,
          key_last4 TEXT,
          environment TEXT NOT NULL DEFAULT 'prod'
            CHECK (environment IN ('prod', 'dev', 'staging')),
          monthly_cost NUMERIC(14, 2) NOT NULL DEFAULT 0,
          currency TEXT NOT NULL DEFAULT 'KRW',
          status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'revoked', 'expired')),
          rotated_at DATE,
          owner_member_id UUID REFERENCES team_members(member_id) ON DELETE SET NULL,
          notes TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # --- 자금: 계정 관리 (메타데이터만) ---
    op.execute(
        """
        CREATE TABLE finance_accounts (
          account_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          account_name TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'login'
            CHECK (kind IN ('bank', 'card', 'saas', 'login', 'other')),
          masked_identifier TEXT,
          owner_member_id UUID REFERENCES team_members(member_id) ON DELETE SET NULL,
          notes TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # --- 자금: 원장 (매출/지출 → 매출현황·잔액) ---
    op.execute(
        """
        CREATE TABLE finance_ledger (
          ledger_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          entry_date DATE NOT NULL,
          entry_type TEXT NOT NULL
            CHECK (entry_type IN ('revenue', 'expense')),
          amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
          currency TEXT NOT NULL DEFAULT 'KRW',
          category TEXT,
          description TEXT,
          project_id UUID REFERENCES dashboard_projects(project_id) ON DELETE SET NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_finance_ledger_node_date ON finance_ledger(node_id, entry_date);"
    )

    # --- 컬처: 노드당 1행 설정 (사무실/WiFi/근무시간 등 display-only) ---
    op.execute(
        """
        CREATE TABLE company_culture (
          node_id TEXT PRIMARY KEY,
          content JSONB NOT NULL DEFAULT '{}'::jsonb,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS company_culture;")
    op.execute("DROP INDEX IF EXISTS idx_finance_ledger_node_date;")
    op.execute("DROP TABLE IF EXISTS finance_ledger;")
    op.execute("DROP TABLE IF EXISTS finance_accounts;")
    op.execute("DROP TABLE IF EXISTS finance_api_keys;")
    op.execute("DROP TABLE IF EXISTS finance_subscriptions;")
    op.execute("DROP INDEX IF EXISTS idx_team_calendar_events_range;")
    op.execute("DROP TABLE IF EXISTS team_calendar_events;")
    op.execute("DROP TABLE IF EXISTS monthly_entries;")
    op.execute("DROP TABLE IF EXISTS weekly_entries;")
    op.execute("DROP TABLE IF EXISTS dashboard_projects;")
    op.execute("DROP INDEX IF EXISTS idx_team_members_node;")
    op.execute("DROP TABLE IF EXISTS team_members;")
