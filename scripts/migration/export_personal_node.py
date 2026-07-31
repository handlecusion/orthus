#!/usr/bin/env python
"""P8.6 export a personal node's owner data into a JSONL migration bundle.

Read-only on the source DB (SELECT only, via a readonly-flagged engine). Writes
one JSONL file per table plus a manifest with counts, the source node_id, and an
exported_at timestamp. Wiki pages/chunks/links are intentionally NOT exported —
they are recompiled centrally from the imported documents (P8.4).

  python scripts/migration/export_personal_node.py \
    --dsn postgresql+psycopg://orthus:orthus@localhost:5433/orthus_personal \
    --node-id personal-a \
    --user-id <owner-uuid> \
    --out /tmp/personal-bundle

docs/p8-central-consolidation.md §6 (P8.6) / §7.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import Table, create_engine, func, select
from sqlalchemy.engine import Engine

from orthus.tables import (
    agent_policy_observations,
    agent_work_decisions,
    agent_work_items,
    data_gaps,
    documents,
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

from scripts.migration.bundle import (
    ALL_TABLE_NAMES,
    MANIFEST_NAME,
    row_to_json,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    user_id = _parse_user_id(args.user_id)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = _readonly_engine(args.dsn)
    try:
        node_id = args.node_id.strip()
        null_owner_work_count = _count_legacy_null_owner_agent_work(engine, node_id)
        if null_owner_work_count and not args.allow_legacy_null_owner_agent_work:
            print(
                "refusing export: found "
                f"{null_owner_work_count} legacy Agent Work rows with owner_id=NULL for "
                f"node {node_id!r}. Confirm the source personal node is single-owner, "
                "then rerun with --allow-legacy-null-owner-agent-work.",
                file=sys.stderr,
            )
            return 2
        counts, null_owner_work_count = _export_all(engine, user_id, node_id, out_dir)
    finally:
        engine.dispose()

    manifest = {
        "version": 1,
        "source_node_id": node_id,
        "user_id": str(user_id),
        "exported_at": datetime.now(UTC).isoformat(),
        "counts": counts,
        "legacy_null_owner_agent_work_count": null_owner_work_count,
        "legacy_null_owner_agent_work_allowed": bool(args.allow_legacy_null_owner_agent_work),
        "legacy_null_owner_agent_work_policy": (
            "included only when explicitly allowed; valid only for single-owner legacy personal nodes"
        ),
    }
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total = sum(counts.values())
    print(f"exported {total} rows from node {node_id!r} for user {user_id} -> {out_dir}")
    for name in ALL_TABLE_NAMES:
        print(f"  {name}: {counts.get(name, 0)}")
    return 0


def _export_all(
    engine: Engine, user_id: UUID, node_id: str, out_dir: Path
) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        counts["documents"] = _write_rows(
            conn,
            "documents",
            select(documents).where(
                documents.c.scope == "personal", documents.c.user_id == user_id
            ),
            out_dir,
        )
        # Board tree: gather child rows by FK from the owner's workspaces so we
        # never depend on a (possibly absent) owner column on child tables.
        workspace_ids = _owner_workspace_ids(conn, user_id, node_id)
        for name, stmt in _board_export_specs(workspace_ids):
            counts[name] = _write_rows(conn, name, stmt, out_dir)
        for name, stmt in _agent_export_specs(user_id, node_id):
            counts[name] = _write_rows(conn, name, stmt, out_dir)
        null_owner_work_count = _legacy_null_owner_agent_work_count(conn, node_id)
    return counts, null_owner_work_count


def _board_export_specs(workspace_ids: list[UUID]) -> list[tuple[str, object]]:
    """Build a per-table SELECT scoped to the owner's workspace ids.

    Empty workspace set -> a WHERE that matches nothing (IN ()), so every board
    file is written empty rather than dumping another user's rows.
    """
    ws = personal_board_workspaces
    proj = personal_board_projects
    task = personal_board_tasks
    obj = personal_weekly_objectives

    def ws_scoped(table: Table):
        return select(table).where(table.c.workspace_id.in_(workspace_ids))

    project_stmt = select(proj).where(proj.c.workspace_id.in_(workspace_ids))
    task_stmt = select(task).where(task.c.workspace_id.in_(workspace_ids))
    task_ids_sub = select(task.c.task_id).where(task.c.workspace_id.in_(workspace_ids))
    obj_stmt = select(obj).where(obj.c.workspace_id.in_(workspace_ids))
    obj_ids_sub = select(obj.c.objective_id).where(obj.c.workspace_id.in_(workspace_ids))

    return [
        ("personal_board_workspaces", select(ws).where(ws.c.workspace_id.in_(workspace_ids))),
        ("personal_board_projects", project_stmt),
        ("personal_board_folders", ws_scoped(personal_board_folders)),
        ("personal_board_backlog_buckets", ws_scoped(personal_board_backlog_buckets)),
        ("personal_board_integrations", ws_scoped(personal_board_integrations)),
        ("personal_board_preferences", ws_scoped(personal_board_preferences)),
        ("personal_board_tasks", task_stmt),
        (
            "personal_board_subtasks",
            select(personal_board_subtasks).where(
                personal_board_subtasks.c.task_id.in_(task_ids_sub)
            ),
        ),
        ("personal_board_fixed_events", ws_scoped(personal_board_fixed_events)),
        ("personal_board_notes", ws_scoped(personal_board_notes)),
        ("personal_weekly_objectives", obj_stmt),
        (
            "personal_objective_subtasks",
            select(personal_objective_subtasks).where(
                personal_objective_subtasks.c.objective_id.in_(obj_ids_sub)
            ),
        ),
        (
            "personal_objective_comments",
            select(personal_objective_comments).where(
                personal_objective_comments.c.objective_id.in_(obj_ids_sub)
            ),
        ),
    ]


def _owner_workspace_ids(conn, user_id: UUID, node_id: str) -> list[UUID]:
    ws = personal_board_workspaces
    rows = conn.execute(
        select(ws.c.workspace_id).where(ws.c.user_id == user_id, ws.c.node_id == node_id)
    ).all()
    return [r.workspace_id for r in rows]


def _agent_export_specs(user_id: UUID, node_id: str) -> list[tuple[str, object]]:
    """Build owner-scoped Agent Work/data-gap SELECTs for a personal source node.

    Legacy personal nodes are effectively single-owner, so some old Agent Work
    rows may have `owner_id IS NULL`. Those rows are eligible only when they
    belong to the requested source node and personal node kind. Child tables are
    then scoped by the exported work_id set instead of by node_id, preventing a
    multi-user source DB from leaking another owner의 decisions/observations.
    """
    owner_work_ids = select(agent_work_items.c.work_id).where(
        agent_work_items.c.node_id == node_id,
        agent_work_items.c.node_kind == "personal",
        (agent_work_items.c.owner_id == user_id) | agent_work_items.c.owner_id.is_(None),
    )

    return [
        (
            "agent_work_items",
            select(agent_work_items).where(agent_work_items.c.work_id.in_(owner_work_ids)),
        ),
        (
            "agent_work_decisions",
            select(agent_work_decisions).where(agent_work_decisions.c.work_id.in_(owner_work_ids)),
        ),
        (
            "agent_policy_observations",
            select(agent_policy_observations).where(
                agent_policy_observations.c.work_id.in_(owner_work_ids)
            ),
        ),
        (
            "data_gaps",
            select(data_gaps).where(
                data_gaps.c.scope == "personal", data_gaps.c.owner_id == user_id
            ),
        ),
    ]


def _legacy_null_owner_agent_work_count(conn, node_id: str) -> int:
    row = conn.execute(
        select(func.count())
        .select_from(agent_work_items)
        .where(
            agent_work_items.c.node_id == node_id,
            agent_work_items.c.node_kind == "personal",
            agent_work_items.c.owner_id.is_(None),
        )
    ).scalar_one()
    return int(row)


def _count_legacy_null_owner_agent_work(engine: Engine, node_id: str) -> int:
    with engine.connect() as conn:
        return _legacy_null_owner_agent_work_count(conn, node_id)


def _write_rows(conn, name: str, stmt, out_dir: Path) -> int:
    path = out_dir / f"{name}.jsonl"
    written = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in conn.execute(stmt).mappings():
            fh.write(row_to_json(dict(row)))
            fh.write("\n")
            written += 1
    return written


def _readonly_engine(dsn: str) -> Engine:
    # postgresql_readonly flags the connection read-only so any accidental write
    # fails loudly; the export only issues SELECTs.
    return create_engine(
        dsn,
        future=True,
        pool_pre_ping=True,
        execution_options={"postgresql_readonly": True},
    )


def _parse_user_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise SystemExit(f"invalid --user-id: {raw}") from exc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P8.6 export personal node owner data")
    parser.add_argument("--dsn", required=True, help="source personal DB DSN (read-only)")
    parser.add_argument("--user-id", required=True, help="owner user id on the personal node")
    parser.add_argument(
        "--node-id", required=True, help="source personal node id, e.g. personal-a"
    )
    parser.add_argument("--out", required=True, help="output bundle directory")
    parser.add_argument(
        "--allow-legacy-null-owner-agent-work",
        action="store_true",
        help=(
            "allow owner_id=NULL legacy Agent Work rows after confirming the source "
            "personal node is single-owner"
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
