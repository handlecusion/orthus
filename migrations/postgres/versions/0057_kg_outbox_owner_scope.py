"""kg_outbox owner-scope columns — K7.1 (kg-k7-plan §2)

K7.1의 exact-scope worker DELETE는 enqueue된 이벤트에서 scope/owner_id를 읽어
"이 owner의 노드만" 지운다. 하지만 kg_outbox(0047)는 entity_kind/entity_id/op/
correlation_id만 들고 있었다. scope/owner_id 컬럼을 추가한다.

Pre-migration in-flight 이벤트는 scope='company'로 기본값이 들어간다 — **safe**.
그 이벤트들은 옛 `if scope != "company": return` enqueue no-op 아래에서 company-only
였기 때문이다(kg-k7-plan §2 / `test_enqueue_noops_on_personal_today`).

현재 단일 head는 0049/0055를 합친 merge migration(2a08d980700a) 위의
0056_rekey_weekly_sunday다. 그 위에 매단다(0056_ prefix는 이미 점유).

Revision ID: 0057_kg_outbox_owner_scope
Revises: 0056_rekey_weekly_sunday
Create Date: 2026-06-15
"""

from __future__ import annotations

from alembic import op

revision = "0057_kg_outbox_owner_scope"
down_revision = "0056_rekey_weekly_sunday"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE kg_outbox ADD COLUMN scope TEXT NOT NULL DEFAULT 'company';")
    op.execute("ALTER TABLE kg_outbox ADD COLUMN owner_id UUID NULL;")


def downgrade() -> None:
    op.execute("ALTER TABLE kg_outbox DROP COLUMN owner_id;")
    op.execute("ALTER TABLE kg_outbox DROP COLUMN scope;")
