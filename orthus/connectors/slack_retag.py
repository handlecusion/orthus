"""Retag existing Slack documents into company-internal project buckets."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select, update

from orthus.connectors.slack import (
    parse_slack_channel_projects,
    resolve_slack_project_from_text,
)
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import corpus_chunks, documents, embeddings, structured_rows


@dataclass(frozen=True)
class SlackRetagSummary:
    scanned: int
    changed: int
    documents: int
    corpus_chunks: int
    embeddings: int
    structured_rows: int
    by_project: dict[str, int]


def retag_slack_projects(*, scope: str = "company", dry_run: bool = False) -> SlackRetagSummary:
    settings = get_settings()
    channel_defaults = parse_slack_channel_projects(settings.conn_slack_channel_projects)
    with session() as s:
        rows = s.execute(
            select(
                documents.c.doc_id,
                documents.c.source_external_id,
                documents.c.title,
                documents.c.markdown,
                documents.c.project,
            ).where(documents.c.source == "slack", documents.c.scope == scope)
        ).all()

        by_project: Counter[str] = Counter()
        counts = Counter()
        scanned = 0
        changed = 0
        for row in rows:
            scanned += 1
            channel = _channel_from_external_id(row.source_external_id)
            resolved = resolve_slack_project_from_text(f"{row.title}\n{row.markdown}")
            project = resolved or channel_defaults.get(channel or "", "company")
            by_project[project] += 1
            if project == row.project:
                continue
            changed += 1
            if dry_run:
                continue

            # updated_at bump — KG 증분 sync watermark가 updated_at 기반(kg-model §3).
            doc_stmt = (
                update(documents)
                .where(documents.c.doc_id == row.doc_id)
                .values(project=project, updated_at=func.now())
            )
            counts["documents"] += s.execute(doc_stmt).rowcount or 0
            chunk_stmt = (
                update(corpus_chunks)
                .where(corpus_chunks.c.doc_id == row.doc_id)
                .values(project=project)
            )
            counts["corpus_chunks"] += s.execute(chunk_stmt).rowcount or 0
            emb_stmt = (
                update(embeddings)
                .where(embeddings.c.kind == "corpus_chunk", embeddings.c.ref_id == row.doc_id)
                .values(project=project)
            )
            counts["embeddings"] += s.execute(emb_stmt).rowcount or 0
            structured_stmt = (
                update(structured_rows)
                .where(structured_rows.c.source_doc_id == row.doc_id)
                .values(project=project, updated_at=func.now())
            )
            counts["structured_rows"] += s.execute(structured_stmt).rowcount or 0
        if not dry_run:
            s.commit()

    return SlackRetagSummary(
        scanned=scanned,
        changed=changed,
        documents=counts["documents"],
        corpus_chunks=counts["corpus_chunks"],
        embeddings=counts["embeddings"],
        structured_rows=counts["structured_rows"],
        by_project=dict(sorted(by_project.items())),
    )


def _channel_from_external_id(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split(":")
    if len(parts) >= 3 and parts[0] == "slack":
        return parts[1]
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Retag existing Slack docs by project.")
    parser.add_argument("--scope", default="company")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    summary = retag_slack_projects(scope=args.scope, dry_run=args.dry_run)
    print(
        "slack retag "
        f"{'dry-run ' if args.dry_run else ''}done: "
        f"scanned={summary.scanned}, changed={summary.changed}, "
        f"documents={summary.documents}, corpus_chunks={summary.corpus_chunks}, "
        f"embeddings={summary.embeddings}, structured_rows={summary.structured_rows}, "
        f"by_project={summary.by_project}"
    )


if __name__ == "__main__":
    main()
