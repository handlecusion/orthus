"""Shared bundle contract for the P8.6 export/import scripts.

Defines the per-table JSONL filenames + the source-side row-selection strategy,
plus safe JSON (de)serialization for UUID / datetime / date / time / Decimal
column values. Both `export_personal_node.py` and `import_personal_bundle.py`
import from here so the table list and column handling stay in one place.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Table

from orthus.tables import (
    agent_policy_observations,
    agent_work_decisions,
    agent_work_items,
    data_gaps,
    personal_board_backlog_buckets,
    personal_board_fixed_events,
    personal_board_folders,
    personal_board_integrations,
    personal_board_notes,
    personal_board_preferences,
    personal_board_projects,
    personal_board_subtasks,
    personal_board_tasks,
    personal_board_workspaces,
    personal_objective_comments,
    personal_objective_subtasks,
    personal_weekly_objectives,
)

MANIFEST_NAME = "manifest.json"
PARITY_REPORT_NAME = "parity_report.json"

# The board tree, listed parent-before-child so import honors FK order. Each
# table is keyed by the JSONL filename it serializes to.
BOARD_TABLES: list[tuple[str, Table]] = [
    ("personal_board_workspaces", personal_board_workspaces),
    ("personal_board_projects", personal_board_projects),
    ("personal_board_folders", personal_board_folders),
    ("personal_board_backlog_buckets", personal_board_backlog_buckets),
    ("personal_board_integrations", personal_board_integrations),
    ("personal_board_preferences", personal_board_preferences),
    ("personal_board_tasks", personal_board_tasks),
    ("personal_board_subtasks", personal_board_subtasks),
    ("personal_board_fixed_events", personal_board_fixed_events),
    ("personal_board_notes", personal_board_notes),
    ("personal_weekly_objectives", personal_weekly_objectives),
    ("personal_objective_subtasks", personal_objective_subtasks),
    ("personal_objective_comments", personal_objective_comments),
]

# Agent-work / policy / data-gap tables imported with owner_id=target and
# node_id rewritten. Ordered so append-only children land after their parents.
AGENT_TABLES: list[tuple[str, Table]] = [
    ("agent_work_items", agent_work_items),
    ("agent_work_decisions", agent_work_decisions),
    ("agent_policy_observations", agent_policy_observations),
    ("data_gaps", data_gaps),
]

# Convenience: every table name the bundle carries, documents first.
ALL_TABLE_NAMES: list[str] = (
    ["documents"] + [name for name, _ in BOARD_TABLES] + [name for name, _ in AGENT_TABLES]
)


def _encode(value: object) -> object:
    if isinstance(value, UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, time):
        return {"__time__": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    return value


def _decode(value: object) -> object:
    if isinstance(value, dict) and len(value) == 1:
        if "__uuid__" in value:
            return UUID(value["__uuid__"])
        if "__datetime__" in value:
            return datetime.fromisoformat(value["__datetime__"])
        if "__date__" in value:
            return date.fromisoformat(value["__date__"])
        if "__time__" in value:
            return time.fromisoformat(value["__time__"])
        if "__decimal__" in value:
            return Decimal(value["__decimal__"])
    return value


def row_to_json(row_mapping: dict) -> str:
    """Serialize one DB row (column->value) to a single JSONL line.

    JSONB columns are already native Python (dict/list); scalar UUID/datetime/
    date/time/Decimal values are wrapped in a tagged object so the import side
    reconstructs the exact Python type.
    """
    encoded = {key: _encode(val) for key, val in row_mapping.items()}
    return json.dumps(encoded, ensure_ascii=False)


def json_to_row(line: str) -> dict:
    """Parse one JSONL line back into a column->value dict with native types."""
    raw = json.loads(line)
    return {key: _decode(val) for key, val in raw.items()}
