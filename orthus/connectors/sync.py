"""Manual connector sync CLI for node-local make targets."""

from __future__ import annotations

import argparse
import os
import uuid
from uuid import UUID

from sqlalchemy import insert, select

from orthus.connectors.default_accounts import ensure_default_connector_account
from orthus.connectors.registry import register_default_connector_providers
from orthus.connectors.runner import run_connector_account_sync
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import connector_accounts, users

_SEED_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def resolve_connector_user_id() -> UUID:
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
                display_name="Connector Sync",
                preferred_timezone="Asia/Seoul",
            )
        )
        s.commit()
        return _SEED_USER_ID


def _resolve_user_id() -> UUID:
    return resolve_connector_user_id()


def _ensure_account(connector_slug: str, user_id: UUID) -> UUID:
    settings = get_settings()
    with session() as s:
        row = s.execute(
            select(connector_accounts.c.account_id)
            .where(
                connector_accounts.c.connector_slug == connector_slug,
                connector_accounts.c.node_id == settings.node_id,
                connector_accounts.c.account_kind == settings.node_kind,
                connector_accounts.c.status == "active",
            )
            .order_by(connector_accounts.c.updated_at.desc())
            .limit(1)
        ).first()
    if row is not None:
        return row[0]
    return ensure_default_connector_account(connector_slug, user_id, settings)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run one connector sync")
    parser.add_argument("connector", help="Connector slug, e.g. local_files")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("ORTHUS_INGEST_CONCURRENCY", "8")),
    )
    args = parser.parse_args(argv)

    connector_slug = args.connector.strip().lower()
    register_default_connector_providers(replace=True)
    user_id = _resolve_user_id()
    account_id = _ensure_account(connector_slug, user_id)
    result = run_connector_account_sync(
        account_id,
        user_id,
        reason="manual",
        continue_on_error=True,
        concurrency=args.concurrency,
    )

    if result.status != "succeeded":
        raise SystemExit(result.error_message or f"{connector_slug} sync failed")

    report = result.report
    if report is None:
        raise SystemExit(f"{connector_slug} sync returned no report")
    print(
        f"{connector_slug} sync succeeded: "
        f"created={report.created}, updated={report.updated}, "
        f"skipped={report.skipped}, errors={report.errors}, total={len(report.doc_ids)}"
    )


if __name__ == "__main__":
    main()
