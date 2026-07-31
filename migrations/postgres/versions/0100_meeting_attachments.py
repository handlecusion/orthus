"""회의록 첨부파일(meeting_attachments): PDF 등 미팅자료를 회의별로 보관

노션 DB의 'Files & media' 속성처럼 각 회의록(meeting_notes)에 파일을 여러 개
붙인다. 파일 바이트 자체는 media_store(디스크 실파일 + capability URL)에 저장하고,
이 테이블은 회의↔파일 매핑 + 원본 파일명/타입/크기 메타만 보관한다(본문 JSON에
base64로 박지 않으므로 본문이 커지거나 깨져도 첨부는 독립적으로 안전하다).

Revision ID: 0100_meeting_attachments
Revises: 0099_kpi_scoring
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op

revision = "0100_meeting_attachments"
down_revision = "0099_kpi_scoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_attachments (
          attachment_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          note_id UUID NOT NULL,
          filename TEXT NOT NULL,
          media_filename TEXT NOT NULL,
          mime_type TEXT,
          size_bytes BIGINT NOT NULL DEFAULT 0,
          created_by UUID,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_meeting_attachments_note "
        "ON meeting_attachments (node_id, note_id, created_at);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_meeting_attachments_note;")
    op.execute("DROP TABLE IF EXISTS meeting_attachments;")
