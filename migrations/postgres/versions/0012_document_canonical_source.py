"""document canonical source identity for multi-account Notion dedupe

Revision ID: 0012_document_canonical_source
Revises: 0011_auth_sessions
Create Date: 2026-06-03
"""

from __future__ import annotations

from alembic import op

revision = "0012_document_canonical_source"
down_revision = "0011_auth_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN source_canonical_id TEXT NULL;")
    op.execute(
        """
        UPDATE documents
        SET source_canonical_id =
          'notion:page:' ||
          CASE
            WHEN source_external_id ~* '^[0-9a-f]{32}$'
              OR source_external_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN (source_external_id::uuid)::text
            ELSE lower(source_external_id)
          END
        WHERE source = 'notion'
          AND scope IN ('company', 'personal')
          AND source_external_id IS NOT NULL;
        """
    )
    op.execute(
        """
        WITH ranked AS (
          SELECT
            doc_id,
            first_value(doc_id) OVER (
              PARTITION BY source, scope, source_canonical_id
              ORDER BY source_last_edited_at DESC NULLS LAST,
                       updated_at DESC NULLS LAST,
                       created_at ASC NULLS LAST,
                       doc_id ASC
            ) AS keep_doc_id
          FROM documents
          WHERE source_canonical_id IS NOT NULL
            AND scope = 'company'
        )
        UPDATE connector_items ci
        SET doc_id = ranked.keep_doc_id
        FROM ranked
        WHERE ci.doc_id = ranked.doc_id
          AND ranked.doc_id <> ranked.keep_doc_id;
        """
    )
    op.execute(
        """
        WITH ranked AS (
          SELECT
            doc_id,
            first_value(doc_id) OVER (
              PARTITION BY source, scope, user_id, source_canonical_id
              ORDER BY source_last_edited_at DESC NULLS LAST,
                       updated_at DESC NULLS LAST,
                       created_at ASC NULLS LAST,
                       doc_id ASC
            ) AS keep_doc_id
          FROM documents
          WHERE source_canonical_id IS NOT NULL
            AND scope = 'personal'
        )
        UPDATE connector_items ci
        SET doc_id = ranked.keep_doc_id
        FROM ranked
        WHERE ci.doc_id = ranked.doc_id
          AND ranked.doc_id <> ranked.keep_doc_id;
        """
    )
    op.execute(
        """
        WITH ranked AS (
          SELECT
            doc_id,
            first_value(doc_id) OVER (
              PARTITION BY source, scope, source_canonical_id
              ORDER BY source_last_edited_at DESC NULLS LAST,
                       updated_at DESC NULLS LAST,
                       created_at ASC NULLS LAST,
                       doc_id ASC
            ) AS keep_doc_id
          FROM documents
          WHERE source_canonical_id IS NOT NULL
            AND scope = 'company'
        )
        DELETE FROM documents d
        USING ranked
        WHERE d.doc_id = ranked.doc_id
          AND ranked.doc_id <> ranked.keep_doc_id;
        """
    )
    op.execute(
        """
        WITH ranked AS (
          SELECT
            doc_id,
            first_value(doc_id) OVER (
              PARTITION BY source, scope, user_id, source_canonical_id
              ORDER BY source_last_edited_at DESC NULLS LAST,
                       updated_at DESC NULLS LAST,
                       created_at ASC NULLS LAST,
                       doc_id ASC
            ) AS keep_doc_id
          FROM documents
          WHERE source_canonical_id IS NOT NULL
            AND scope = 'personal'
        )
        DELETE FROM documents d
        USING ranked
        WHERE d.doc_id = ranked.doc_id
          AND ranked.doc_id <> ranked.keep_doc_id;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_documents_company_canonical_source
          ON documents(source, scope, source_canonical_id)
          WHERE source_canonical_id IS NOT NULL
            AND scope = 'company';
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_documents_personal_canonical_source
          ON documents(source, scope, user_id, source_canonical_id)
          WHERE source_canonical_id IS NOT NULL
            AND scope = 'personal';
        """
    )
    op.execute(
        """
        CREATE INDEX idx_documents_source_canonical
          ON documents(source, source_canonical_id)
          WHERE source_canonical_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_documents_source_canonical;")
    op.execute("DROP INDEX IF EXISTS uq_documents_personal_canonical_source;")
    op.execute("DROP INDEX IF EXISTS uq_documents_company_canonical_source;")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS source_canonical_id;")
