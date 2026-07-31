"""mail_tracking: 메일 열람(읽음) 추적 owner-scope hash-only 저장

보낸 HTML 메일 끝에 orthus 공개 픽셀(<img>)을 심고, 수신자가 열 때 픽셀 로드를
1건 기록한다. provider 협조 불필요(orthus가 본문 HTML을 직접 조립). email_send_log와
동일하게 recipient/subject는 sha256 hash-only로 보관해 평문 PII를 남기지 않는다.
token은 추측 불가 랜덤이고 공개 픽셀 엔드포인트가 이 token만으로 open을 올린다.
fail-closed: 플래그/공개 base URL이 설정된 경우에만 행이 생성된다.

Revision ID: 0083_mail_tracking
Revises: 0082_mail_signatures
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op

revision = "0083_mail_tracking"
down_revision = "0082_mail_signatures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mail_tracking (
            tracking_id UUID PRIMARY KEY,
            node_id TEXT NOT NULL,
            owner_id UUID NOT NULL,
            token TEXT NOT NULL,
            recipient_hash TEXT NOT NULL,
            subject_hash TEXT NOT NULL,
            sender_kind TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            open_count INTEGER NOT NULL DEFAULT 0,
            first_opened_at TIMESTAMPTZ,
            last_opened_at TIMESTAMPTZ
        );
        """
    )
    # 공개 픽셀이 token만으로 조회/증가하므로 token은 유니크.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_tracking_token "
        "ON mail_tracking (token);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_mail_tracking_owner "
        "ON mail_tracking (node_id, owner_id, created_at);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_mail_tracking_owner;")
    op.execute("DROP INDEX IF EXISTS uq_mail_tracking_token;")
    op.execute("DROP TABLE IF EXISTS mail_tracking;")
