"""agent_chat_messages.kind + payload — Human-in-the-Loop structured turns

The agent-work chat orchestrator gains a first-class "ask the user" turn: the
agent can end a turn with a structured question (options / approval / free text)
and resume once the user answers. That question is persisted as an
`agent_chat_messages` row with kind="user_input_request" and a typed payload
(UserInputRequest); the user's answer is kind="user_input_response".

additive + backward-compatible: existing rows default kind='text', payload=NULL,
so behavior is unchanged until ORTHUS_AGENT_HITL_ENABLED is turned on.

Revision ID: 0101_agent_chat_message_kind
Revises: 0100_meeting_attachments
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op

revision = "0101_agent_chat_message_kind"
down_revision = "0100_meeting_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_chat_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'text';")
    op.execute("ALTER TABLE agent_chat_messages ADD COLUMN payload JSONB;")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_chat_messages DROP COLUMN payload;")
    op.execute("ALTER TABLE agent_chat_messages DROP COLUMN kind;")
