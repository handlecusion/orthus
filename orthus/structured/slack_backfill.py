"""Backfill `structured_rows` from existing Slack documents."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.db import session
from orthus.structured.rows import structured_row_uuid
from orthus.structured.slack_extract import extract_slack_rows_from_markdown
from orthus.tables import documents, structured_rows


def backfill_slack_structured_rows(
    *,
    scope: str = "company",
    project: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    replace: bool = False,
) -> dict[str, int]:
    where = [documents.c.source == "slack", documents.c.scope == scope]
    if project is not None:
        where.append(documents.c.project == project)
    stmt = (
        select(
            documents.c.doc_id,
            documents.c.user_id,
            documents.c.source_external_id,
            documents.c.source_account_id,
            documents.c.project,
            documents.c.markdown,
        )
        .where(*where)
        .order_by(documents.c.source_last_edited_at.asc().nulls_last(), documents.c.doc_id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    counts: dict[str, int] = {"docs": 0, "rows": 0}
    with session() as s:
        if replace and not dry_run:
            delete_where = [
                structured_rows.c.source == "slack",
                structured_rows.c.scope == scope,
            ]
            if project is not None:
                delete_where.append(structured_rows.c.project == project)
            s.execute(delete(structured_rows).where(*delete_where))
        rows = s.execute(stmt).all()
        for doc in rows:
            counts["docs"] += 1
            extracted = extract_slack_rows_from_markdown(doc.markdown or "")
            if not extracted:
                continue
            for item in extracted:
                record_type = str(item.get("record_type") or "").strip()
                record_key = str(item.get("record_key") or item.get("key") or "").strip()
                if not record_type or not record_key:
                    continue
                counts["rows"] += 1
                counts[record_type] = counts.get(record_type, 0) + 1
                if dry_run:
                    continue
                _upsert_row(s, doc, item, record_type, record_key, scope)
        if not dry_run:
            s.commit()
    return counts


def _upsert_row(s, doc, item: dict, record_type: str, record_key: str, scope: str) -> None:
    source = str(item.get("source") or "slack")
    now = datetime.now(UTC)
    stmt = pg_insert(structured_rows).values(
        row_id=structured_row_uuid(source, record_type, doc.doc_id, record_key),
        source=source,
        record_type=record_type,
        source_doc_id=doc.doc_id,
        source_external_id=doc.source_external_id,
        source_account_id=doc.source_account_id,
        record_key=record_key,
        properties=item.get("properties") or {},
        evidence=item.get("evidence"),
        confidence=str(item.get("confidence") or "medium"),
        scope=scope,
        project=doc.project,
        owner_id=doc.user_id if scope == "personal" else None,
        user_id=doc.user_id,
    )
    s.execute(
        stmt.on_conflict_do_update(
            index_elements=[structured_rows.c.row_id],
            set_={
                "source_external_id": stmt.excluded.source_external_id,
                "source_account_id": stmt.excluded.source_account_id,
                "properties": stmt.excluded.properties,
                "evidence": stmt.excluded.evidence,
                "confidence": stmt.excluded.confidence,
                "scope": stmt.excluded.scope,
                "project": stmt.excluded.project,
                "owner_id": stmt.excluded.owner_id,
                "user_id": stmt.excluded.user_id,
                "updated_at": now,
            },
        )
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backfill structured_rows from Slack docs")
    parser.add_argument("--scope", default="company", choices=["company", "personal"])
    parser.add_argument("--project")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    counts = backfill_slack_structured_rows(
        scope=args.scope,
        project=args.project,
        limit=args.limit,
        dry_run=args.dry_run,
        replace=args.replace,
    )
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"slack structured backfill {'dry-run ' if args.dry_run else ''}done: {summary}")


if __name__ == "__main__":
    main()
