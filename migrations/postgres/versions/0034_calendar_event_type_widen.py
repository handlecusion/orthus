"""widen team_calendar_events.event_type check to match FE option set

The frontend calendar form (`web/src/app/dashboard/calendar/page.tsx`) and the
`CalendarEventType` TS union (`web/src/lib/api.ts`) added project kinds
(`dev`, `update`, `atlas_dev`, `atlas_update`) but the DB CHECK constraint
from 0023 still only allowed `event/meeting/vacation/wfh/holiday/deadline`.
Saving any of the new kinds raised CheckViolation → POST/PATCH 500. Widen the
constraint so it matches the TS contract.

Revision ID: 0034_calendar_event_type_widen
Revises: 0033_auth_sso_tickets
Create Date: 2026-06-08
"""

from __future__ import annotations

from alembic import op

revision = "0034_calendar_event_type_widen"
down_revision = "0033_auth_sso_tickets"
branch_labels = None
depends_on = None

# Full set = original 0023 values + frontend option values (CalendarEventType).
_ALLOWED = (
    "event",
    "meeting",
    "vacation",
    "wfh",
    "holiday",
    "deadline",
    "dev",
    "update",
    "atlas_dev",
    "atlas_update",
)
_OLD = ("event", "meeting", "vacation", "wfh", "holiday", "deadline")


def _check(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return (
        "ALTER TABLE team_calendar_events "
        "DROP CONSTRAINT IF EXISTS team_calendar_events_event_type_check, "
        "ADD CONSTRAINT team_calendar_events_event_type_check "
        f"CHECK (event_type IN ({quoted}));"
    )


def upgrade() -> None:
    op.execute(_check(_ALLOWED))


def downgrade() -> None:
    op.execute(_check(_OLD))
