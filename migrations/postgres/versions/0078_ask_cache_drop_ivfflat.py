"""ask_cache drop ivfflat — make semantic ranking exact/deterministic (MA.7b fix)

The MA.7b semantic lookup ranks a SELECTIVE partition (single company owner sentinel +
scope + project + node + federation, narrowed further by watermark==current + TTL) by
cosine distance and post-filters against the fixed threshold τ. That ranking must be EXACT
for hit/miss to stay deterministic (불변식 25, docs/company-agent-orchestration.md §P3A).

The ivfflat index added in 0077 is APPROXIMATE: for a `ORDER BY embedding <=> $q LIMIT k`
the planner may pick it even on a selective partition, and with the default
`ivfflat.probes=1` the true nearest in-threshold neighbor can sit in an unprobed list and be
skipped — a false MISS that silently lowers recall and makes hit/miss depend on index/ANALYZE
state. Dropping the index forces an exact partition scan (the partition btree `uq_ask_cache_key`
prefix filters, then the matching rows are distance-sorted exactly). The partition is bounded
(MA.7c GC + TTL), so the exact scan is cheap; the approximate index bought acceleration the
cache does not want at the cost of determinism.

Revision ID: 0078_ask_cache_drop_ivfflat
Revises: 0077_ask_cache_semantic
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op

revision = "0078_ask_cache_drop_ivfflat"
down_revision = "0077_ask_cache_semantic"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_ask_cache_question_embedding;")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX idx_ask_cache_question_embedding ON ask_cache "
        "USING ivfflat (question_embedding vector_cosine_ops) WITH (lists = 100);"
    )
