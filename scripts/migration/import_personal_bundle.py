#!/usr/bin/env python
"""P8.6 import a personal migration bundle into the central (company) node DB.

Imports the JSONL bundle produced by `export_personal_node.py` into the CURRENT
node DB (central), under the owner-only row boundary
(docs/p8-central-consolidation.md §3 / §6 P8.6 / §7):

  - documents: honor the canonical (source, source_account_id, source_external_id)
    + source_canonical_id idempotency contract; scope stays 'personal',
    user_id=target, project preserved.
  - board tree: workspace remapped onto the central (user_id, node_id) workspace;
    child UUIDs preserved; node_id rewritten to central; PK conflict skips+counts.
  - agent-work / policy / data-gap: owner_id=target, node_id rewritten to central.

Idempotent: a second import of the same bundle skips every row. `--compile`
runs `compile_personal_documents(target_user)` after import to build the
owner-scoped corpus + wiki centrally. The import is wrapped in the
`migration.import_personal` audit span and writes a `parity_report.json`.

  python scripts/migration/import_personal_bundle.py \
    --bundle /tmp/personal-bundle [--user-id <central-uuid>] [--node-id company] [--compile]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.audit import audit
from orthus.db import session
from orthus.settings import get_settings
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
    AGENT_TABLES,
    ALL_TABLE_NAMES,
    MANIFEST_NAME,
    PARITY_REPORT_NAME,
    json_to_row,
)

# Board tables whose rows hang off a workspace and are imported with node_id
# rewrite handled at the workspace; children only need PK-conflict skip+count.
_BOARD_CHILD_TABLES = {
    "personal_board_projects": personal_board_projects,
    "personal_board_folders": personal_board_folders,
    "personal_board_backlog_buckets": personal_board_backlog_buckets,
    "personal_board_integrations": personal_board_integrations,
    "personal_board_preferences": personal_board_preferences,
    "personal_board_tasks": personal_board_tasks,
    "personal_board_subtasks": personal_board_subtasks,
    "personal_board_fixed_events": personal_board_fixed_events,
    "personal_board_notes": personal_board_notes,
    "personal_weekly_objectives": personal_weekly_objectives,
    "personal_objective_subtasks": personal_objective_subtasks,
    "personal_objective_comments": personal_objective_comments,
}


class ImportStats:
    """Per-table tally: bundle rows seen, imported, skipped, skipped_unmapped.

    `skipped` counts idempotent re-import no-ops (row already present);
    `skipped_unmapped` counts rows dropped fail-closed because their source
    workspace_id had no entry in the remap (data loss — flagged in parity)."""

    def __init__(self) -> None:
        self.imported = 0
        self.skipped = 0
        self.skipped_unmapped = 0

    @property
    def total(self) -> int:
        return self.imported + self.skipped + self.skipped_unmapped


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    bundle = Path(args.bundle)
    manifest = _load_manifest(bundle)
    settings = get_settings()
    source_user = UUID(manifest["user_id"])
    target_user = UUID(args.user_id) if args.user_id else source_user
    node_id = (args.node_id or settings.node_id).strip()
    if not node_id:
        raise SystemExit("node-id required (set --node-id or ORTHUS_NODE_ID)")

    with audit("migration.import_personal") as span:
        span.add_meta(
            source_node_id=manifest.get("source_node_id"),
            target_node_id=node_id,
            source_user=str(source_user),
            target_user=str(target_user),
        )
        stats = _import_bundle(
            bundle,
            source_user=source_user,
            source_node_id=str(manifest.get("source_node_id") or "unknown"),
            target_user=target_user,
            node_id=node_id,
        )
        report = _build_parity_report(
            bundle, manifest, stats, target_user=target_user, node_id=node_id
        )
        span.add_meta(
            imported=sum(s.imported for s in stats.values()),
            skipped=sum(s.skipped for s in stats.values()),
            skipped_unmapped=sum(s.skipped_unmapped for s in stats.values()),
            parity_ok=report["parity_ok"],
        )

    (bundle / PARITY_REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_summary(report)

    if not report["parity_ok"]:
        print(
            "PARITY MISMATCH: count mismatch or fail-closed dropped rows "
            "(skipped_unmapped) for one or more tables"
        )
        return 1

    if args.compile:
        from orthus.collector.compile import compile_personal_documents

        result = compile_personal_documents(target_user)
        print(
            f"compile: indexed={result.indexed} authored={result.authored} "
            f"skipped={result.skipped} failed={result.failed}"
        )
    return 0


def _import_bundle(
    bundle: Path,
    *,
    source_user: UUID,
    source_node_id: str,
    target_user: UUID,
    node_id: str,
) -> dict[str, ImportStats]:
    stats: dict[str, ImportStats] = {name: ImportStats() for name in ALL_TABLE_NAMES}

    for row in _read_jsonl(bundle, "documents"):
        _import_document(row, target_user, stats["documents"])

    # The central workspace the source board remaps onto: ensure (target_user,
    # node_id) and build source-workspace-id -> central-workspace-id.
    ws_rows = list(_read_jsonl(bundle, "personal_board_workspaces"))
    ws_map = _import_workspaces(ws_rows, target_user, node_id, stats["personal_board_workspaces"])
    _assert_remap_targets_exist(ws_map)

    for name, table in _BOARD_CHILD_TABLES.items():
        for row in _read_jsonl(bundle, name):
            _import_board_child(row, table, ws_map, target_user, stats[name])

    for name, table in AGENT_TABLES:
        for row in _read_jsonl(bundle, name):
            _import_agent_row(
                row,
                table,
                source_user=source_user,
                source_node_id=source_node_id,
                target_user=target_user,
                node_id=node_id,
                stat=stats[name],
            )

    return stats


# --- documents ------------------------------------------------------------------


def _import_document(row: dict, target_user: UUID, stat: ImportStats) -> None:
    """Insert (or recognize as existing) one personal document under target_user.

    Reuses the canonical identity contract: a source_canonical_id dedupes within
    the owner's personal scope, else (source, source_account_id, source_external_id).
    An already-present identity is a skip (idempotent re-import).

    `source_account_id` is nulled: connector accounts are not migrated, so the
    bundle's original account id has no `connector_accounts` row in central and
    would violate the documents->connector_accounts FK. This mirrors the
    collector-ingest convention (orthus/collector/ingest.py), and the identity
    lookup below reads the nulled value so re-import idempotency all-skips. The
    original account ids stay in the bundle JSONL as the provenance record."""
    payload = dict(row)
    payload["user_id"] = target_user
    payload["scope"] = "personal"
    payload["source_account_id"] = None
    with session() as s:
        existing = s.execute(
            select(documents.c.doc_id).where(*_document_identity(payload, target_user))
        ).first()
        if existing is not None:
            stat.skipped += 1
            return
        s.execute(insert(documents).values(**payload))
        s.commit()
    stat.imported += 1


def _document_identity(payload: dict, target_user: UUID) -> list:
    source = payload.get("source")
    canonical = payload.get("source_canonical_id")
    if canonical:
        return [
            documents.c.source == source,
            documents.c.scope == "personal",
            documents.c.user_id == target_user,
            documents.c.source_canonical_id == canonical,
        ]
    account_id = payload.get("source_account_id")
    return [
        documents.c.scope == "personal",
        documents.c.user_id == target_user,
        documents.c.source == source,
        documents.c.source_account_id.is_(None)
        if account_id is None
        else documents.c.source_account_id == account_id,
        documents.c.source_external_id == payload.get("source_external_id"),
    ]


# --- board ----------------------------------------------------------------------


def _import_workspaces(
    ws_rows: list[dict], target_user: UUID, node_id: str, stat: ImportStats
) -> dict[UUID, UUID]:
    """Remap each source workspace onto the central (target_user, node_id) one.

    The central node serves one workspace per (user_id, node_id); every source
    workspace's children re-parent onto that single central workspace. Returns
    source-workspace-id -> central-workspace-id. Counts a remap that lands on an
    existing central workspace as skipped, a freshly created one as imported."""
    central_id, created = _ensure_central_workspace(target_user, node_id)
    ws_map: dict[UUID, UUID] = {}
    for row in ws_rows:
        ws_map[row["workspace_id"]] = central_id
    if created and ws_rows:
        stat.imported += 1
        stat.skipped += max(0, len(ws_rows) - 1)
    else:
        stat.skipped += len(ws_rows)
    return ws_map


def _ensure_central_workspace(target_user: UUID, node_id: str) -> tuple[UUID, bool]:
    with session() as s:
        existing = s.execute(
            select(personal_board_workspaces.c.workspace_id).where(
                personal_board_workspaces.c.user_id == target_user,
                personal_board_workspaces.c.node_id == node_id,
            )
        ).first()
        if existing is not None:
            return existing.workspace_id, False
        new_id = _deterministic_workspace_id(target_user, node_id)
        s.execute(
            insert(personal_board_workspaces).values(
                workspace_id=new_id,
                user_id=target_user,
                node_id=node_id,
                name="개인 워크스페이스",
                timezone="Asia/Seoul",
            )
        )
        s.commit()
        return new_id, True


def _assert_remap_targets_exist(ws_map: dict[UUID, UUID]) -> None:
    """Fail the run cleanly if any remapped central workspace id is missing.

    Every target id in `ws_map` was created (or found) during workspace import,
    so it must exist in central before children re-parent onto it. A missing id
    means the workspace phase did not persist — abort rather than orphan."""
    targets = set(ws_map.values())
    if not targets:
        return
    with session() as s:
        present = set(
            s.execute(
                select(personal_board_workspaces.c.workspace_id).where(
                    personal_board_workspaces.c.workspace_id.in_(targets)
                )
            ).scalars()
        )
    missing = targets - present
    if missing:
        raise SystemExit(
            "remap target workspace(s) missing from central after import: "
            + ", ".join(str(m) for m in sorted(missing, key=str))
        )


# Fixed namespace for deterministic central-workspace ids (uuid5). The tool is
# unreleased, so this constant must stay stable from here on.
_WORKSPACE_NAMESPACE = UUID("6f2c0d1e-8a3b-4c5d-9e7f-0a1b2c3d4e5f")


def _deterministic_workspace_id(target_user: UUID, node_id: str) -> UUID:
    return uuid.uuid5(_WORKSPACE_NAMESPACE, f"{node_id}:{target_user}")


def _import_board_child(
    row: dict, table, ws_map: dict[UUID, UUID], target_user: UUID, stat: ImportStats
) -> None:
    """Insert one board child row with its workspace_id remapped onto central.

    Child UUIDs are preserved; on PK conflict (row already imported) we skip and
    count, keeping the import idempotent. A row whose source workspace_id has no
    remap entry is dropped fail-closed (counted as skipped_unmapped) rather than
    inserted under the original id, which would FK-fail or orphan the row. Child
    tables without a workspace_id column (subtasks / objective children) re-parent
    via their FK chain and need no remap.

    `user_id` is rewritten to target_user when the column is present: several
    board child tables (preferences, tasks, fixed_events, notes, objective_comments)
    carry a direct user_id FK that references `users`; the personal source UUID
    has no row in the central `users` table."""
    payload = dict(row)
    if "workspace_id" in payload:
        source_ws = payload["workspace_id"]
        if source_ws not in ws_map:
            stat.skipped_unmapped += 1
            print(
                f"  skip unmapped {table.name} row: workspace_id {source_ws} "
                "not in bundle remap (dropped)",
                file=sys.stderr,
            )
            return
        payload["workspace_id"] = ws_map[source_ws]
    if "user_id" in table.c:
        payload["user_id"] = target_user
    if _insert_skip_conflict(table, payload):
        stat.imported += 1
    else:
        stat.skipped += 1


# --- agent-work / policy / data-gap ---------------------------------------------


def _import_agent_row(
    row: dict,
    table,
    *,
    source_user: UUID,
    source_node_id: str,
    target_user: UUID,
    node_id: str,
    stat: ImportStats,
) -> None:
    """Insert one agent-work/policy/data-gap row with owner stamping + node rewrite.

    owner_id is always stamped to the target user when the table has it: P8.6
    imports personal rows into central, and NULL owner_id would become shared
    company-visible in several read paths. Reviewer/creator ids are also mapped
    to the central target owner for the same personal-node migration boundary.
    PK conflict skips+counts."""
    payload = dict(row)
    if "owner_id" in table.c:
        payload["owner_id"] = target_user
    if "node_id" in table.c:
        payload["node_id"] = node_id
    if table is agent_work_items and payload.get("source_ref_id"):
        payload["source_ref_id"] = _migrated_agent_source_ref(
            source_node_id, source_user, str(payload["source_ref_id"])
        )
        if isinstance(payload.get("payload"), dict):
            payload["payload"] = {
                **payload["payload"],
                "_p8_migration": {
                    "source_node_id": source_node_id,
                    "source_user": str(source_user),
                    "source_ref_id": row.get("source_ref_id"),
                },
            }
    if table is agent_policy_observations and payload.get("source_ref_id"):
        payload["source_ref_id"] = _migrated_agent_source_ref(
            source_node_id, source_user, str(payload["source_ref_id"])
        )
    if "reviewer_id" in table.c:
        payload["reviewer_id"] = target_user
    if "created_by" in table.c and payload.get("created_by") is not None:
        payload["created_by"] = target_user
    if _insert_skip_conflict(table, payload):
        stat.imported += 1
    else:
        stat.skipped += 1


def _migrated_agent_source_ref(source_node_id: str, source_user: UUID, source_ref_id: str) -> str:
    return f"p8:{source_node_id}:{source_user}:{source_ref_id}"


def _insert_skip_conflict(table, payload: dict) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING, returning True iff a row was inserted.

    psycopg reports `rowcount == -1` for a conflict no-op, so a RETURNING of the
    primary key is the reliable insert/skip signal for the idempotent re-import."""
    pk_col = list(table.primary_key.columns)[0]
    stmt = pg_insert(table).values(**payload).on_conflict_do_nothing().returning(pk_col)
    with session() as s:
        inserted = s.execute(stmt).first() is not None
        s.commit()
    return inserted


# --- parity report --------------------------------------------------------------


def _build_parity_report(
    bundle: Path,
    manifest: dict,
    stats: dict[str, ImportStats],
    *,
    target_user: UUID,
    node_id: str,
) -> dict:
    tables: dict[str, dict] = {}
    parity_ok = True
    document_identity_collisions = _document_identity_collisions(bundle)
    document_checks = _document_spot_checks(bundle, target_user)
    document_checks_ok = all(c["match"] for c in document_checks)
    for name in ALL_TABLE_NAMES:
        stat = stats[name]
        bundle_count = manifest.get("counts", {}).get(name, stat.total)
        db_count = _owner_db_count(name, bundle, target_user, node_id)
        # Strict: count mismatch, fail-closed dropped row, or missing landed row
        # fails parity. Workspace rows are remapped many->one by design, so the
        # landed workspace count is informational only for that parent table.
        db_count_ok = name == "personal_board_workspaces" or db_count == bundle_count
        table_ok = stat.total == bundle_count and stat.skipped_unmapped == 0 and db_count_ok
        if name == "documents" and (document_identity_collisions or not document_checks_ok):
            table_ok = False
        parity_ok = parity_ok and table_ok
        tables[name] = {
            "bundle": bundle_count,
            "imported": stat.imported,
            "skipped": stat.skipped,
            "skipped_unmapped": stat.skipped_unmapped,
            "post_import_db_owner": db_count,
            "ok": table_ok,
        }
        if name == "documents":
            tables[name]["identity_collisions"] = document_identity_collisions

    parity_ok = parity_ok and document_checks_ok

    return {
        "source_node_id": manifest.get("source_node_id"),
        "target_node_id": node_id,
        "source_user": manifest.get("user_id"),
        "target_user": str(target_user),
        "tables": tables,
        "document_identity_collisions": document_identity_collisions,
        "document_spot_checks": document_checks,
        "parity_ok": parity_ok,
    }


def _owner_db_count(name: str, bundle: Path, target_user: UUID, node_id: str) -> int:
    if name == "documents":
        identities = [
            _document_identity({**row, "source_account_id": None}, target_user)
            for row in _read_jsonl(bundle, "documents")
        ]
        if not identities:
            return 0
        with session() as s:
            return sum(
                1
                for identity in identities
                if s.execute(select(documents.c.doc_id).where(*identity)).first()
            )
    if name in _BOARD_CHILD_TABLES or name == "personal_board_workspaces":
        return _board_owner_count(name, target_user, node_id)
    return _agent_owner_count(name, bundle, target_user)


def _agent_owner_count(name: str, bundle: Path, target_user: UUID) -> int:
    if name == "agent_work_items":
        work_ids = _bundle_uuid_set(bundle, name, "work_id")
        if not work_ids:
            return 0
        return _count(
            agent_work_items,
            agent_work_items.c.work_id.in_(work_ids),
            agent_work_items.c.owner_id == target_user,
        )
    if name == "agent_work_decisions":
        work_ids = _bundle_uuid_set(bundle, name, "work_id")
        if not work_ids:
            return 0
        return _count(agent_work_decisions, agent_work_decisions.c.work_id.in_(work_ids))
    if name == "agent_policy_observations":
        observation_ids = _bundle_uuid_set(bundle, name, "observation_id")
        if not observation_ids:
            return 0
        return _count(
            agent_policy_observations,
            agent_policy_observations.c.observation_id.in_(observation_ids),
            agent_policy_observations.c.owner_id == target_user,
        )
    if name == "data_gaps":
        gap_ids = _bundle_uuid_set(bundle, name, "gap_id")
        if not gap_ids:
            return 0
        return _count(
            data_gaps,
            data_gaps.c.gap_id.in_(gap_ids),
            data_gaps.c.scope == "personal",
            data_gaps.c.owner_id == target_user,
        )
    return 0


def _board_owner_count(name: str, target_user: UUID, node_id: str) -> int:
    """Owner-scoped post-import count for a board table via the central workspace."""
    with session() as s:
        ws = s.execute(
            select(personal_board_workspaces.c.workspace_id).where(
                personal_board_workspaces.c.user_id == target_user,
                personal_board_workspaces.c.node_id == node_id,
            )
        ).first()
        if name == "personal_board_workspaces":
            return 1 if ws else 0
        if ws is None:
            return 0
        ws_id = ws.workspace_id
        table = _BOARD_CHILD_TABLES[name]
        if "workspace_id" in table.c:
            return s.execute(
                select(func.count()).select_from(table).where(table.c.workspace_id == ws_id)
            ).scalar_one()
        # subtasks (task_id), objective subtasks/comments (objective_id): count
        # via the FK chain back to the central workspace.
        return _board_child_count_via_parent(s, name, ws_id)


def _board_child_count_via_parent(s, name: str, ws_id: UUID) -> int:
    if name == "personal_board_subtasks":
        task_ids = select(personal_board_tasks.c.task_id).where(
            personal_board_tasks.c.workspace_id == ws_id
        )
        return s.execute(
            select(func.count())
            .select_from(personal_board_subtasks)
            .where(personal_board_subtasks.c.task_id.in_(task_ids))
        ).scalar_one()
    obj_ids = select(personal_weekly_objectives.c.objective_id).where(
        personal_weekly_objectives.c.workspace_id == ws_id
    )
    table = (
        personal_objective_subtasks
        if name == "personal_objective_subtasks"
        else personal_objective_comments
    )
    return s.execute(
        select(func.count()).select_from(table).where(table.c.objective_id.in_(obj_ids))
    ).scalar_one()


def _count(table, *clauses) -> int:
    with session() as s:
        stmt = select(func.count()).select_from(table)
        if clauses:
            stmt = stmt.where(*clauses)
        return s.execute(stmt).scalar_one()


def _document_spot_checks(bundle: Path, target_user: UUID) -> list[dict]:
    """Content-hash checks for every bundle document.

    This catches pre-existing central identities with different content as well
    as bundle rows that collapsed onto one nulled-account identity."""
    checks: list[dict] = []
    for row in _read_jsonl(bundle, "documents"):
        bundle_hash = _markdown_hash(row.get("markdown") or "")
        # Match the import: source_account_id is nulled, so the stored row is
        # found by the nulled identity rather than the bundle's original id.
        identity = _document_identity(
            {**row, "user_id": target_user, "source_account_id": None}, target_user
        )
        with session() as s:
            db_md = s.execute(select(documents.c.markdown).where(*identity)).scalar()
        db_hash = _markdown_hash(db_md or "") if db_md is not None else None
        checks.append(
            {
                "source_external_id": row.get("source_external_id"),
                "bundle_hash": bundle_hash,
                "db_hash": db_hash,
                "match": db_hash == bundle_hash,
            }
        )
    return checks


def _bundle_uuid_set(bundle: Path, name: str, key: str) -> set[UUID]:
    ids: set[UUID] = set()
    for row in _read_jsonl(bundle, name):
        value = row.get(key)
        if isinstance(value, UUID):
            ids.add(value)
    return ids


def _document_identity_collisions(bundle: Path) -> list[dict]:
    """Detect source rows that collapse onto one central document identity.

    P8.6 nulls `source_account_id` during import because connector accounts are
    not migrated. That means two source documents with the same canonical id or
    `(source, source_external_id)` would otherwise become one inserted row plus
    one silent skip. Treat that as parity failure, not idempotency.
    """
    seen: dict[tuple[str, str, str], dict] = {}
    collisions: list[dict] = []
    for row in _read_jsonl(bundle, "documents"):
        key = _document_bundle_identity(row)
        previous = seen.get(key)
        current = {
            "doc_id": str(row.get("doc_id")),
            "source": row.get("source"),
            "source_external_id": row.get("source_external_id"),
            "source_canonical_id": row.get("source_canonical_id"),
        }
        if previous is not None:
            collisions.append(
                {
                    "identity": ":".join(key),
                    "first": previous,
                    "duplicate": current,
                }
            )
            continue
        seen[key] = current
    return collisions


def _document_bundle_identity(row: dict) -> tuple[str, str, str]:
    source = str(row.get("source") or "")
    canonical = row.get("source_canonical_id")
    if canonical:
        return ("canonical", source, str(canonical))
    return ("external", source, str(row.get("source_external_id") or ""))


def _markdown_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


# --- io -------------------------------------------------------------------------


def _read_jsonl(bundle: Path, name: str):
    path = bundle / f"{name}.jsonl"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json_to_row(line)


def _load_manifest(bundle: Path) -> dict:
    path = bundle / MANIFEST_NAME
    if not path.exists():
        raise SystemExit(f"no manifest at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _print_summary(report: dict) -> None:
    print(
        f"import {report['source_user']} -> user {report['target_user']} "
        f"on node {report['target_node_id']!r}"
    )
    print(
        f"{'table':<32} {'bundle':>7} {'imported':>9} {'skipped':>8} "
        f"{'unmapped':>9} {'db_owner':>9} ok"
    )
    for name, t in report["tables"].items():
        flag = "ok" if t["ok"] else "FAIL"
        print(
            f"{name:<32} {t['bundle']:>7} {t['imported']:>9} "
            f"{t['skipped']:>8} {t.get('skipped_unmapped', 0):>9} "
            f"{t['post_import_db_owner']:>9} {flag}"
        )
    matched = sum(1 for c in report["document_spot_checks"] if c["match"])
    print(f"document spot checks: {matched}/{len(report['document_spot_checks'])} matched")
    if report.get("document_identity_collisions"):
        print(
            f"document identity collisions: {len(report['document_identity_collisions'])} "
            "(would collapse during import)"
        )
    print(f"parity_ok: {report['parity_ok']}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P8.6 import personal bundle into central")
    parser.add_argument("--bundle", required=True, help="bundle directory from export")
    parser.add_argument("--user-id", help="target central user id (default = source user)")
    parser.add_argument("--node-id", help="central node id (default = ORTHUS_NODE_ID)")
    parser.add_argument(
        "--compile",
        action="store_true",
        help="run compile_personal_documents(target_user) after import",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
