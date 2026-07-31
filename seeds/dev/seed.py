"""Dev seed: one demo user + a few archive documents. Idempotent — safe to re-run.

`data_sources` was dropped in migration 0005 (replaced by `notion_rows` JSONB
catalog built in `orthus.structured.query.build_notion_catalog` at query time)
so this seed no longer touches it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, insert, select

from orthus.db import session
from orthus.documents import save_editor_document
from orthus.tables import documents, users
from seeds.dev.dashboard_seed import seed_dashboard

DEMO_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")

_DEMO_DOCS = [
    (
        "휴가 정책",
        "연차 휴가는 입사 1년차부터 15일이 부여됩니다.\n\n병가는 연 5일까지 무급으로 사용할 수 있습니다.\n\n재택근무는 주 2회까지 사전 승인 후 가능합니다.",
    ),
    (
        "경비 처리",
        "법인카드 사용 후 영수증은 3일 이내 제출합니다.\n\n식대는 1인 1만 5천원까지 인정됩니다.\n\n출장 경비는 사전 승인이 필요합니다.",
    ),
    (
        "보안 수칙",
        "사내 VPN 접속은 2단계 인증이 필요합니다.\n\n비밀번호는 90일마다 변경합니다.\n\n외부 공유 시 문서 등급을 확인합니다.",
    ),
]


def seed() -> None:
    with session() as s:
        if not s.execute(select(users.c.user_id).where(users.c.user_id == DEMO_USER_ID)).first():
            s.execute(
                insert(users).values(
                    user_id=DEMO_USER_ID,
                    display_name="Demo User",
                    preferred_timezone="Asia/Seoul",
                )
            )
        s.commit()

        has_docs = s.execute(
            select(func.count()).select_from(documents).where(documents.c.user_id == DEMO_USER_ID)
        ).scalar_one()

    if not has_docs:
        for title, md in _DEMO_DOCS:
            save_editor_document(DEMO_USER_ID, title, [{"type": "paragraph"}], md)

    with session() as s:
        n_docs = s.execute(
            select(func.count()).select_from(documents).where(documents.c.user_id == DEMO_USER_ID)
        ).scalar_one()
    print(f"seeded: user={DEMO_USER_ID} docs={n_docs}")
    seed_dashboard()


if __name__ == "__main__":
    seed()
