"""project overrides: user-editable per-db_name project assignment that takes
precedence over the ingest DEFAULT (id-set membership / db_name heuristic) and
persists across reseeds, plus documents.source_db_name so wiki/corpus content is
traceable back to the Notion DB it came from (P2, docs/p2-fe-and-sources.md).

`project_overrides` is a tiny user-managed table (db_name -> project). The FE
PATCHes it; ingest reads it FIRST when resolving a row's project, so an override
beats the default. `documents.source_db_name` records the originating Notion DB
for DB-row documents (NULL for standalone pages / editor docs) so apply-on-edit
can re-tag the wiki/corpus content (scope "B") that traces to that db_name.

Revision ID: 0007_project_overrides
Revises: 0006_project_dimension
Create Date: 2026-05-28
"""

from alembic import op

revision = "0007_project_overrides"
down_revision = "0006_project_dimension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # User-editable per-db_name project override (FE-managed). Survives reseeds:
    # ingest consults this table before falling back to the id-set / db_name default.
    op.execute(
        "CREATE TABLE project_overrides ("
        "db_name TEXT PRIMARY KEY, "
        "project TEXT NOT NULL "
        "CHECK (project IN ('atlas','nova','orbit','company')), "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ");"
    )

    # The Notion DB a doc's row came from. NULL for editor docs / standalone pages.
    op.execute("ALTER TABLE documents ADD COLUMN source_db_name TEXT NULL;")
    op.execute("CREATE INDEX idx_documents_source_db_name ON documents(source_db_name);")

    # Backfill existing DB-row documents from notion_rows. notion_rows.row_id is the
    # deterministic UUID PK derived from the Notion row id (which is also stored on
    # documents.source_external_id as text), so the join keys them together.
    op.execute(
        "UPDATE documents d SET source_db_name = nr.db_name "
        "FROM notion_rows nr "
        "WHERE d.source_external_id = nr.row_id::text;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_documents_source_db_name;")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS source_db_name;")
    op.execute("DROP TABLE IF EXISTS project_overrides;")
