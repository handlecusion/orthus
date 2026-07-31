"""wiki_pages.display_title: human-readable KG claim node label

Revision ID: 0089_wiki_pages_display_title
Revises: 0088_mail_signatures_from_addr
Create Date: 2026-07-02

nullable TEXT. NULL이면 KG 투영이 기존 title(=claim slug)로 폴백하므로
server_default/NOT NULL을 두지 않는다. LLM 헤드라인은 wiki 저작/백필 계층에서만
생성돼 이 컬럼에 저장되고, KG 투영은 저장값을 slug처럼 복사만 한다(투영 LLM 0회).
"""

from __future__ import annotations

from alembic import op

revision = "0089_wiki_pages_display_title"
down_revision = "0088_mail_signatures_from_addr"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS display_title TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE wiki_pages DROP COLUMN IF EXISTS display_title;")
