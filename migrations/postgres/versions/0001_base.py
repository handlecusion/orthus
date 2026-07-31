"""base: users, pgvector, embeddings, audit_log

Revision ID: 0001_base
Revises:
Create Date: 2026-05-25
"""

from alembic import op

revision = "0001_base"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    # users — subset of data-model.md §1.1 used by this product line.
    op.execute(
        """
        CREATE TABLE users (
          user_id             UUID PRIMARY KEY,
          external_id         TEXT UNIQUE,
          display_name        TEXT NOT NULL,
          preferred_timezone  TEXT NOT NULL DEFAULT 'UTC',   -- IANA
          created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # embeddings — pgvector, 1024-d (data-model.md §2). kind extended with 'corpus_chunk'.
    op.execute(
        """
        CREATE TABLE embeddings (
          embedding_id    UUID PRIMARY KEY,
          user_id         UUID NOT NULL REFERENCES users(user_id),
          kind            TEXT NOT NULL CHECK (kind IN ('corpus_chunk')),
          ref_id          UUID NOT NULL,
          vec             vector(1024) NOT NULL,
          meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
          schema_version  INT  NOT NULL,
          model_version   TEXT NOT NULL,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_embeddings_user_kind ON embeddings(user_id, kind);")
    op.execute(
        """
        CREATE INDEX idx_embeddings_vec ON embeddings
          USING ivfflat (vec vector_cosine_ops) WITH (lists = 100);
        """
    )

    # audit_log — per-call span log (prompt §4.3). enter/exit/error one each per span.
    op.execute(
        """
        CREATE TABLE audit_log (
          audit_id        BIGSERIAL PRIMARY KEY,
          correlation_id  UUID NOT NULL,
          node_run_id     UUID NOT NULL,
          node            TEXT NOT NULL,
          phase           TEXT NOT NULL CHECK (phase IN ('enter','exit','error')),
          output          JSONB,                 -- redacted (operations §2.3)
          meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
          error_class     TEXT,
          error_message   TEXT,
          occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_audit_correlation ON audit_log(correlation_id, occurred_at);")
    op.execute("CREATE INDEX idx_audit_run ON audit_log(node_run_id);")
    op.execute(
        "CREATE UNIQUE INDEX uq_audit_run_phase ON audit_log(node_run_id, phase);"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_log;")
    op.execute("DROP TABLE IF EXISTS embeddings;")
    op.execute("DROP TABLE IF EXISTS users;")
