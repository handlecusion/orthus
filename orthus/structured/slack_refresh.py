"""Refresh Slack-derived structured rows from raw Slack API replies."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.connectors.account_config import account_secret
from orthus.db import session
from orthus.settings import get_settings
from orthus.structured.rows import structured_row_uuid
from orthus.structured.slack_extract import extract_slack_rows_from_messages
from orthus.tables import connector_accounts, documents, structured_rows


def refresh_slack_raw_structured_rows(
    *,
    scope: str = "company",
    limit: int | None = None,
    contactish_only: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    token = _slack_token()
    where = [documents.c.source == "slack", documents.c.scope == scope]
    if contactish_only:
        where.append(
            or_(
                documents.c.markdown.ilike("%mailto:%"),
                documents.c.markdown.ilike("%tel:%"),
                documents.c.markdown.ilike("%이메일%"),
                documents.c.markdown.ilike("%전화%"),
                documents.c.markdown.ilike("%@%"),
            )
        )
    stmt = (
        select(
            documents.c.doc_id,
            documents.c.user_id,
            documents.c.source_external_id,
            documents.c.source_account_id,
            documents.c.project,
        )
        .where(*where)
        .order_by(documents.c.source_last_edited_at.asc().nulls_last(), documents.c.doc_id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    counts: dict[str, int] = {"docs": 0, "rows": 0, "errors": 0}
    with httpx.Client(
        base_url="https://slack.com/api",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    ) as client:
        with session() as s:
            for doc in s.execute(stmt).all():
                channel, thread_ts = _parse_source_external_id(doc.source_external_id)
                if not channel or not thread_ts:
                    counts["errors"] += 1
                    continue
                try:
                    messages = _fetch_replies(client, channel, thread_ts)
                except Exception:  # noqa: BLE001
                    counts["errors"] += 1
                    continue
                rows = extract_slack_rows_from_messages(channel, thread_ts, messages)
                counts["docs"] += 1
                counts["rows"] += len(rows)
                for row in rows:
                    counts[row["record_type"]] = counts.get(row["record_type"], 0) + 1
                if dry_run:
                    continue
                s.execute(
                    delete(structured_rows).where(structured_rows.c.source_doc_id == doc.doc_id)
                )
                for row in rows:
                    _upsert_row(s, doc, row, scope)
            if not dry_run:
                s.commit()
    return counts


def _slack_token() -> str:
    settings = get_settings()
    with session() as s:
        row = (
            s.execute(
                select(
                    connector_accounts.c.account_id,
                    connector_accounts.c.settings_redacted,
                )
                .where(
                    connector_accounts.c.connector_slug == "slack",
                    connector_accounts.c.node_id == settings.node_id,
                    connector_accounts.c.account_kind == settings.node_kind,
                    connector_accounts.c.status == "active",
                )
                .order_by(connector_accounts.c.updated_at.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
    token = account_secret(row, "token") if row is not None else None
    token = token or settings.conn_slack_bot_token
    if not token:
        raise RuntimeError("Slack bot token not configured")
    return token


def _parse_source_external_id(value: str | None) -> tuple[str, str]:
    parts = (value or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "slack":
        return "", ""
    return parts[1], parts[2]


def _fetch_replies(client: httpx.Client, channel: str, thread_ts: str) -> list[dict[str, str]]:
    res = client.get(
        "conversations.replies", params={"channel": channel, "ts": thread_ts, "limit": 200}
    )
    res.raise_for_status()
    data = res.json()
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise RuntimeError(
            f"slack_api_error:{data.get('error') if isinstance(data, dict) else 'invalid'}"
        )
    out: list[dict[str, str]] = []
    for item in data.get("messages") or []:
        if not isinstance(item, dict):
            continue
        if item.get("bot_id") or item.get("app_id"):
            continue
        out.append(
            {
                "user": str(item.get("user") or ""),
                "ts": str(item.get("ts") or ""),
                "text": str(item.get("text") or ""),
            }
        )
    return out


def _upsert_row(s, doc, row: dict, scope: str) -> None:
    record_type = str(row.get("record_type") or "").strip()
    record_key = str(row.get("record_key") or "").strip()
    if not record_type or not record_key:
        return
    source = str(row.get("source") or "slack")
    now = datetime.now(UTC)
    stmt = pg_insert(structured_rows).values(
        row_id=structured_row_uuid(source, record_type, doc.doc_id, record_key),
        source=source,
        record_type=record_type,
        source_doc_id=doc.doc_id,
        source_external_id=doc.source_external_id,
        source_account_id=doc.source_account_id,
        record_key=record_key,
        properties=row.get("properties") or {},
        evidence=row.get("evidence"),
        confidence=str(row.get("confidence") or "medium"),
        scope=scope,
        project=doc.project,
        owner_id=doc.user_id if scope == "personal" else None,
        user_id=doc.user_id,
    )
    s.execute(
        stmt.on_conflict_do_update(
            index_elements=[structured_rows.c.row_id],
            set_={
                "properties": stmt.excluded.properties,
                "evidence": stmt.excluded.evidence,
                "confidence": stmt.excluded.confidence,
                "updated_at": now,
            },
        )
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Refresh Slack structured rows from raw Slack API")
    parser.add_argument("--scope", default="company", choices=["company", "personal"])
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--all", action="store_true", help="refresh all Slack docs, not contact-ish subset"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    counts = refresh_slack_raw_structured_rows(
        scope=args.scope,
        limit=args.limit,
        contactish_only=not args.all,
        dry_run=args.dry_run,
    )
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"slack raw refresh {'dry-run ' if args.dry_run else ''}done: {summary}")


if __name__ == "__main__":
    main()
