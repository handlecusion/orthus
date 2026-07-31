"""connector substrate for company/personal source accounts.

Revision ID: 0009_connector_substrate
Revises: 0008_task_states
Create Date: 2026-05-28
"""

from alembic import op

revision = "0009_connector_substrate"
down_revision = "0008_task_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE connector_accounts (
          account_id         UUID PRIMARY KEY,
          connector_slug     TEXT NOT NULL,
          account_kind       TEXT NOT NULL CHECK (account_kind IN ('company','personal')),
          node_id            TEXT NOT NULL,
          scope              TEXT NOT NULL CHECK (scope IN ('company','personal')),
          owner_id           UUID REFERENCES users(user_id) ON DELETE CASCADE,
          project            TEXT,
          auth_mode          TEXT NOT NULL,
          account_label      TEXT,
          status             TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','paused','error','disabled')),
          settings_redacted  JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          CHECK (
            (account_kind = 'company' AND scope = 'company' AND owner_id IS NULL)
            OR
            (account_kind = 'personal' AND scope = 'personal' AND owner_id IS NOT NULL)
          )
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_connector_accounts_kind_slug
          ON connector_accounts(account_kind, connector_slug, status);
        """
    )
    op.execute(
        """
        CREATE INDEX idx_connector_accounts_owner
          ON connector_accounts(owner_id, connector_slug)
          WHERE owner_id IS NOT NULL;
        """
    )

    op.execute(
        """
        CREATE TABLE connector_sync_state (
          account_id         UUID PRIMARY KEY
                             REFERENCES connector_accounts(account_id) ON DELETE CASCADE,
          cursor_json        JSONB NOT NULL DEFAULT '{}'::jsonb,
          seen_json          JSONB NOT NULL DEFAULT '{}'::jsonb,
          daily_budget_json  JSONB NOT NULL DEFAULT '{}'::jsonb,
          last_seen_id       TEXT,
          last_sync_at       TIMESTAMPTZ,
          last_error         TEXT,
          updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE connector_runs (
          run_id             UUID PRIMARY KEY,
          account_id         UUID REFERENCES connector_accounts(account_id) ON DELETE SET NULL,
          connector_slug     TEXT NOT NULL,
          reason             TEXT NOT NULL,
          status             TEXT NOT NULL CHECK (status IN ('running','succeeded','failed')),
          fetched            INT NOT NULL DEFAULT 0,
          created            INT NOT NULL DEFAULT 0,
          updated            INT NOT NULL DEFAULT 0,
          skipped            INT NOT NULL DEFAULT 0,
          errors             INT NOT NULL DEFAULT 0,
          error_message      TEXT,
          started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at        TIMESTAMPTZ
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_connector_runs_account ON connector_runs(account_id, started_at DESC);"
    )

    op.execute(
        """
        ALTER TABLE documents
          ADD COLUMN source_account_id UUID
          REFERENCES connector_accounts(account_id) ON DELETE SET NULL;
        """
    )

    # C0 opens connector sources beyond the original Notion/editor pair.
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_source_check;")
    op.execute("DROP INDEX IF EXISTS uq_documents_source;")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_documents_source_account_external
          ON documents(source, source_account_id, source_external_id) NULLS NOT DISTINCT
          WHERE source_external_id IS NOT NULL;
        """
    )
    op.execute(
        """
        CREATE INDEX idx_documents_source_account
          ON documents(source, source_account_id);
        """
    )

    op.execute(
        """
        CREATE TABLE connector_items (
          account_id         UUID NOT NULL
                             REFERENCES connector_accounts(account_id) ON DELETE CASCADE,
          external_id        TEXT NOT NULL,
          external_version   TEXT,
          content_hash       TEXT,
          doc_id             UUID REFERENCES documents(doc_id) ON DELETE SET NULL,
          ingested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (account_id, external_id)
        );
        """
    )
    op.execute("CREATE INDEX idx_connector_items_doc ON connector_items(doc_id);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_connector_items_doc;")
    op.execute("DROP TABLE IF EXISTS connector_items;")

    op.execute("DROP INDEX IF EXISTS idx_documents_source_account;")
    op.execute("DROP INDEX IF EXISTS uq_documents_source_account_external;")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_documents_source
          ON documents(source, source_external_id)
          WHERE source_external_id IS NOT NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE documents
          ADD CONSTRAINT documents_source_check
          CHECK (source IN ('notion','editor'));
        """
    )
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS source_account_id;")

    op.execute("DROP INDEX IF EXISTS idx_connector_runs_account;")
    op.execute("DROP TABLE IF EXISTS connector_runs;")
    op.execute("DROP TABLE IF EXISTS connector_sync_state;")
    op.execute("DROP INDEX IF EXISTS idx_connector_accounts_owner;")
    op.execute("DROP INDEX IF EXISTS idx_connector_accounts_kind_slug;")
    op.execute("DROP TABLE IF EXISTS connector_accounts;")
