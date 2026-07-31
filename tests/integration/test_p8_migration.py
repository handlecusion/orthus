"""P8.6 acceptance: personal -> central migration tooling + parity report.

docs/p8-central-consolidation.md §6 (P8.6) / §7.

Spins up a scratch SOURCE Postgres DB inside the test container
(`orthus_test_p86_src`), migrates it to head, and seeds a personal owner with
documents + a full board tree + agent-work/policy + data-gaps. Then exports that
node to a JSONL bundle and imports the bundle into the MAIN test DB (company /
central). Asserts:

  - owner stamping: imported rows carry owner_id / user_id = target.
  - board-child user_id rewrite: rows with a direct user_id FK (preferences,
    tasks, ...) land on the central target user, even when it differs from the
    personal-node owner.
  - node_id rewrite: board + agent rows land on the central node_id.
  - counts parity: parity_report.json bundle == imported+skipped, db owner count.
  - second import is idempotent (everything skipped, no new rows).
  - --compile authors owner-scoped wiki pages (mock LLM/embedding).

The scratch DB is dropped on fixture teardown.
"""

from __future__ import annotations

import os
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, insert, select

from orthus.db import session
from orthus.schemas.canonical import SCHEMA_VERSION
from orthus.settings import get_settings
from orthus.tables import (
    agent_policy_observations,
    agent_work_decisions,
    agent_work_items,
    connector_accounts,
    corpus_chunks,
    data_gaps,
    documents,
    personal_board_backlog_buckets,
    personal_board_notes,
    personal_board_preferences,
    personal_board_projects,
    personal_board_subtasks,
    personal_board_tasks,
    personal_board_workspaces,
    personal_objective_subtasks,
    personal_weekly_objectives,
    users,
    wiki_pages,
)

from scripts.migration import export_personal_node, import_personal_bundle

SCRATCH_DB = "orthus_test_p86_src"
PG_CONTAINER = os.environ.get("PG_CONTAINER", "orthus_pg")
PG_USER = os.environ.get("PG_USER", "orthus")
SOURCE_NODE = "personal-a"
CENTRAL_NODE = "company"


def _pg(*sql_args: str, db: str = "postgres") -> None:
    subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-U", PG_USER, "-d", db, *sql_args],
        check=True,
        capture_output=True,
    )


def _scratch_dsn() -> str:
    # Derive host/port/creds from the active test DSN; only the DB name differs.
    base = get_settings().pg_dsn
    return base.rsplit("/", 1)[0] + f"/{SCRATCH_DB}"


@pytest.fixture
def source_db(pg):
    """Create + migrate a scratch source DB, yield its DSN, then drop it."""
    if not _docker_pg_available():
        pytest.skip("docker exec into the test Postgres container is unavailable")

    _pg("-c", f"DROP DATABASE IF EXISTS {SCRATCH_DB};")
    _pg("-c", f"CREATE DATABASE {SCRATCH_DB} OWNER {PG_USER};")
    dsn = _scratch_dsn()
    env = dict(os.environ, ORTHUS_PG_DSN=dsn)
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"], check=True, env=env, capture_output=True
    )
    try:
        yield dsn
    finally:
        # Drop connections then the DB.
        _pg(
            "-c",
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{SCRATCH_DB}';",
        )
        _pg("-c", f"DROP DATABASE IF EXISTS {SCRATCH_DB};")


def _docker_pg_available() -> bool:
    try:
        subprocess.run(["docker", "exec", PG_CONTAINER, "true"], check=True, capture_output=True)
        return True
    except Exception:
        return False


# --- source-DB seeding ----------------------------------------------------------


def _seed_source(dsn: str, owner: uuid.UUID) -> dict:
    """Seed the scratch source DB with a personal owner and a representative slice
    of every migrated surface. Returns the ids used for later assertions."""
    engine = create_engine(dsn, future=True)
    ws_id = uuid.uuid4()
    project_id = uuid.uuid4()
    bucket_id = uuid.uuid4()
    task_id = uuid.uuid4()
    subtask_id = uuid.uuid4()
    note_id = uuid.uuid4()
    objective_id = uuid.uuid4()
    obj_subtask_id = uuid.uuid4()
    work_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    observation_id = uuid.uuid4()
    gap_id = uuid.uuid4()
    account_id = uuid.uuid4()
    doc_ids = [uuid.uuid4(), uuid.uuid4()]
    source_ref_id = f"cmd-{work_id.hex[:8]}"
    now = datetime.now(UTC)

    with engine.begin() as c:
        c.execute(insert(users).values(user_id=owner, display_name="Owner"))
        # A personal connector account the source documents reference. Connector
        # accounts are NOT migrated, so the import must null source_account_id
        # rather than carry this id into central (where it has no FK target).
        c.execute(
            insert(connector_accounts).values(
                account_id=account_id,
                connector_slug="gws_gmail",
                account_kind="personal",
                node_id=SOURCE_NODE,
                scope="personal",
                owner_id=owner,
                auth_mode="gws_cli",
            )
        )
        for i, doc_id in enumerate(doc_ids):
            c.execute(
                insert(documents).values(
                    doc_id=doc_id,
                    user_id=owner,
                    title=f"개인 문서 {i}",
                    block_json=[],
                    markdown=f"개인 노트 본문 {i} 한 줄.",
                    source="gws_gmail",
                    source_account_id=account_id,
                    source_external_id=f"msg-{i}",
                    source_canonical_id=None,
                    schema_version=SCHEMA_VERSION,
                    scope="personal",
                    project="company",
                    created_at=now,
                    updated_at=now,
                )
            )
        c.execute(
            insert(personal_board_workspaces).values(
                workspace_id=ws_id, user_id=owner, node_id=SOURCE_NODE, name="개인 WS"
            )
        )
        c.execute(
            insert(personal_board_projects).values(
                project_id=project_id, workspace_id=ws_id, name="프로젝트 A"
            )
        )
        c.execute(
            insert(personal_board_backlog_buckets).values(
                bucket_id=bucket_id,
                workspace_id=ws_id,
                key="todo",
                label="할 일",
                badge="T",
                color="#fff",
                order_index=0,
            )
        )
        c.execute(
            insert(personal_board_tasks).values(
                task_id=task_id,
                workspace_id=ws_id,
                user_id=owner,
                project_id=project_id,
                backlog_bucket_id=bucket_id,
                title="작업 하나",
                status="open",
                scheduled_date=None,
            )
        )
        c.execute(
            insert(personal_board_subtasks).values(
                subtask_id=subtask_id, task_id=task_id, title="하위 작업", order_index=0
            )
        )
        c.execute(
            insert(personal_board_notes).values(
                note_id=note_id,
                workspace_id=ws_id,
                user_id=owner,
                note_date=now.date(),
                kind="note",
                title="메모",
                body="메모 본문",
            )
        )
        # A preferences row keys on (workspace_id, user_id) and carries a direct
        # user_id FK to `users`; with a differing central target user the import
        # must rewrite user_id (regression target for the board-child FK fix).
        c.execute(
            insert(personal_board_preferences).values(
                workspace_id=ws_id,
                user_id=owner,
                right_panel="backlog",
            )
        )
        c.execute(
            insert(personal_weekly_objectives).values(
                objective_id=objective_id,
                workspace_id=ws_id,
                week_start=now.date(),
                title="주간 목표",
            )
        )
        c.execute(
            insert(personal_objective_subtasks).values(
                subtask_id=obj_subtask_id, objective_id=objective_id, title="목표 하위"
            )
        )
        c.execute(
            insert(agent_work_items).values(
                work_id=work_id,
                node_id=SOURCE_NODE,
                node_kind="personal",
                owner_id=owner,
                source_kind="assistant_command",
                source_ref_id=source_ref_id,
                action_family="personal_board_cleanup",
                title="정리",
                payload={"intake": "ask"},
                state="pending",
            )
        )
        c.execute(
            insert(agent_work_decisions).values(
                decision_id=decision_id,
                work_id=work_id,
                node_id=SOURCE_NODE,
                reviewer_id=owner,
                decision="approve",
                from_state="pending",
                to_state="resolved",
            )
        )
        c.execute(
            insert(agent_policy_observations).values(
                observation_id=observation_id,
                node_id=SOURCE_NODE,
                node_kind="personal",
                owner_id=owner,
                work_id=work_id,
                decision_id=decision_id,
                reviewer_id=owner,
                source_kind="assistant_command",
                source_ref_id=source_ref_id,
                action_family="personal_board_cleanup",
                policy_outcome="auto_execute",
                reviewer_decision="approve",
                from_state="pending",
                to_state="resolved",
                bucket_key="personal_board_cleanup",
            )
        )
        c.execute(
            insert(data_gaps).values(
                gap_id=gap_id,
                scope="personal",
                owner_id=owner,
                node_id=SOURCE_NODE,
                question_norm="질문",
                question="질문?",
                reason="no_ground",
            )
        )
    engine.dispose()
    return {
        "ws_id": ws_id,
        "task_id": task_id,
        "subtask_id": subtask_id,
        "objective_id": objective_id,
        "obj_subtask_id": obj_subtask_id,
        "work_id": work_id,
        "gap_id": gap_id,
        "account_id": account_id,
        "doc_ids": doc_ids,
        "source_ref_id": source_ref_id,
    }


# --- helpers --------------------------------------------------------------------


def _export(dsn: str, owner: uuid.UUID, out_dir, *, allow_legacy_null_owner: bool = False) -> int:
    argv = ["--dsn", dsn, "--node-id", SOURCE_NODE, "--user-id", str(owner), "--out", str(out_dir)]
    if allow_legacy_null_owner:
        argv.append("--allow-legacy-null-owner-agent-work")
    return export_personal_node.main(argv)


def _import(out_dir, *, compile_flag: bool = False) -> int:
    argv = ["--bundle", str(out_dir)]
    if compile_flag:
        argv.append("--compile")
    return import_personal_bundle.main(argv)


def _count(table, *clauses) -> int:
    with session() as s:
        stmt = select(func.count()).select_from(table)
        if clauses:
            stmt = stmt.where(*clauses)
        return s.execute(stmt).scalar_one()


def _jsonl_rows(path: Path) -> list[dict]:
    import json

    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- tests ----------------------------------------------------------------------


def test_export_then_import_stamps_owner_and_rewrites_node(clean, source_db, tmp_path):
    owner = uuid.uuid4()
    ids = _seed_source(source_db, owner)
    # The target central user must exist (the import does not create users).
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Central Owner"))
        s.commit()

    out = tmp_path / "bundle"
    assert _export(source_db, owner, out) == 0
    assert _import(out) == 0

    # documents: owner-scoped, personal, both imported.
    assert _count(documents, documents.c.scope == "personal", documents.c.user_id == owner) == 2

    # board: workspace remapped onto the central (owner, company) workspace;
    # children preserved their UUIDs and re-parented.
    with session() as s:
        central_ws = s.execute(
            select(personal_board_workspaces.c.workspace_id).where(
                personal_board_workspaces.c.user_id == owner,
                personal_board_workspaces.c.node_id == CENTRAL_NODE,
            )
        ).scalar_one()
        task_ws = s.execute(
            select(personal_board_tasks.c.workspace_id).where(
                personal_board_tasks.c.task_id == ids["task_id"]
            )
        ).scalar_one()
    assert task_ws == central_ws, "task re-parented onto the central workspace"
    assert (
        _count(personal_board_subtasks, personal_board_subtasks.c.subtask_id == ids["subtask_id"])
        == 1
    )
    assert (
        _count(
            personal_objective_subtasks,
            personal_objective_subtasks.c.subtask_id == ids["obj_subtask_id"],
        )
        == 1
    )

    # agent-work / policy / data-gap: owner stamped, node rewritten to central.
    with session() as s:
        wi = s.execute(
            select(agent_work_items.c.owner_id, agent_work_items.c.node_id).where(
                agent_work_items.c.work_id == ids["work_id"]
            )
        ).one()
        gap = s.execute(
            select(data_gaps.c.owner_id, data_gaps.c.node_id).where(
                data_gaps.c.gap_id == ids["gap_id"]
            )
        ).one()
    assert wi.owner_id == owner and wi.node_id == CENTRAL_NODE
    assert gap.owner_id == owner and gap.node_id == CENTRAL_NODE


def test_import_nulls_source_account_id(clean, source_db, tmp_path):
    """Imported documents drop their source_account_id: connector accounts are not
    migrated, so the bundle's original account id has no FK target in central. The
    nulled identity keeps the second import an all-skip (idempotent)."""
    owner = uuid.uuid4()
    ids = _seed_source(source_db, owner)
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Central Owner"))
        s.commit()

    out = tmp_path / "bundle"
    # Source documents reference the seeded connector account (non-null FK).
    assert _export(source_db, owner, out) == 0
    assert _import(out) == 0

    # Central rows imported under the owner, with source_account_id nulled.
    with session() as s:
        account_ids = (
            s.execute(
                select(documents.c.source_account_id).where(
                    documents.c.scope == "personal", documents.c.user_id == owner
                )
            )
            .scalars()
            .all()
        )
    assert len(account_ids) == 2
    assert all(a is None for a in account_ids), "source_account_id nulled on import"
    # The original (non-null) account id never landed in central.
    assert _count(documents, documents.c.source_account_id == ids["account_id"]) == 0

    import json

    report = json.loads((out / "parity_report.json").read_text())
    assert report["parity_ok"] is True
    assert report["tables"]["documents"]["imported"] == 2
    assert all(c["match"] for c in report["document_spot_checks"])

    # Second import recognizes the nulled identity and skips both documents.
    assert _import(out) == 0
    second = json.loads((out / "parity_report.json").read_text())
    assert second["parity_ok"] is True
    assert second["tables"]["documents"]["imported"] == 0
    assert second["tables"]["documents"]["skipped"] == 2
    assert _count(documents, documents.c.scope == "personal", documents.c.user_id == owner) == 2


def test_parity_report_counts_match_bundle(clean, source_db, tmp_path):
    owner = uuid.uuid4()
    _seed_source(source_db, owner)
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Central Owner"))
        s.commit()

    out = tmp_path / "bundle"
    _export(source_db, owner, out)
    assert _import(out) == 0

    import json

    report = json.loads((out / "parity_report.json").read_text())
    assert report["parity_ok"] is True
    assert report["target_node_id"] == CENTRAL_NODE
    # imported + skipped == bundle for every table.
    for name, t in report["tables"].items():
        assert t["imported"] + t["skipped"] == t["bundle"], name
    assert report["tables"]["documents"]["bundle"] == 2
    assert report["tables"]["documents"]["imported"] == 2
    assert report["tables"]["documents"]["post_import_db_owner"] == 2
    # spot checks all matched (documents have identical content).
    assert all(c["match"] for c in report["document_spot_checks"])
    assert report["document_spot_checks"], "at least one document spot check ran"


def test_second_import_is_idempotent(clean, source_db, tmp_path):
    owner = uuid.uuid4()
    _seed_source(source_db, owner)
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Central Owner"))
        s.commit()

    out = tmp_path / "bundle"
    _export(source_db, owner, out)
    assert _import(out) == 0
    doc_count_after_first = _count(documents)
    task_count_after_first = _count(personal_board_tasks)

    import json

    assert _import(out) == 0
    second = json.loads((out / "parity_report.json").read_text())
    assert second["parity_ok"] is True
    # Second import: nothing new imported, everything skipped.
    for name, t in second["tables"].items():
        assert t["imported"] == 0, f"{name} re-imported on the second run"
        assert t["skipped"] == t["bundle"], name
    assert _count(documents) == doc_count_after_first
    assert _count(personal_board_tasks) == task_count_after_first


def test_import_with_compile_authors_owner_scoped_pages(clean, source_db, tmp_path):
    owner = uuid.uuid4()
    _seed_source(source_db, owner)
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Central Owner"))
        s.commit()

    out = tmp_path / "bundle"
    _export(source_db, owner, out)
    assert _import(out, compile_flag=True) == 0

    # Compile produced owner-scoped corpus chunks + personal wiki pages.
    with session() as s:
        chunk_rows = s.execute(
            select(corpus_chunks.c.scope).where(corpus_chunks.c.scope == "personal")
        ).all()
        page_rows = s.execute(
            select(wiki_pages.c.scope, wiki_pages.c.owner_id).where(
                wiki_pages.c.scope == "personal"
            )
        ).all()
    assert chunk_rows and all(r.scope == "personal" for r in chunk_rows)
    assert page_rows and all(r.scope == "personal" and r.owner_id == owner for r in page_rows)


def test_unmapped_workspace_child_is_dropped_fail_closed(clean, source_db, tmp_path):
    """A board-child row referencing a workspace absent from the bundle is dropped
    fail-closed: counted as skipped_unmapped, never inserted, and parity strict
    mode flags it (non-zero exit) because data was lost."""
    owner = uuid.uuid4()
    _seed_source(source_db, owner)
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Central Owner"))
        s.commit()

    out = tmp_path / "bundle"
    _export(source_db, owner, out)

    # Inject a task row whose workspace_id is not in the exported workspaces.
    import json

    orphan_task_id = uuid.uuid4()
    orphan_ws_id = uuid.uuid4()
    orphan_row = {
        "task_id": {"__uuid__": str(orphan_task_id)},
        "workspace_id": {"__uuid__": str(orphan_ws_id)},
        "user_id": {"__uuid__": str(owner)},
        "title": "고아 작업",
        "status": "open",
    }
    tasks_path = out / "personal_board_tasks.jsonl"
    with tasks_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(orphan_row, ensure_ascii=False) + "\n")
    # Bump the manifest count so the count check stays consistent with the file.
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["counts"]["personal_board_tasks"] += 1
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    # Parity strict: dropped row -> non-zero exit.
    assert _import(out) == 1

    report = json.loads((out / "parity_report.json").read_text())
    assert report["parity_ok"] is False
    tasks = report["tables"]["personal_board_tasks"]
    assert tasks["skipped_unmapped"] == 1
    assert tasks["ok"] is False
    # The original (mapped) task still imported; the orphan was never inserted.
    assert tasks["imported"] == 1
    assert _count(personal_board_tasks, personal_board_tasks.c.task_id == orphan_task_id) == 0


def test_import_rewrites_board_child_user_id_to_central_target(clean, source_db, tmp_path):
    """Board child rows carrying a direct user_id FK are rewritten to the central
    target user, not the personal-node owner.

    In prod the personal-node owner UUID never matches the central user UUID, so
    the source owner has no row in central `users`. Board child tables
    (preferences, tasks, fixed_events, notes, objective_comments) carry a direct
    user_id FK; without the rewrite the insert hits ForeignKeyViolation. The
    earlier tests reuse one UUID for source + central, which masked this — here
    the source owner and the central target user are deliberately different."""
    source_owner = uuid.uuid4()
    central_user = uuid.uuid4()
    assert source_owner != central_user
    ids = _seed_source(source_db, source_owner)
    # Only the central target user exists in central; the source owner does NOT
    # (mirrors prod, where the personal-node owner has no central `users` row).
    with session() as s:
        s.execute(insert(users).values(user_id=central_user, display_name="Central Owner"))
        s.commit()

    out = tmp_path / "bundle"
    assert _export(source_db, source_owner, out) == 0
    # Import under the differing central user id; would FK-fail without the rewrite.
    assert import_personal_bundle.main(["--bundle", str(out), "--user-id", str(central_user)]) == 0

    # The task + preference (direct user_id FK) carry the central user, not source.
    with session() as s:
        task_user = s.execute(
            select(personal_board_tasks.c.user_id).where(
                personal_board_tasks.c.task_id == ids["task_id"]
            )
        ).scalar_one()
        pref_users = (
            s.execute(
                select(personal_board_preferences.c.user_id).where(
                    personal_board_preferences.c.workspace_id.in_(
                        select(personal_board_workspaces.c.workspace_id).where(
                            personal_board_workspaces.c.user_id == central_user,
                            personal_board_workspaces.c.node_id == CENTRAL_NODE,
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    assert task_user == central_user, "task user_id rewritten to central target"
    assert pref_users == [central_user], "preference user_id rewritten to central target"
    # No board child row leaked the source owner UUID into central.
    assert _count(personal_board_tasks, personal_board_tasks.c.user_id == source_owner) == 0
    assert (
        _count(personal_board_preferences, personal_board_preferences.c.user_id == source_owner)
        == 0
    )


def test_export_scopes_agent_rows_to_owner_work_ids(clean, source_db, tmp_path):
    """A multi-user source DB must not export another owner's Agent Work children
    just because they share the same personal node_id."""
    owner = uuid.uuid4()
    other = uuid.uuid4()
    owner_ids = _seed_source(source_db, owner)
    other_ids = _seed_source(source_db, other)
    assert owner_ids["work_id"] != other_ids["work_id"]

    out = tmp_path / "bundle"
    assert _export(source_db, owner, out) == 0

    work_rows = _jsonl_rows(out / "agent_work_items.jsonl")
    decision_rows = _jsonl_rows(out / "agent_work_decisions.jsonl")
    observation_rows = _jsonl_rows(out / "agent_policy_observations.jsonl")
    gap_rows = _jsonl_rows(out / "data_gaps.jsonl")

    assert [row["work_id"]["__uuid__"] for row in work_rows] == [str(owner_ids["work_id"])]
    assert [row["work_id"]["__uuid__"] for row in decision_rows] == [str(owner_ids["work_id"])]
    assert [row["work_id"]["__uuid__"] for row in observation_rows] == [str(owner_ids["work_id"])]
    assert [row["gap_id"]["__uuid__"] for row in gap_rows] == [str(owner_ids["gap_id"])]


def test_import_stamps_null_owner_agent_rows_to_central_owner(clean, source_db, tmp_path):
    """Legacy personal nodes may have node-local Agent Work rows with NULL
    owner_id. P8.6 must stamp them to the target owner before they land in
    central; otherwise NULL owner rows can become shared company-visible."""
    source_owner = uuid.uuid4()
    central_user = uuid.uuid4()
    ids = _seed_source(source_db, source_owner)
    engine = create_engine(source_db, future=True)
    with engine.begin() as c:
        c.execute(
            agent_work_items.update()
            .where(agent_work_items.c.work_id == ids["work_id"])
            .values(owner_id=None, created_by=source_owner)
        )
        c.execute(
            agent_policy_observations.update()
            .where(agent_policy_observations.c.work_id == ids["work_id"])
            .values(owner_id=None)
        )
    engine.dispose()
    with session() as s:
        s.execute(insert(users).values(user_id=central_user, display_name="Central Owner"))
        s.commit()

    out = tmp_path / "bundle"
    assert _export(source_db, source_owner, out, allow_legacy_null_owner=True) == 0
    assert import_personal_bundle.main(["--bundle", str(out), "--user-id", str(central_user)]) == 0

    with session() as s:
        work = s.execute(
            select(agent_work_items.c.owner_id, agent_work_items.c.created_by).where(
                agent_work_items.c.work_id == ids["work_id"]
            )
        ).one()
        decision_reviewer = s.execute(
            select(agent_work_decisions.c.reviewer_id).where(
                agent_work_decisions.c.work_id == ids["work_id"]
            )
        ).scalar_one()
        observation = s.execute(
            select(
                agent_policy_observations.c.owner_id,
                agent_policy_observations.c.reviewer_id,
            ).where(agent_policy_observations.c.work_id == ids["work_id"])
        ).one()
    assert work.owner_id == central_user
    assert work.created_by == central_user
    assert decision_reviewer == central_user
    assert observation.owner_id == central_user
    assert observation.reviewer_id == central_user


def test_export_requires_explicit_ack_for_legacy_null_owner_agent_work(
    clean, source_db, tmp_path, capsys
):
    source_owner = uuid.uuid4()
    ids = _seed_source(source_db, source_owner)
    engine = create_engine(source_db, future=True)
    with engine.begin() as c:
        c.execute(
            agent_work_items.update()
            .where(agent_work_items.c.work_id == ids["work_id"])
            .values(owner_id=None)
        )
    engine.dispose()

    out = tmp_path / "bundle"
    assert _export(source_db, source_owner, out) == 2
    captured = capsys.readouterr()
    assert "refusing export" in captured.err
    assert "--allow-legacy-null-owner-agent-work" in captured.err
    assert not (out / "agent_work_items.jsonl").exists()

    allowed = tmp_path / "allowed"
    assert _export(source_db, source_owner, allowed, allow_legacy_null_owner=True) == 0
    manifest = json.loads((allowed / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["legacy_null_owner_agent_work_count"] == 1
    assert manifest["legacy_null_owner_agent_work_allowed"] is True


def test_import_namespaces_agent_work_source_ref_and_counts_owner_children(
    clean, source_db, tmp_path
):
    """Migrating into the company node must not collide with existing central
    Agent Work `(node_id, source_kind, source_ref_id)` rows, and parity child
    counts must be scoped to the migrated owner work ids."""
    source_owner = uuid.uuid4()
    central_user = uuid.uuid4()
    ids = _seed_source(source_db, source_owner)
    existing_work_id = uuid.uuid4()
    existing_decision_id = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=central_user, display_name="Central Owner"))
        s.execute(
            insert(agent_work_items).values(
                work_id=existing_work_id,
                node_id=CENTRAL_NODE,
                node_kind="company",
                owner_id=None,
                source_kind="assistant_command",
                source_ref_id=ids["source_ref_id"],
                action_family="request_more_data",
                title="existing company work",
                payload={},
                state="pending",
            )
        )
        s.execute(
            insert(agent_work_decisions).values(
                decision_id=existing_decision_id,
                work_id=existing_work_id,
                node_id=CENTRAL_NODE,
                reviewer_id=central_user,
                decision="dismiss",
                from_state="pending",
                to_state="dismissed",
            )
        )
        s.commit()

    out = tmp_path / "bundle"
    assert _export(source_db, source_owner, out) == 0
    assert import_personal_bundle.main(["--bundle", str(out), "--user-id", str(central_user)]) == 0

    migrated_ref = f"p8:{SOURCE_NODE}:{source_owner}:{ids['source_ref_id']}"
    with session() as s:
        migrated_ref_in_db = s.execute(
            select(agent_work_items.c.source_ref_id).where(
                agent_work_items.c.work_id == ids["work_id"],
                agent_work_items.c.owner_id == central_user,
            )
        ).scalar_one()
        migrated_policy_ref = s.execute(
            select(agent_policy_observations.c.source_ref_id).where(
                agent_policy_observations.c.work_id == ids["work_id"],
                agent_policy_observations.c.owner_id == central_user,
            )
        ).scalar_one()
        existing_still_present = s.execute(
            select(agent_work_items.c.work_id).where(agent_work_items.c.work_id == existing_work_id)
        ).scalar_one()
    assert migrated_ref_in_db == migrated_ref
    assert migrated_policy_ref == migrated_ref
    assert existing_still_present == existing_work_id

    import json

    report = json.loads((out / "parity_report.json").read_text(encoding="utf-8"))
    assert report["parity_ok"] is True
    assert report["tables"]["agent_work_items"]["post_import_db_owner"] == 1
    assert report["tables"]["agent_work_decisions"]["post_import_db_owner"] == 1


def test_document_identity_collision_fails_parity(clean, source_db, tmp_path):
    """If two bundle documents collapse onto one central identity after
    source_account_id is nulled, parity must fail instead of treating the second
    row as an idempotent skip."""
    owner = uuid.uuid4()
    _seed_source(source_db, owner)
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Central Owner"))
        s.commit()

    out = tmp_path / "bundle"
    assert _export(source_db, owner, out) == 0

    import json

    docs_path = out / "documents.jsonl"
    rows = _jsonl_rows(docs_path)
    duplicate = dict(rows[0])
    duplicate["doc_id"] = {"__uuid__": str(uuid.uuid4())}
    duplicate["title"] = "중복 identity 문서"
    with docs_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(duplicate, ensure_ascii=False) + "\n")
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["documents"] += 1
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    assert _import(out) == 1
    report = json.loads((out / "parity_report.json").read_text(encoding="utf-8"))
    assert report["parity_ok"] is False
    assert report["tables"]["documents"]["ok"] is False
    assert len(report["document_identity_collisions"]) == 1


def test_existing_central_document_identity_mismatch_fails_parity(clean, source_db, tmp_path):
    """A pre-existing central document with the same nulled identity but different
    content is not an idempotent match."""
    owner = uuid.uuid4()
    _seed_source(source_db, owner)
    with session() as s:
        s.execute(insert(users).values(user_id=owner, display_name="Central Owner"))
        s.execute(
            insert(documents).values(
                doc_id=uuid.uuid4(),
                user_id=owner,
                title="기존 다른 내용",
                block_json=[],
                markdown="기존 central 내용이 bundle과 다름",
                source="gws_gmail",
                source_account_id=None,
                source_external_id="msg-0",
                source_canonical_id=None,
                schema_version=SCHEMA_VERSION,
                scope="personal",
                project="company",
            )
        )
        s.commit()

    out = tmp_path / "bundle"
    assert _export(source_db, owner, out) == 0

    assert _import(out) == 1
    import json

    report = json.loads((out / "parity_report.json").read_text(encoding="utf-8"))
    assert report["parity_ok"] is False
    mismatches = [check for check in report["document_spot_checks"] if not check["match"]]
    assert mismatches
    assert mismatches[0]["source_external_id"] == "msg-0"
