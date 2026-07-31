"""tenant scope: scope column on documents/corpus_chunks/embeddings/wiki_pages
+ wiki_pages.owner_id for personal isolation (P2.1, docs/architecture-v2.md §2)

Revision ID: 0004_tenant_scope
Revises: 0003_llm_wiki
Create Date: 2026-05-27
"""

from alembic import op

revision = "0004_tenant_scope"
down_revision = "0003_llm_wiki"
branch_labels = None
depends_on = None

# DEFAULT 'company' backfills every seeded row (the 1365 company docs) correctly.
_SCOPE_COL = (
    "ADD COLUMN scope TEXT NOT NULL DEFAULT 'company' "
    "CHECK (scope IN ('company','personal'))"
)


def upgrade() -> None:
    # scope on the four scoped tables (corpus + wiki layers).
    op.execute(f"ALTER TABLE documents {_SCOPE_COL};")
    op.execute(f"ALTER TABLE corpus_chunks {_SCOPE_COL};")
    op.execute(f"ALTER TABLE embeddings {_SCOPE_COL};")
    op.execute(f"ALTER TABLE wiki_pages {_SCOPE_COL};")

    # wiki_pages owner: documents/embeddings already carry user_id, but wiki_pages
    # has none — personal pages need an owner. NULL == company-shared.
    op.execute("ALTER TABLE wiki_pages ADD COLUMN owner_id UUID;")

    # The old global UNIQUE on slug would block a personal page reusing a company
    # slug. Replace it with a composite unique keyed by (slug, scope, owner_id).
    # NULLS NOT DISTINCT (PG15+) makes company rows (owner_id NULL) still collide
    # on duplicate slug, so company-slug uniqueness is preserved.
    op.execute("ALTER TABLE wiki_pages DROP CONSTRAINT IF EXISTS wiki_pages_slug_key;")
    op.execute(
        "CREATE UNIQUE INDEX uq_wiki_pages_slug_scope_owner "
        "ON wiki_pages (slug, scope, owner_id) NULLS NOT DISTINCT;"
    )

    # Filtered-lookup indexes for the scope-aware retrieval path.
    op.execute(
        "CREATE INDEX idx_embeddings_user_scope_kind ON embeddings(user_id, scope, kind);"
    )
    op.execute("CREATE INDEX idx_wiki_pages_scope_owner ON wiki_pages(scope, owner_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_wiki_pages_scope_owner;")
    op.execute("DROP INDEX IF EXISTS idx_embeddings_user_scope_kind;")

    # Restore the original global UNIQUE on slug, drop the composite.
    op.execute("DROP INDEX IF EXISTS uq_wiki_pages_slug_scope_owner;")
    op.execute(
        "ALTER TABLE wiki_pages ADD CONSTRAINT wiki_pages_slug_key UNIQUE (slug);"
    )

    op.execute("ALTER TABLE wiki_pages DROP COLUMN IF EXISTS owner_id;")
    op.execute("ALTER TABLE wiki_pages DROP COLUMN IF EXISTS scope;")
    op.execute("ALTER TABLE embeddings DROP COLUMN IF EXISTS scope;")
    op.execute("ALTER TABLE corpus_chunks DROP COLUMN IF EXISTS scope;")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS scope;")
