"""Owner-scoped document ingestion for the personal collector daemon.

This writes (or idempotently updates) `documents` rows under `scope='personal'`
and the token owner's `user_id`, honoring the canonical
`(source, source_account_id, source_external_id)` idempotency contract (and
`source_canonical_id` when the collector supplies one). It does NOT run corpus
chunking/embedding or wiki authoring — P8.4 wires the central compile step. The
P8 personal ingest path may skip redaction (spec §5-B); the owner-only row
boundary is the enforcement, and company-scope promotion still redacts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import insert, select, update

from orthus.audit import audit
from orthus.collector.auth import CollectorToken
from orthus.db import session
from orthus.schemas.canonical import (
    SCHEMA_VERSION,
    CollectorIngestDocument,
    CollectorIngestDocumentResult,
    CollectorIngestResponse,
)
from orthus.tables import documents


class CollectorIngestError(ValueError):
    """Raised when an ingest document is not acceptable (e.g. bad project)."""


INGEST_SCOPE = "personal"
# documents.project is the company project taxonomy (CHECK constraint:
# atlas|nova|orbit|company). Personal ownership is carried by scope+user_id,
# not the project dimension, so default to 'company' and reject anything outside
# the allowed set rather than writing an invalid value.
DEFAULT_PROJECT = "company"
ALLOWED_PROJECTS = {"atlas", "nova", "orbit", "company"}
ALLOWED_COLLECTOR_SOURCES = frozenset(
    {
        "local_files",
        "codex_sessions",
        "claude_sessions",
        "chat_exports",
        "email_exports",
        "gws_gmail",
        "gws_drive",
        "github",
    }
)


def ingest_documents(
    token: CollectorToken, docs: list[CollectorIngestDocument]
) -> CollectorIngestResponse:
    """Upsert each owner-scoped document; return per-doc created/updated/unchanged."""

    results: list[CollectorIngestDocumentResult] = []
    with audit("collector.ingest") as span:
        span.add_meta(user_id=str(token.user_id), node_id=token.node_id, batch=len(docs))
        created = updated = unchanged = 0
        for doc in docs:
            status = _upsert_personal_document(token.user_id, doc, results)
            if status == "created":
                created += 1
            elif status == "updated":
                updated += 1
            else:
                unchanged += 1
        span.add_meta(created=created, updated=updated, unchanged=unchanged)
    return CollectorIngestResponse(results=results)


def _upsert_personal_document(
    user_id: UUID,
    doc: CollectorIngestDocument,
    results: list[CollectorIngestDocumentResult],
) -> str:
    source = _collector_source(doc.source)
    project = (doc.project or "").strip() or DEFAULT_PROJECT
    if project not in ALLOWED_PROJECTS:
        raise CollectorIngestError(f"unsupported project: {project}")
    now = datetime.now(UTC)
    with session() as s:
        existing = s.execute(
            select(
                documents.c.doc_id,
                documents.c.user_id,
                documents.c.title,
                documents.c.markdown,
                documents.c.project,
                documents.c.source_last_edited_at,
            ).where(*_identity_clauses(doc, source))
        ).first()

        if existing is None:
            doc_id = uuid.uuid4()
            s.execute(
                insert(documents).values(
                    doc_id=doc_id,
                    user_id=user_id,
                    title=doc.title,
                    block_json=[],
                    markdown=doc.content_md,
                    source=source,
                    source_account_id=doc.source_account_id,
                    source_external_id=doc.source_external_id,
                    source_canonical_id=doc.source_canonical_id,
                    source_db_name=None,
                    source_last_edited_at=doc.source_last_edited_at,
                    schema_version=SCHEMA_VERSION,
                    scope=INGEST_SCOPE,
                    project=project,
                    created_at=now,
                    updated_at=now,
                )
            )
            s.commit()
            results.append(
                CollectorIngestDocumentResult(
                    doc_id=doc_id, source_external_id=doc.source_external_id, status="created"
                )
            )
            return "created"

        doc_id = existing.doc_id
        if (
            existing.user_id == user_id
            and existing.title == doc.title
            and existing.markdown == doc.content_md
            and existing.project == project
            and existing.source_last_edited_at == doc.source_last_edited_at
        ):
            results.append(
                CollectorIngestDocumentResult(
                    doc_id=doc_id, source_external_id=doc.source_external_id, status="unchanged"
                )
            )
            return "unchanged"

        s.execute(
            update(documents)
            .where(documents.c.doc_id == doc_id)
            .values(
                user_id=user_id,
                scope=INGEST_SCOPE,
                title=doc.title,
                block_json=[],
                markdown=doc.content_md,
                source_canonical_id=doc.source_canonical_id,
                source_last_edited_at=doc.source_last_edited_at,
                project=project,
                updated_at=now,
            )
        )
        s.commit()
        results.append(
            CollectorIngestDocumentResult(
                doc_id=doc_id, source_external_id=doc.source_external_id, status="updated"
            )
        )
        return "updated"


def _collector_source(source: str) -> str:
    value = source.strip()
    if value not in ALLOWED_COLLECTOR_SOURCES:
        raise CollectorIngestError(f"unsupported collector source: {value or '(empty)'}")
    return value


def _identity_clauses(doc: CollectorIngestDocument, source: str) -> list:
    """Document identity matching the DB unique constraint.

    Uses `(source, source_canonical_id)` when the collector supplies a
    canonical id, otherwise falls back to the constraint columns
    `(source, source_account_id, source_external_id)`. Does NOT filter by
    `user_id` or `scope` — those are not part of the constraint, and
    filtering by them would miss an existing row when ownership changes,
    causing a bare INSERT to hit the UniqueViolation.
    """
    if doc.source_canonical_id:
        return [
            documents.c.source == source,
            documents.c.source_canonical_id == doc.source_canonical_id,
        ]
    return [
        documents.c.source == source,
        documents.c.source_account_id.is_(None)
        if doc.source_account_id is None
        else documents.c.source_account_id == doc.source_account_id,
        documents.c.source_external_id == doc.source_external_id,
    ]
