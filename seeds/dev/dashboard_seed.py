"""Dev seed for the company dashboard. Idempotent — only fills empty tables so
re-running never duplicates rows. Company node only (matches settings.node_id)."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select

from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import (
    company_culture,
    dashboard_projects,
    finance_ledger,
    finance_subscriptions,
    team_members,
)

_MEMBERS = [
    ("김대표", "대표", "#FF8243"),
    ("이개발", "팀원", "#62c4ba"),
    ("박기획", "팀원", "#9b68d6"),
]
_PROJECTS = [("Nova", "#5d9bd8"), ("Atlas", "#efc47b"), ("회사", "#9fb4bf")]
_CULTURE = {
    "office_address": "서울특별시 어딘가구 데모로 1 (데모워크스페이스) 101호",
    "postal_code": "00000",
    "wifi_ssid": "DEMO WORKSPACE 2G / DEMO WORKSPACE 5G",
    "wifi_password": "demo-password",
    "work_hours": "월~금 09:00~18:00",
    "work_note": "휴무가 필요하면 일주일 전에 미리 공유해주세요.",
}


def _empty(s, table) -> bool:
    return (
        s.execute(
            select(func.count()).select_from(table).where(table.c.node_id == node_id())
        ).scalar_one()
        == 0
    )


def node_id() -> str:
    return get_settings().node_id


def seed_dashboard() -> None:
    settings = get_settings()
    if settings.node_kind != "company":
        print(f"dashboard seed skipped (node_kind={settings.node_kind})")
        return
    nid = settings.node_id
    with session() as s:
        if _empty(s, team_members):
            for i, (name, title, color) in enumerate(_MEMBERS):
                s.execute(
                    team_members.insert().values(
                        member_id=uuid.uuid4(),
                        node_id=nid,
                        name=name,
                        title=title,
                        color=color,
                        sort_order=i,
                    )
                )
        if _empty(s, dashboard_projects):
            for i, (name, color) in enumerate(_PROJECTS):
                s.execute(
                    dashboard_projects.insert().values(
                        project_id=uuid.uuid4(),
                        node_id=nid,
                        name=name,
                        color=color,
                        sort_order=i,
                    )
                )
        if _empty(s, finance_subscriptions):
            s.execute(
                finance_subscriptions.insert().values(
                    sub_id=uuid.uuid4(),
                    node_id=nid,
                    name="Notion",
                    vendor="Notion Labs",
                    billing_cycle="monthly",
                    amount=12000,
                    currency="KRW",
                    status="active",
                    category="협업 도구",
                )
            )
        if _empty(s, finance_ledger):
            s.execute(
                finance_ledger.insert().values(
                    ledger_id=uuid.uuid4(),
                    node_id=nid,
                    entry_date=date(2026, 6, 1),
                    entry_type="revenue",
                    amount=5000000,
                    currency="KRW",
                    category="매출",
                    description="6월 매출",
                )
            )
            s.execute(
                finance_ledger.insert().values(
                    ledger_id=uuid.uuid4(),
                    node_id=nid,
                    entry_date=date(2026, 6, 2),
                    entry_type="expense",
                    amount=1200000,
                    currency="KRW",
                    category="운영비",
                    description="6월 운영비",
                )
            )
        if (
            s.execute(
                select(func.count())
                .select_from(company_culture)
                .where(company_culture.c.node_id == nid)
            ).scalar_one()
            == 0
        ):
            s.execute(company_culture.insert().values(node_id=nid, content=_CULTURE))
        s.commit()
    print(f"dashboard seeded: node={nid}")


if __name__ == "__main__":
    seed_dashboard()
