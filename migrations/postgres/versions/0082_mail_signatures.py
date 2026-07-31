"""mail_signatures: 메일 명함(서명) owner-scope 저장

작성한 메일 끝에 넣는 본인 비즈니스 카드(이름/직함/회사/이메일/전화/웹사이트 +
LinkedIn 등 외부 링크). owner-scope 개인 데이터로 (node_id, owner_id)당 1행 upsert.
본문 HTML 렌더는 FE가 구조화 필드에서 만들고 서버는 필드만 보관한다.

Revision ID: 0082_mail_signatures
Revises: 0081_weekly_obj_company_source
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op

revision = "0082_mail_signatures"
down_revision = "0081_weekly_obj_company_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mail_signatures (
            signature_id UUID PRIMARY KEY,
            node_id TEXT NOT NULL,
            owner_id UUID NOT NULL,
            display_name TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            website TEXT NOT NULL DEFAULT '',
            links JSONB NOT NULL DEFAULT '[]'::jsonb,
            enabled BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    # owner당 1개 명함 — upsert 키.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_signatures_owner "
        "ON mail_signatures (node_id, owner_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_mail_signatures_owner;")
    op.execute("DROP TABLE IF EXISTS mail_signatures;")
