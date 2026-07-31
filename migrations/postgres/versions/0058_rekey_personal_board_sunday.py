"""re-key personal board: Monday-start week → Sunday-start week

개인 보드(personal_weekly_objectives + objective subtasks/comments)의 주 시작을
월요일에서 일요일로 바꾼다. 회사 weekly_entries는 0056에서 이미 일요일로 옮겼고,
개인 보드만 월요일 기준으로 남아 있었다. FE/BE weekday 인덱스도 0=일요일로 통일한다.

마이그레이션(멱등):
- 월요일 키(EXTRACT DOW = 1) objective만 대상. 재실행 시 일요일 키(DOW=0)는 제외된다.
- day_allocations JSONB의 각 항목 weekday: new = (old + 1) % 7.
- 해당 objective의 subtasks/comments weekday도 동일 remap.
- 그 뒤 week_start -1일(같은 달력 주의 일요일)로 시프트.

주의: 옛 모델의 일요일 칸(old weekday 6 = 그 주의 후행 일요일)은 새 모델에서
선행 일요일(new weekday 0)로 들어가 달력상 7일 앞으로 당겨진다. 주간 상대 배치는
보존되며, 경계 일요일 할당만 슬롯이 한 칸 이동한다.

Revision ID: 0058_rekey_personal_board_sunday
Revises: 0056_rekey_weekly_sunday
Create Date: 2026-06-15
"""

from __future__ import annotations

import json

from alembic import op
from sqlalchemy import text

revision = "0058_rekey_personal_board_sunday"
down_revision = "0056_rekey_weekly_sunday"
branch_labels = None
depends_on = None


def _rekey(dow_filter: int, delta: int, week_shift: str) -> None:
    """dow_filter 요일 키 objective의 weekday를 +delta(mod 7) remap 후 week_start 시프트."""
    conn = op.get_bind()
    rows = conn.execute(
        text(
            "SELECT objective_id, day_allocations FROM personal_weekly_objectives "
            "WHERE EXTRACT(DOW FROM week_start) = :dow"
        ),
        {"dow": dow_filter},
    ).fetchall()
    for objective_id, allocations in rows:
        new_alloc = []
        for item in allocations or []:
            entry = dict(item)
            if entry.get("weekday") is not None:
                entry["weekday"] = (int(entry["weekday"]) + delta) % 7
            new_alloc.append(entry)
        conn.execute(
            text(
                "UPDATE personal_weekly_objectives "
                "SET day_allocations = CAST(:alloc AS jsonb) WHERE objective_id = :oid"
            ),
            {"alloc": json.dumps(new_alloc), "oid": objective_id},
        )
        conn.execute(
            text(
                "UPDATE personal_objective_subtasks "
                "SET weekday = (weekday + :delta) % 7 WHERE objective_id = :oid"
            ),
            {"delta": delta, "oid": objective_id},
        )
        conn.execute(
            text(
                "UPDATE personal_objective_comments "
                "SET weekday = (weekday + :delta) % 7 WHERE objective_id = :oid"
            ),
            {"delta": delta, "oid": objective_id},
        )
    conn.execute(
        text(
            "UPDATE personal_weekly_objectives "
            f"SET week_start = week_start {week_shift} "
            "WHERE EXTRACT(DOW FROM week_start) = :dow"
        ),
        {"dow": dow_filter},
    )


def upgrade() -> None:
    # 월요일(DOW=1) → 일요일: weekday +1(mod 7), week_start -1일.
    _rekey(dow_filter=1, delta=1, week_shift="- INTERVAL '1 day'")


def downgrade() -> None:
    # 일요일(DOW=0) → 월요일: weekday +6(≡ -1 mod 7), week_start +1일.
    _rekey(dow_filter=0, delta=6, week_shift="+ INTERVAL '1 day'")
