"""ask_cache semantic match (MA.7b) — docs/company-agent-orchestration.md §P3A.4

Adds the embedding-similarity matching layer on top of MA.7a's exact cache:
- `question_redacted` (TEXT, nullable): the redacted+normalized question text the
  embedding was computed from. Stored only when the MA.7b sub-flag is on, after
  `redact_pii_text` (불변식 6 / §P3A.7) so no raw question PII lands in the row;
  lets an operator see what a semantic row represents (matching itself only needs
  the vector).
- ivfflat index on the (already-present) `question_embedding` column so the
  `ORDER BY question_embedding <=> $qvec LIMIT 1` candidate scan is index-backed.
  ivfflat is approximate; an approximate miss just recomputes (safe degradation),
  and a false hit is impossible because the lookup post-filters cosine distance
  against the fixed threshold τ.

Both stay inert until ORTHUS_ASK_CACHE_SEMANTIC_MATCH_ENABLED (default false) — with
the sub-flag off `cache_store` writes NULL into both columns and `cache_lookup`
never runs the semantic query (exact-only, MA.7a byte-identical).

Revision ID: 0077_ask_cache_semantic
Revises: 0076_ask_cache
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op

revision = "0077_ask_cache_semantic"
down_revision = "0076_ask_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ask_cache ADD COLUMN question_redacted TEXT NULL;")
    # Same ops/lists as the corpus `embeddings` ivfflat index (0001). NULL embeddings
    # (exact-only rows) are simply not indexed.
    op.execute(
        "CREATE INDEX idx_ask_cache_question_embedding ON ask_cache "
        "USING ivfflat (question_embedding vector_cosine_ops) WITH (lists = 100);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_ask_cache_question_embedding;")
    op.execute("ALTER TABLE ask_cache DROP COLUMN IF EXISTS question_redacted;")
