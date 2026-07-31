"""KG entity SoR (K6)

Revision ID: 0049_kg_entities
Revises: fadb806ba302
Create Date: 2026-06-13

K6 entity 레이어의 SoR. LLM은 distill에서 entity 이름을 추출만 하고, 결정론 코드가
이 테이블에 적재한 뒤 KG가 :Entity/MENTIONED_IN/RELATES_TO로 결정론 투영한다
(rebuild 시 LLM 재호출 없음 — rebuildable 계약, docs/kg-model.md §2 / 구현 명세 §9.1).

- entity_key = "{entity_kind}:{name_norm}" (=:Entity MERGE 키)
- owner_id는 K7 대비 컬럼이며 K6에서는 항상 NULL (company scope only)
- display_name(person)은 persist 전 redact_pii 통과 — Direct PII person carve-out
  (docs/operations.md §8.3, kg-model §1)
- idx_kg_entities_name_norm: entity_conflict 교차-kind 검사용 (구현 명세 §9.2)
"""

from __future__ import annotations

from alembic import op

revision = "0049_kg_entities"
down_revision = "fadb806ba302"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE kg_entities (
          entity_id      UUID PRIMARY KEY,
          entity_key     TEXT NOT NULL UNIQUE,
          entity_kind    TEXT NOT NULL CHECK (entity_kind IN ('person','org','project','system')),
          name_norm      TEXT NOT NULL,
          display_name   TEXT NOT NULL,
          scope          TEXT NOT NULL DEFAULT 'company',
          owner_id       UUID NULL,
          first_seen     TIMESTAMPTZ NOT NULL,
          last_seen      TIMESTAMPTZ NOT NULL,
          schema_version INT NOT NULL,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_kg_entities_name_norm ON kg_entities (name_norm);")
    op.execute(
        """
        CREATE TABLE kg_entity_mentions (
          mention_id     UUID PRIMARY KEY,
          entity_id      UUID NOT NULL REFERENCES kg_entities(entity_id) ON DELETE CASCADE,
          page_id        UUID NOT NULL,
          evidence_slug  TEXT NOT NULL,
          schema_version INT NOT NULL,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          UNIQUE (entity_id, page_id)
        );
        """
    )
    op.execute("CREATE INDEX idx_kg_entity_mentions_page ON kg_entity_mentions (page_id);")


def downgrade() -> None:
    op.execute("DROP TABLE kg_entity_mentions;")
    op.execute("DROP TABLE kg_entities;")
