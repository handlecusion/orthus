"""collector device_id — per-token machine identity (multi-device collectors)

한 유저 계정이 여러 머신(Mac mini + MacBook)에서 collector daemon을 충돌 없이
돌릴 수 있게 한다. 지금까지 `node_id`는 central 서버 identity(서버당 상수)라
한 유저의 모든 daemon이 같은 command queue를 공유하고 같은 결정론 connector
account_id로 수렴해 설정 덮어쓰기/claim 경쟁이 났다. per-token `device_id`를
추가해 server-side로 connector account + command routing을 분리한다.

backcompat: `device_id == ''`(빈 값)이면 기존 동작과 byte-identical하다 — 기존
결정론 account_id와 기존 row가 동일하게 유지된다. partial unique index는
device_id가 비어 있지 않은 LIVE 토큰만 (user_id, node_id, device_id)로 막아,
legacy device_id='' row는 서로 충돌하지 않는다.

Revision ID: 0072_collector_device_id
Revises: 0071_seed_company_kpis
Create Date: 2026-06-21
"""

from __future__ import annotations

from alembic import op

revision = "0072_collector_device_id"
down_revision = "0071_seed_company_kpis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE collector_tokens ADD COLUMN device_id TEXT NOT NULL DEFAULT '';")
    op.execute(
        "CREATE UNIQUE INDEX uq_collector_tokens_active_device "
        "ON collector_tokens (user_id, node_id, device_id) "
        "WHERE revoked_at IS NULL AND device_id <> '';"
    )
    op.execute("ALTER TABLE collector_commands ADD COLUMN device_id TEXT NULL;")
    op.execute("ALTER TABLE connector_accounts ADD COLUMN device_id TEXT NOT NULL DEFAULT '';")


def downgrade() -> None:
    op.execute("ALTER TABLE connector_accounts DROP COLUMN device_id;")
    op.execute("ALTER TABLE collector_commands DROP COLUMN device_id;")
    op.execute("DROP INDEX IF EXISTS uq_collector_tokens_active_device;")
    op.execute("ALTER TABLE collector_tokens DROP COLUMN device_id;")
