"""recruiting candidates: new status values (컨택전/컨택중/프로젝트/보류/취소)

Revision ID: 0069_recruiting_status_values
Revises: 0068_seed_recruiting_as
Create Date: 2026-06-21

상태 값을 컨택전/컨택중/프로젝트/보류/취소로 바꾼다. 기존 값은 매핑한다:
미발송→컨택전, 발송/회신→컨택중, 종료→취소, 보류 유지. 컬럼 기본값도 컨택전으로.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "0069_recruiting_status_values"
down_revision = "0068_seed_recruiting_as"
branch_labels = None
depends_on = None

_FORWARD = {
    "미발송": "컨택전",
    "발송": "컨택중",
    "회신": "컨택중",
    "종료": "취소",
}
_BACK = {
    "컨택전": "미발송",
    "컨택중": "발송",
    "프로젝트": "회신",
    "취소": "종료",
}


def _remap(mapping: dict[str, str]) -> None:
    bind = op.get_bind()
    for old, new in mapping.items():
        bind.execute(
            text(
                "UPDATE recruiting_candidates SET send_status = :new "
                "WHERE send_status = :old"
            ),
            {"old": old, "new": new},
        )


def upgrade() -> None:
    _remap(_FORWARD)
    op.alter_column(
        "recruiting_candidates", "send_status", server_default="컨택전"
    )


def downgrade() -> None:
    _remap(_BACK)
    op.alter_column(
        "recruiting_candidates", "send_status", server_default="미발송"
    )
