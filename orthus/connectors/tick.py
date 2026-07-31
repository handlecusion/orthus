"""Periodic connector sync CLI for node-local schedulers."""

from __future__ import annotations

import argparse
import os
import re
import sys

from orthus.connectors.default_accounts import ensure_default_connector_account
from orthus.connectors.registry import register_default_connector_providers
from orthus.connectors.runner import ConnectorRunResult, run_due_connector_syncs
from orthus.connectors.sync import resolve_connector_user_id
from orthus.settings import get_settings


def connector_slugs(values: list[str], env_value: str | None = None) -> list[str]:
    """Normalize positional slugs plus ORTHUS_CONNECTOR_TICK_CONNECTORS."""
    raw = [*values]
    if env_value:
        raw.extend(re.split(r"[\s,]+", env_value))

    seen: set[str] = set()
    slugs: list[str] = []
    for value in raw:
        slug = value.strip().lower()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run one due connector sync tick")
    parser.add_argument(
        "connectors",
        nargs="*",
        help=("Optional connector slugs to ensure before the due tick, e.g. gws_gmail gws_drive"),
    )
    parser.add_argument(
        "--no-ensure",
        action="store_true",
        help="Only run already configured due accounts.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("ORTHUS_INGEST_CONCURRENCY", "8")),
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    register_default_connector_providers(replace=True)
    user_id = resolve_connector_user_id()

    slugs = connector_slugs(
        list(args.connectors),
        os.environ.get("ORTHUS_CONNECTOR_TICK_CONNECTORS"),
    )
    ensure_errors: list[str] = []
    if not args.no_ensure:
        for slug in slugs:
            try:
                ensure_default_connector_account(slug, user_id, settings)
            except ValueError as exc:
                ensure_errors.append(f"{slug}: {exc}")

    results = run_due_connector_syncs(
        user_id,
        node_id=settings.node_id,
        continue_on_error=True,
        concurrency=args.concurrency,
    )

    for message in ensure_errors:
        print(f"ensure skipped: {message}", file=sys.stderr)
    if not results:
        print("connector tick: due=0")
    for result in results:
        print(_format_result(result))

    if ensure_errors or any(result.status != "succeeded" for result in results):
        raise SystemExit(1)


def _format_result(result: ConnectorRunResult) -> str:
    if result.report is None:
        return (
            f"{result.connector_slug} periodic {result.status}: "
            f"{result.error_message or 'no report'}"
        )
    report = result.report
    return (
        f"{result.connector_slug} periodic {result.status}: "
        f"created={report.created}, updated={report.updated}, "
        f"skipped={report.skipped}, errors={report.errors}, total={len(report.doc_ids)}"
    )


if __name__ == "__main__":
    main()
