"""meeting notes (회의록) — Notion-DB-like records

Revision ID: 0030_meeting_notes
Revises: 0029_calendar_event_location
Create Date: 2026-06-07
"""

from __future__ import annotations

from alembic import op

revision = "0030_meeting_notes"
down_revision = "0029_calendar_event_location"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE meeting_notes (
          note_id UUID PRIMARY KEY,
          node_id TEXT NOT NULL,
          title TEXT NOT NULL,
          project_id UUID REFERENCES dashboard_projects(project_id) ON DELETE SET NULL,
          meeting_date DATE NOT NULL,
          attendee_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
          body TEXT,
          created_by UUID,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_meeting_notes_node_date ON meeting_notes(node_id, meeting_date DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_meeting_notes_node_date;")
    op.execute("DROP TABLE IF EXISTS meeting_notes;")
