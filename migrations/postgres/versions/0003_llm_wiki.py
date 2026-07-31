"""llm wiki: wiki_pages, wiki_chunks, wiki_links; extend embeddings.kind CHECK

Revision ID: 0003_llm_wiki
Revises: 0002_archive_secretary
Create Date: 2026-05-25
"""

from alembic import op

revision = "0003_llm_wiki"
down_revision = "0002_archive_secretary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extend embeddings.kind CHECK to include 'wiki_chunk'.
    # Drop the existing constraint, recreate with both allowed values.
    op.execute(
        "ALTER TABLE embeddings DROP CONSTRAINT IF EXISTS embeddings_kind_check;"
    )
    op.execute(
        "ALTER TABLE embeddings ADD CONSTRAINT embeddings_kind_check "
        "CHECK (kind IN ('corpus_chunk', 'wiki_chunk'));"
    )

    # wiki_pages — markdown SoR mirror + search index.
    op.execute(
        """
        CREATE TABLE wiki_pages (
          page_id        UUID PRIMARY KEY,
          slug           TEXT NOT NULL UNIQUE,
          kind           TEXT NOT NULL CHECK (kind IN ('source','claim','page','task')),
          path           TEXT NOT NULL,
          title          TEXT NOT NULL,
          confidence     TEXT CHECK (confidence IN ('high','medium','low')),
          content_hash   TEXT NOT NULL,
          schema_version INT  NOT NULL,
          created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    # wiki_chunks — pgvector retrieval units for wiki pages.
    op.execute(
        """
        CREATE TABLE wiki_chunks (
          chunk_id     UUID PRIMARY KEY,
          page_id      UUID NOT NULL REFERENCES wiki_pages(page_id) ON DELETE CASCADE,
          ordinal      INT  NOT NULL,
          content      TEXT NOT NULL,
          embedding_id UUID REFERENCES embeddings(embedding_id),
          created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("CREATE INDEX idx_wiki_chunks_page ON wiki_chunks(page_id, ordinal);")

    # wiki_links — backlink/relation graph + provenance chain.
    op.execute(
        """
        CREATE TABLE wiki_links (
          src_page_id  UUID NOT NULL REFERENCES wiki_pages(page_id) ON DELETE CASCADE,
          dst_slug     TEXT NOT NULL,
          rel          TEXT NOT NULL CHECK (rel IN ('backlink','supports','conflicts','derived_from')),
          PRIMARY KEY (src_page_id, dst_slug, rel)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wiki_links;")
    op.execute("DROP TABLE IF EXISTS wiki_chunks;")
    op.execute("DROP TABLE IF EXISTS wiki_pages;")

    # Revert embeddings.kind CHECK to original ('corpus_chunk' only).
    op.execute(
        "ALTER TABLE embeddings DROP CONSTRAINT IF EXISTS embeddings_kind_check;"
    )
    op.execute(
        "ALTER TABLE embeddings ADD CONSTRAINT embeddings_kind_check "
        "CHECK (kind IN ('corpus_chunk'));"
    )
