"""ask_cache — Phase 3-A semantic answer cache (MA.7a)

A company-scope `/ask` answer cache. Only grounded, single-mode, non-federated
company answers are stored (docs/company-agent-orchestration.md §P3A.2); the row
carries the partition tuple (owner_id sentinel "company" + scope + project +
federation + node_id) plus a `question_key` (sha256 of the normalized question —
never plaintext) and a `watermark` (the company wiki knowledge version at store
time). A read that finds a stale watermark or expired TTL is a MISS.

`question_embedding` (pgvector, nullable) is reserved for MA.7b semantic similarity
matching and stays NULL until that slice; no ivfflat index here.

All behavior sits behind ORTHUS_ASK_SEMANTIC_CACHE_ENABLED (default false), so this
table is simply unused until an operator opts in.

Revision ID: 0076_ask_cache
Revises: 0075_agent_task_threads
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op

revision = "0076_ask_cache"
down_revision = "0075_agent_task_threads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ask_cache (
          id                  UUID PRIMARY KEY,
          owner_id            TEXT NOT NULL,
          scope               TEXT NOT NULL,
          project             TEXT NOT NULL,
          federation          BOOLEAN NOT NULL,
          node_id             TEXT NOT NULL,
          question_key        TEXT NOT NULL,
          question_embedding  vector(1024) NULL,
          watermark           TEXT NOT NULL,
          answer_json         JSONB NOT NULL,
          created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    # The full partition tuple + question_key is the upsert/lookup key. A prefix of
    # this covers partition-only scans too, so no separate partition index is needed.
    op.execute(
        "CREATE UNIQUE INDEX uq_ask_cache_key ON ask_cache "
        "(owner_id, scope, project, federation, node_id, question_key);"
    )
    # Covering index for the company-knowledge watermark aggregate
    # (COUNT(*), MAX(updated_at) over company wiki_pages: scope=? AND owner_id IS NULL
    # [AND project=?]), computed on each company /ask cache lookup/store. Partial on
    # owner_id IS NULL (company pages) keeps it small; including updated_at makes COUNT+MAX
    # an index-only scan instead of a full company-partition heap scan.
    op.execute(
        "CREATE INDEX idx_wiki_pages_company_watermark ON wiki_pages "
        "(scope, project, updated_at) WHERE owner_id IS NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_wiki_pages_company_watermark;")
    op.execute("DROP TABLE ask_cache;")
