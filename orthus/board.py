"""Board module: list and update kanban status for the Nova 개발 로드맵 DB.

Reads notion_rows (company scope, db_name = BOARD_DB_NAME) and applies a local
status overlay from task_states. Drag-and-drop first records a local overlay,
then attempts explicit Notion write-back. Successful Notion writes update the
local Notion row snapshot and clear the overlay.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.audit.logger import audit
from orthus.connectors.notion_writeback import push_page_status
from orthus.db import session
from orthus.schemas.canonical import BoardCard
from orthus.settings import get_settings
from orthus.tables import notion_rows, task_states

BOARD_DB_NAME = "Nova 개발 로드맵"

STATUS_COLUMNS = ["Todo", "In Progress", "Review", "Done"]

# Explicit Notion-status → board-column mapping. Any unmapped value falls to "Todo".
STATUS_MAP: dict[str, str] = {
    "Not Started": "Todo",
    "Ready": "Todo",
    "In Progress": "In Progress",
    "Review": "Review",
    "Done": "Done",
}

# Board-column → Notion-status mapping for explicit write-back.
BOARD_TO_NOTION_STATUS: dict[str, str] = {
    "Todo": "Not Started",
    "In Progress": "In Progress",
    "Review": "Review",
    "Done": "Done",
}

# Property keys to inspect for title, assignee, priority, due date.
_TITLE_KEYS = ["제목", "이름", "name", "title", "Name", "Title"]
_ASSIGNEE_KEYS = ["담당자", "Assignee", "assignee"]
_PRIORITY_KEYS = ["우선순위", "Priority", "priority"]
_DUE_KEYS = ["종료일", "Due", "due", "마감일"]


def _resolve_status(notion_status: str | None) -> str:
    if notion_status is None:
        return "Todo"
    return STATUS_MAP.get(notion_status, "Todo")


def _pick(props: dict, keys: list[str]) -> str | None:
    for k in keys:
        v = props.get(k)
        if v:
            return str(v)
    return None


def _parse_due(props: dict) -> datetime | None:
    for k in _DUE_KEYS:
        v = props.get(k)
        if v:
            try:
                return datetime.fromisoformat(str(v).rstrip("Z"))
            except (ValueError, TypeError):
                pass
    return None


def _build_card(
    *,
    row_id: UUID,
    props: dict,
    status: str,
    is_local_override: bool,
    notion_status: str | None = None,
    writeback_status: str | None = None,
    writeback_error: str | None = None,
) -> BoardCard:
    if notion_status is None:
        notion_status = props.get("상태")
    return BoardCard(
        row_id=row_id,
        title=_pick(props, _TITLE_KEYS) or "(제목 없음)",
        status=status,
        notion_status=notion_status,
        assignee=_pick(props, _ASSIGNEE_KEYS),
        priority=_pick(props, _PRIORITY_KEYS),
        due_at=_parse_due(props),
        is_local_override=is_local_override,
        writeback_status=writeback_status,
        writeback_error=writeback_error,
    )


def _resolve_writeback_token(explicit_token: str | None) -> str:
    if explicit_token is not None:
        return explicit_token
    settings = get_settings()
    # Nova board write-back should prefer the scoped Nova token when present.
    return settings.conn_notion_token_2 or settings.conn_notion_token


def list_board(user_id: UUID) -> list[BoardCard]:
    """Return all cards for the Nova 개발 로드맵 DB (company scope).

    Applies task_states overlay via LEFT JOIN so drag-and-drop results survive
    without touching Notion.
    """
    with audit("board.list") as span:
        with session() as s:
            rows = s.execute(
                select(
                    notion_rows.c.row_id,
                    notion_rows.c.properties,
                    task_states.c.status.label("overlay_status"),
                )
                .outerjoin(
                    task_states,
                    task_states.c.row_id == notion_rows.c.row_id,
                )
                .where(
                    notion_rows.c.db_name == BOARD_DB_NAME,
                    notion_rows.c.scope == "company",
                )
            ).all()

        cards = []
        for row_id, props, overlay_status in rows:
            notion_status: str | None = props.get("상태") if props else None
            resolved = (
                overlay_status if overlay_status is not None else _resolve_status(notion_status)
            )
            cards.append(
                _build_card(
                    row_id=row_id,
                    props=props or {},
                    status=resolved,
                    notion_status=notion_status,
                    is_local_override=overlay_status is not None,
                    writeback_status="local_only" if overlay_status is not None else None,
                )
            )

        span.set_output({"count": len(cards)})
        return cards


def set_status(
    user_id: UUID,
    row_id: UUID,
    status: str,
    *,
    notion_token: str | None = None,
    notion_client: httpx.Client | None = None,
) -> BoardCard:
    """Update board status and attempt Notion write-back.

    Raises ValueError if status is not in STATUS_COLUMNS (route converts to 422).
    Raises LookupError if row_id not found in notion_rows for this DB/scope (404).
    """
    if status not in STATUS_COLUMNS:
        raise ValueError(f"invalid status {status!r}; must be one of {STATUS_COLUMNS}")

    with audit("board.set_status") as span:
        span.add_meta(row_id=str(row_id), status=status)

        with session() as s:
            row = s.execute(
                select(notion_rows.c.row_id, notion_rows.c.properties).where(
                    notion_rows.c.row_id == row_id,
                    notion_rows.c.db_name == BOARD_DB_NAME,
                    notion_rows.c.scope == "company",
                )
            ).first()

        if row is None:
            raise LookupError(f"row_id {row_id} not found in {BOARD_DB_NAME}")

        props = dict(row.properties or {})

        with session() as s:
            stmt = (
                pg_insert(task_states)
                .values(
                    row_id=row_id,
                    status=status,
                    updated_by=user_id,
                )
                .on_conflict_do_update(
                    index_elements=["row_id"],
                    set_={"status": status, "updated_by": user_id, "updated_at": func.now()},
                )
            )
            s.execute(stmt)
            s.commit()

        notion_status: str | None = props.get("상태")
        next_notion_status = BOARD_TO_NOTION_STATUS[status]
        result = push_page_status(
            str(row_id),
            next_notion_status,
            token=_resolve_writeback_token(notion_token),
            client=notion_client,
        )

        if result.status == "synced":
            synced_props = {**props, "상태": next_notion_status}
            with session() as s:
                s.execute(
                    update(notion_rows)
                    .where(notion_rows.c.row_id == row_id)
                    .values(properties=synced_props, updated_at=func.now())
                )
                s.execute(delete(task_states).where(task_states.c.row_id == row_id))
                s.commit()

            card = _build_card(
                row_id=row_id,
                props=synced_props,
                status=status,
                notion_status=next_notion_status,
                is_local_override=False,
                writeback_status="synced",
            )
            span.set_output({"row_id": str(row_id), "status": status, "writeback": result.status})
            return card

        card = _build_card(
            row_id=row_id,
            props=props,
            status=status,
            notion_status=notion_status,
            is_local_override=True,
            writeback_status=result.status,
            writeback_error=result.error,
        )
        span.set_output(
            {
                "row_id": str(row_id),
                "status": status,
                "writeback": result.status,
                "writeback_error": result.error,
            }
        )
        return card
