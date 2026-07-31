"""project dimension: project column on documents/corpus_chunks/embeddings/
wiki_pages/notion_rows for company→project→content organization (P2,
docs/p2-fe-and-sources.md).

Hierarchy: company (acme) → project (atlas | nova | orbit | company)
→ content. The token's Notion workspace IS the (now-deprecated) `atlas` project,
so DEFAULT 'atlas' correctly backfills every existing seeded row. Nova rows are
then split out by db_name match (see backfill below). `orbit`/`company` are
reserved (no current data; company = org-level/cross-project, future).

Revision ID: 0006_project_dimension
Revises: 0005_structured_store
Create Date: 2026-05-27
"""

from alembic import op

revision = "0006_project_dimension"
down_revision = "0005_structured_store"
branch_labels = None
depends_on = None

# DEFAULT 'atlas' backfills every existing row: the seeded workspace = atlas.
_PROJECT_COL = (
    "ADD COLUMN project TEXT NOT NULL DEFAULT 'atlas' "
    "CHECK (project IN ('atlas','nova','orbit','company'))"
)


def upgrade() -> None:
    # project on the five content-bearing tables (corpus + wiki + structured layers).
    op.execute(f"ALTER TABLE documents {_PROJECT_COL};")
    op.execute(f"ALTER TABLE corpus_chunks {_PROJECT_COL};")
    op.execute(f"ALTER TABLE embeddings {_PROJECT_COL};")
    op.execute(f"ALTER TABLE wiki_pages {_PROJECT_COL};")
    op.execute(f"ALTER TABLE notion_rows {_PROJECT_COL};")

    # Backfill Nova: only notion_rows carries db_name, so we can deterministically
    # re-classify Nova DB rows in place. wiki_pages/embeddings/corpus_chunks have no
    # db_name, so their Nova content stays 'atlas' until a reseed re-resolves the
    # project at ingest time (resolve_project on the connector path). This is the
    # documented limitation: existing wiki/corpus content is NOT retroactively split.
    op.execute(
        "UPDATE notion_rows SET project='nova' "
        "WHERE db_name LIKE 'Nova%' OR db_name LIKE '%NOVA%';"
    )

    # Filtered-lookup indexes for the project-aware retrieval/query path.
    op.execute("CREATE INDEX idx_wiki_pages_project_scope ON wiki_pages(project, scope);")
    op.execute("CREATE INDEX idx_notion_rows_project_scope ON notion_rows(project, scope);")
    # Extend the embeddings filter index with project (retrieval narrows on
    # user_id, scope, kind AND project).
    op.execute("DROP INDEX IF EXISTS idx_embeddings_user_scope_kind;")
    op.execute(
        "CREATE INDEX idx_embeddings_user_scope_kind "
        "ON embeddings(user_id, scope, kind, project);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_embeddings_user_scope_kind;")
    op.execute(
        "CREATE INDEX idx_embeddings_user_scope_kind ON embeddings(user_id, scope, kind);"
    )
    op.execute("DROP INDEX IF EXISTS idx_notion_rows_project_scope;")
    op.execute("DROP INDEX IF EXISTS idx_wiki_pages_project_scope;")

    op.execute("ALTER TABLE notion_rows DROP COLUMN IF EXISTS project;")
    op.execute("ALTER TABLE wiki_pages DROP COLUMN IF EXISTS project;")
    op.execute("ALTER TABLE embeddings DROP COLUMN IF EXISTS project;")
    op.execute("ALTER TABLE corpus_chunks DROP COLUMN IF EXISTS project;")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS project;")
