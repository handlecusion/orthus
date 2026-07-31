"""kg_outbox — K3 transactional outbox (docs/kg-model.md §3)

legacy P0/P1 persona-era `kg_outbox`와 무관한 신규 정의다(docs/data-model.md §12).
wiki consolidate / document publish / promote approve의 PG commit과 같은
트랜잭션에서 enqueue되고, KGOutboxWorker가 Neo4j에 준실시간 반영한다.

Revision ID: 0047_kg_outbox
Revises: 0046_email_send_manual_origin
Create Date: 2026-06-12
"""

from __future__ import annotations

from alembic import op

revision = "0047_kg_outbox"
down_revision = "0046_email_send_manual_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE kg_outbox (
          outbox_id      UUID PRIMARY KEY,
          entity_kind    TEXT NOT NULL
                         CHECK (entity_kind IN ('wiki_page','document','structured_row')),
          entity_id      UUID NOT NULL,
          op             TEXT NOT NULL CHECK (op IN ('upsert','delete')),
          status         TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','applied','dead')),
          attempts       INT  NOT NULL DEFAULT 0,
          lease_until    TIMESTAMPTZ NULL,
          last_error     TEXT NULL,
          correlation_id UUID NULL,
          enqueued_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_kg_outbox_status_enqueued ON kg_outbox (status, enqueued_at);")


def downgrade() -> None:
    op.execute("DROP TABLE kg_outbox;")
