"""Owner-scoped deletion for central personal collector data.

This is a central-side counterpart to the local collector archive cleanup. It
only targets the signed-in owner's `scope='personal'` collector documents and
their derived corpus/wiki rows. It never deletes company data and never accepts
collector-token auth; routes using this module stay session-operator only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, delete, select

from orthus.audit import audit
from orthus.collector.ingest import ALLOWED_COLLECTOR_SOURCES
from orthus.db import session
from orthus.tables import (
    connector_items,
    corpus_chunks,
    documents,
    embeddings,
    structured_rows,
)
from orthus.wiki import store
from orthus.wiki.slugs import source_slug_from_title

_SCOPE = "personal"


@dataclass(frozen=True)
class PersonalDataDeleteResult:
    owner_id: UUID
    documents_deleted: int = 0
    connector_items_deleted: int = 0
    structured_rows_deleted: int = 0
    corpus_chunks_deleted: int = 0
    corpus_embeddings_deleted: int = 0
    wiki_items_deleted: int = 0


@dataclass(frozen=True)
class _DocRow:
    doc_id: UUID
    title: str
    source: str
    source_external_id: str | None


def delete_personal_collector_document(owner_id: UUID, doc_id: UUID) -> PersonalDataDeleteResult:
    """Delete one owner-scoped personal collector document by doc id."""
    rows = _select_docs(owner_id, doc_id=doc_id)
    return _delete_docs(owner_id, rows)


def prune_personal_collector_documents(
    owner_id: UUID,
    *,
    cutoff: datetime,
    source: str | None = None,
) -> PersonalDataDeleteResult:
    """Delete owner-scoped personal collector documents older than cutoff."""
    if source is not None and source not in ALLOWED_COLLECTOR_SOURCES:
        raise ValueError(f"unsupported source: {source}")
    rows = _select_docs(owner_id, cutoff=cutoff, source=source)
    return _delete_docs(owner_id, rows)


def _select_docs(
    owner_id: UUID,
    *,
    doc_id: UUID | None = None,
    cutoff: datetime | None = None,
    source: str | None = None,
) -> list[_DocRow]:
    conditions = [
        documents.c.scope == _SCOPE,
        documents.c.user_id == owner_id,
        documents.c.source.in_(ALLOWED_COLLECTOR_SOURCES),
    ]
    if doc_id is not None:
        conditions.append(documents.c.doc_id == doc_id)
    if cutoff is not None:
        conditions.append(documents.c.updated_at < cutoff)
    if source is not None:
        conditions.append(documents.c.source == source)
    with session() as s:
        rows = s.execute(
            select(
                documents.c.doc_id,
                documents.c.title,
                documents.c.source,
                documents.c.source_external_id,
            ).where(and_(*conditions))
        ).all()
    return [
        _DocRow(
            doc_id=row.doc_id,
            title=row.title,
            source=row.source,
            source_external_id=row.source_external_id,
        )
        for row in rows
    ]


def _delete_docs(owner_id: UUID, rows: list[_DocRow]) -> PersonalDataDeleteResult:
    if not rows:
        return PersonalDataDeleteResult(owner_id=owner_id)

    doc_ids = [row.doc_id for row in rows]
    wiki_items_deleted = sum(_delete_derived_wiki(owner_id, row) for row in rows)

    with audit("collector.personal_data_delete") as span:
        span.add_meta(user_id=str(owner_id), documents=len(doc_ids))
        with session() as s:
            embedding_ids = [
                row.embedding_id
                for row in s.execute(
                    select(corpus_chunks.c.embedding_id).where(corpus_chunks.c.doc_id.in_(doc_ids))
                ).all()
                if row.embedding_id is not None
            ]
            corpus_chunks_deleted = _rowcount(
                s.execute(delete(corpus_chunks).where(corpus_chunks.c.doc_id.in_(doc_ids))).rowcount
            )
            corpus_embeddings_deleted = 0
            if embedding_ids:
                corpus_embeddings_deleted += _rowcount(
                    s.execute(
                        delete(embeddings).where(embeddings.c.embedding_id.in_(embedding_ids))
                    ).rowcount
                )
            corpus_embeddings_deleted += _rowcount(
                s.execute(
                    delete(embeddings).where(
                        embeddings.c.kind == "corpus_chunk",
                        embeddings.c.scope == _SCOPE,
                        embeddings.c.user_id == owner_id,
                        embeddings.c.ref_id.in_(doc_ids),
                    )
                ).rowcount
            )
            connector_items_deleted = _rowcount(
                s.execute(
                    delete(connector_items).where(connector_items.c.doc_id.in_(doc_ids))
                ).rowcount
            )
            structured_rows_deleted = _rowcount(
                s.execute(
                    delete(structured_rows).where(structured_rows.c.source_doc_id.in_(doc_ids))
                ).rowcount
            )
            documents_deleted = _rowcount(
                s.execute(
                    delete(documents).where(
                        documents.c.doc_id.in_(doc_ids),
                        documents.c.scope == _SCOPE,
                        documents.c.user_id == owner_id,
                    )
                ).rowcount
            )
            s.commit()

        span.add_meta(
            documents_deleted=documents_deleted,
            corpus_chunks_deleted=corpus_chunks_deleted,
            corpus_embeddings_deleted=corpus_embeddings_deleted,
            wiki_items_deleted=wiki_items_deleted,
        )

    return PersonalDataDeleteResult(
        owner_id=owner_id,
        documents_deleted=documents_deleted,
        connector_items_deleted=connector_items_deleted,
        structured_rows_deleted=structured_rows_deleted,
        corpus_chunks_deleted=corpus_chunks_deleted,
        corpus_embeddings_deleted=corpus_embeddings_deleted,
        wiki_items_deleted=wiki_items_deleted,
    )


def _delete_derived_wiki(owner_id: UUID, row: _DocRow) -> int:
    source_slug = source_slug_from_title(row.title, row.doc_id)
    source = store.load_source(source_slug, scope=_SCOPE, owner_id=owner_id)
    claim_slugs: set[str] = set(source.key_claims if source is not None else [])
    page_slugs: set[str] = set(source.related_pages if source is not None else [])

    for claim_slug in list(claim_slugs):
        claim = store.load_claim(claim_slug, scope=_SCOPE, owner_id=owner_id)
        if claim is None:
            continue
        page_slugs.update(claim.related_pages)

    related = {source_slug} | claim_slugs | page_slugs
    task_slugs = [
        task_slug
        for task_slug in store.list_slugs("task", scope=_SCOPE, owner_id=owner_id)
        if _task_references_any(task_slug, related, owner_id)
    ]

    deleted = 0
    for kind, slugs in (
        ("task", task_slugs),
        ("page", sorted(page_slugs)),
        ("claim", sorted(claim_slugs)),
        ("source", [source_slug]),
    ):
        for slug in slugs:
            if store.delete_item(kind, slug, scope=_SCOPE, owner_id=owner_id):
                deleted += 1
    return deleted


def _task_references_any(task_slug: str, slugs: set[str], owner_id: UUID) -> bool:
    task = store.load_task(task_slug, scope=_SCOPE, owner_id=owner_id)
    return bool(task and set(task.related).intersection(slugs))


def _rowcount(value: int | None) -> int:
    return max(int(value or 0), 0)
