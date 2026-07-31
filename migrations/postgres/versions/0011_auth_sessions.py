"""S1 session auth tables.

Revision ID: 0011_auth_sessions
Revises: 0010_promote_gate
Create Date: 2026-05-31
"""

from alembic import op

revision = "0011_auth_sessions"
down_revision = "0010_promote_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auth_identities (
          identity_id       UUID PRIMARY KEY,
          user_id           UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
          provider          TEXT NOT NULL,
          provider_subject  TEXT NOT NULL,
          email             TEXT NOT NULL,
          email_verified    BOOLEAN NOT NULL DEFAULT false,
          display_name      TEXT,
          created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_login_at     TIMESTAMPTZ,
          UNIQUE (provider, provider_subject)
        );
        """
    )
    op.execute("CREATE INDEX idx_auth_identities_user ON auth_identities(user_id);")
    op.execute("CREATE INDEX idx_auth_identities_email ON auth_identities(email);")

    op.execute(
        """
        CREATE TABLE auth_sessions (
          session_id           UUID PRIMARY KEY,
          token_hash           TEXT NOT NULL UNIQUE,
          user_id              UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
          node_id              TEXT NOT NULL,
          issued_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
          expires_at           TIMESTAMPTZ NOT NULL,
          absolute_expires_at  TIMESTAMPTZ NOT NULL,
          revoked_at           TIMESTAMPTZ,
          user_agent_hash      TEXT,
          ip_hash              TEXT
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_auth_sessions_lookup
          ON auth_sessions(token_hash, node_id)
          WHERE revoked_at IS NULL;
        """
    )
    op.execute("CREATE INDEX idx_auth_sessions_user ON auth_sessions(user_id);")

    op.execute(
        """
        CREATE TABLE auth_allowlist (
          allowlist_id  UUID PRIMARY KEY,
          node_id       TEXT NOT NULL,
          email         TEXT NOT NULL,
          role          TEXT NOT NULL CHECK (role IN ('owner','admin','member','viewer')),
          created_by    UUID REFERENCES users(user_id) ON DELETE SET NULL,
          created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
          revoked_at    TIMESTAMPTZ,
          UNIQUE (node_id, email)
        );
        """
    )
    op.execute(
        """
        CREATE INDEX idx_auth_allowlist_active
          ON auth_allowlist(node_id, email)
          WHERE revoked_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_auth_allowlist_active;")
    op.execute("DROP TABLE IF EXISTS auth_allowlist;")
    op.execute("DROP INDEX IF EXISTS idx_auth_sessions_user;")
    op.execute("DROP INDEX IF EXISTS idx_auth_sessions_lookup;")
    op.execute("DROP TABLE IF EXISTS auth_sessions;")
    op.execute("DROP INDEX IF EXISTS idx_auth_identities_email;")
    op.execute("DROP INDEX IF EXISTS idx_auth_identities_user;")
    op.execute("DROP TABLE IF EXISTS auth_identities;")
