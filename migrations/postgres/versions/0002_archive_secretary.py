"""archive + secretary: documents, corpus_chunks, data_sources, query_runs

Revision ID: 0002_archive_secretary
Revises: 0001_base
Create Date: 2026-05-25
"""

from alembic import op

revision = "0002_archive_secretary"
down_revision = "0001_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 5.1 documents — editor SoR + provenance.
    op.execute(
        """
        CREATE TABLE documents (
          doc_id              UUID PRIMARY KEY,
          user_id             UUID NOT NULL REFERENCES users(user_id),
          title               TEXT NOT NULL,
          block_json          JSONB NOT NULL,
          markdown            TEXT NOT NULL,
          source              TEXT NOT NULL CHECK (source IN ('notion','editor')),
          source_external_id  TEXT,
          source_last_edited_at TIMESTAMPTZ,
          schema_version      INT  NOT NULL,
          created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_documents_source
          ON documents(source, source_external_id)
          WHERE source_external_id IS NOT NULL;
        """
    )

    # 5.2 corpus_chunks — RAG + 비서 grounding retrieval unit.
    op.execute(
        """
        CREATE TABLE corpus_chunks (
          chunk_id        UUID PRIMARY KEY,
          doc_id          UUID NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
          ordinal         INT  NOT NULL,
          content         TEXT NOT NULL,
          embedding_id    UUID REFERENCES embeddings(embedding_id),
          meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_corpus_chunks_doc ON corpus_chunks(doc_id, ordinal);")

    # 5.3 data_sources — registry of DBs the 비서 may query (dialect-agnostic).
    op.execute(
        """
        CREATE TABLE data_sources (
          source_id       UUID PRIMARY KEY,
          name            TEXT NOT NULL UNIQUE,
          dialect         TEXT NOT NULL,
          dsn_secret_key  TEXT NOT NULL,          -- Secrets Manager key name (no plaintext)
          catalog         JSONB NOT NULL,         -- tables/columns/types + business defs
          read_only       BOOLEAN NOT NULL DEFAULT true,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # 5.4 query_runs — 비서 audit + validation result.
    op.execute(
        """
        CREATE TABLE query_runs (
          query_id        UUID PRIMARY KEY,
          user_id         UUID NOT NULL REFERENCES users(user_id),
          source_id       UUID NOT NULL REFERENCES data_sources(source_id),
          nl_question     TEXT NOT NULL,
          compiled_sql    TEXT,
          validation      JSONB NOT NULL,
          status          TEXT NOT NULL CHECK (status IN
                            ('compiled','validated','rejected','executed','failed')),
          result_meta     JSONB,
          schema_version  INT  NOT NULL,
          created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_query_runs_user ON query_runs(user_id, created_at DESC);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS query_runs;")
    op.execute("DROP TABLE IF EXISTS data_sources;")
    op.execute("DROP TABLE IF EXISTS corpus_chunks;")
    op.execute("DROP TABLE IF EXISTS documents;")
