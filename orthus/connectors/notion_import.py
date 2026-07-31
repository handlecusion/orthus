"""CLI entrypoint for `make import-notion` — full/incremental Notion seed import.

Reads the integration token from settings, resolves a target user, drives
`run_import`, and prints the resulting ImportReport. No live import runs unless
this module is executed explicitly.

User resolution (first match wins):
  1. ORTHUS_IMPORT_USER env var (a user_id UUID).
  2. The first existing row in `users` (lowest created_at).
  3. A seed user is created (matching seeds.dev.seed's DEMO_USER_ID).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from uuid import UUID

from sqlalchemy import func, insert, select

from orthus.connectors import (
    NotionConnector,
    adopt_legacy_notion_documents,
    ensure_notion_account,
    run_parallel_import,
)
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import notion_rows, project_overrides, users

# Matches seeds.dev.seed.DEMO_USER_ID so a fresh DB lands on the same seed user.
_SEED_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def _resolve_user_id() -> UUID:
    """Resolve the import target user_id (see module docstring)."""
    env_user = os.environ.get("ORTHUS_IMPORT_USER")
    if env_user:
        return UUID(env_user)

    with session() as s:
        row = s.execute(select(users.c.user_id).order_by(users.c.created_at).limit(1)).first()
        if row is not None:
            return row[0]

        s.execute(
            insert(users).values(
                user_id=_SEED_USER_ID,
                display_name="Notion Import",
                preferred_timezone="Asia/Seoul",
            )
        )
        s.commit()
        return _SEED_USER_ID


def _load_project_overrides() -> dict[str, str]:
    """Load the user-managed db_name -> project overrides (FE-editable).

    Read once at the start of an import and baked into the resolver so an override
    wins over id-set membership and persists across reseeds (P2)."""
    with session() as s:
        rows = s.execute(select(project_overrides.c.db_name, project_overrides.c.project)).all()
    return {db_name: project for db_name, project in rows}


def _build_project_resolver(
    atlas_ids: set[str],
    nova_ids: set[str],
    overrides: dict[str, str] | None = None,
) -> Callable[[str, str | None], str]:
    """(page_id, db_name) -> project.

    Resolution order: a user OVERRIDE keyed by db_name wins first (FE-editable,
    survives reseeds); otherwise id-set membership decides. The id-set partition
    is disjoint by construction (nova ∩ atlas = ∅); nova wins the membership
    check first, then atlas, else the page is company-only. `db_name` is None for
    standalone pages, which can never match an override (overrides are per-DB)."""
    overrides = overrides or {}

    def resolve(page_id: str, db_name: str | None) -> str:
        if db_name is not None and db_name in overrides:
            return overrides[db_name]
        if page_id in nova_ids:
            return "nova"
        if page_id in atlas_ids:
            return "atlas"
        return "company"

    return resolve


def _print_project_counts() -> None:
    """Per-project row counts from notion_rows (post-import summary)."""
    with session() as s:
        rows = s.execute(
            select(notion_rows.c.project, func.count())
            .group_by(notion_rows.c.project)
            .order_by(notion_rows.c.project)
        ).all()
    summary = ", ".join(f"{proj}={n}" for proj, n in rows) or "(no rows)"
    print(f"notion_rows by project: {summary}")


def main() -> None:
    settings = get_settings()
    tok = settings.conn_notion_token  # broad / company token (sees everything)
    if not tok:
        raise SystemExit("ORTHUS_CONN_NOTION_TOKEN not set")
    tok1 = settings.conn_notion_token_1  # atlas subtree
    tok2 = settings.conn_notion_token_2  # nova subtree

    user_id = _resolve_user_id()
    notion_account_id = ensure_notion_account(user_id, settings)
    adopted = adopt_legacy_notion_documents(notion_account_id)
    if adopted:
        print(f"Adopted {adopted} legacy Notion document(s) into connector account")

    # User overrides (db_name -> project) win over the ingest default and persist
    # across reseeds; loaded once and baked into the resolver.
    overrides = _load_project_overrides()
    if overrides:
        print(f"Loaded {len(overrides)} project override(s): {overrides}")

    resolver: Callable[[str, str | None], str] | None = None
    if tok1 and tok2:
        # Multitoken mode: build per-project id-sets from the scoped tokens, then
        # ingest ONCE with the broad token, stamping project by id-set membership
        # (with overrides taking precedence by db_name).
        atlas_ids = NotionConnector(tok1).all_object_ids()
        nova_ids = NotionConnector(tok2).all_object_ids()
        resolver = _build_project_resolver(atlas_ids, nova_ids, overrides)
        print(
            f"Mode: multitoken (id-set membership). "
            f"atlas_ids={len(atlas_ids)}, nova_ids={len(nova_ids)}"
        )
    elif overrides:
        # Single-token, but overrides exist: bake them into a resolver with empty
        # id-sets so override-by-db_name still wins. NOTE: once a resolver is set it
        # owns the decision, so non-overridden DB rows resolve to 'company' (not the
        # db_name heuristic) and standalone pages to 'company'. Single-token
        # deployments that rely on the db_name heuristic should not set overrides.
        resolver = _build_project_resolver(set(), set(), overrides)
        print("Mode: single-token + overrides (override-by-db_name only)")
    else:
        print("Mode: single-token (db_name heuristic / project defaults)")

    # Distill (the slow per-doc LLM call) is parallelized; corpus indexing stays
    # serial and consolidate (page merges) stays serial+ordered. Concurrency from
    # ORTHUS_INGEST_CONCURRENCY (default 8).
    concurrency = int(os.environ.get("ORTHUS_INGEST_CONCURRENCY", "8"))
    print(
        f"Importing Notion workspace into user_id={user_id} (distill concurrency={concurrency}) ..."
    )

    report = run_parallel_import(
        NotionConnector(tok, project_resolver=resolver),
        user_id,
        scope=settings.node_kind,
        source_account_id=notion_account_id,
        continue_on_error=True,
        concurrency=concurrency,
    )
    print(
        f"ImportReport(created={report.created}, updated={report.updated}, "
        f"skipped={report.skipped}, errors={report.errors}, total={len(report.doc_ids)})"
    )
    _print_project_counts()


if __name__ == "__main__":
    main()
