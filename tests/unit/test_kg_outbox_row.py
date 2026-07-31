"""K7.1 — kg_outbox owner-scope 컬럼/OutboxRow 사상 (DB 불필요).

integration `test_kg_outbox_row_carries_scope_owner`(enqueue→claim 라운드트립)는
PG가 필요하지만, 여기서는 테이블 정의와 모델 기본값만 DB 없이 고정한다.
"""

from __future__ import annotations

import uuid

from orthus.kg.outbox import OutboxRow
from orthus.tables import kg_outbox


def test_kg_outbox_table_has_owner_scope_columns():
    cols = set(kg_outbox.c.keys())
    assert "scope" in cols
    assert "owner_id" in cols
    assert kg_outbox.c.scope.nullable is False  # NOT NULL DEFAULT 'company'
    assert kg_outbox.c.owner_id.nullable is True


def test_outbox_row_defaults_company():
    # claim() SELECT가 아직 scope/owner_id를 안 고르는 중간 상태에서도 깨지지 않게
    # 기본값이 company/None이다.
    row = OutboxRow(
        outbox_id=uuid.uuid4(),
        entity_kind="wiki_page",
        entity_id=uuid.uuid4(),
        op="upsert",
        attempts=0,
    )
    assert row.scope == "company"
    assert row.owner_id is None


def test_outbox_row_carries_personal_scope():
    owner = uuid.uuid4()
    row = OutboxRow(
        outbox_id=uuid.uuid4(),
        entity_kind="wiki_page",
        entity_id=uuid.uuid4(),
        op="delete",
        attempts=0,
        scope="personal",
        owner_id=owner,
    )
    assert row.scope == "personal"
    assert row.owner_id == owner
