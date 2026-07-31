"""P8.4 central compile for owner-scoped personal documents.

The collector daemon pushes `documents` rows under `scope='personal'` +
`user_id=owner` via `POST /collector/ingest/documents`; that path deliberately
stops at the row write (no corpus chunks, no wiki authoring). This module runs
the missing compile steps on the central runtime for one owner:

  1. corpus index (chunk -> embed -> pgvector) for personal docs that have no
     corpus chunks yet, via the single `index_document` path, and
  2. wiki authoring (distill -> consolidate) for personal docs not yet authored,
     reusing the `skip_authored` source-ref scan to stay idempotent.

Both stages stamp `scope='personal'` + `owner_id=user_id`, so the central
owner-only row boundary (§3) and merged read plane apply automatically. The
distill stage runs in parallel (I/O-bound LLM) and consolidate stays serial +
input-ordered, mirroring `run_parallel_import` / `rebuild_all_parallel`. The
embed/session boundary (QueuePool drain protection) is delegated to
`index_document` / the wiki store, which own that pattern internally
(see `orthus/corpus/pipeline.py`); this module does not manage it locally.

A failed distill for one document is isolated: it is counted in `failed`, the
rest of the batch continues, and consolidate skips the failed document.

Idempotent: a second run finds chunks + authored sources already present and
skips both stages (indexed=0, authored=0).
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select

from orthus.audit import audit
from orthus.corpus import index_document
from orthus.db import session
from orthus.models.base import ChatModel
from orthus.kg.entities import persist_distilled_entities
from orthus.tables import corpus_chunks, documents
from orthus.wiki.author import _authored_source_refs
from orthus.wiki.consolidate import consolidate
from orthus.wiki.distill import DistillResult, distill_document

_COMPILE_SCOPE = "personal"
_DISTILL_CONCURRENCY = 4


@dataclass(frozen=True)
class CompileResult:
    indexed: int
    authored: int
    skipped: int
    failed: int = 0


@dataclass(frozen=True)
class _PersonalDoc:
    doc_id: UUID
    source: str
    title: str
    markdown: str
    project: str


def compile_personal_documents(
    user_id: UUID,
    *,
    limit: int | None = None,
    chat_model: ChatModel | None = None,
    concurrency: int = _DISTILL_CONCURRENCY,
) -> CompileResult:
    """Compile this owner's personal documents into corpus + wiki.

    Finds `documents` rows with `scope='personal'` and `user_id=user_id`, then
    (a) indexes any that lack corpus chunks and (b) authors any not yet present
    in the personal wiki store for this owner. Returns {indexed, authored,
    skipped} counts where `skipped` is documents that needed neither stage.
    """
    with audit("collector.compile") as span:
        span.add_meta(user_id=str(user_id))
        docs = _list_personal_documents(user_id, limit=limit)
        chunked_doc_ids = _doc_ids_with_chunks(user_id)
        authored_refs = _authored_source_refs(root=None, scope=_COMPILE_SCOPE, owner_id=user_id)

        to_index = [doc for doc in docs if doc.doc_id not in chunked_doc_ids]
        to_author = [
            doc
            for doc in docs
            if str(doc.doc_id) not in authored_refs and _should_author_personal_doc(doc)
        ]

        for doc in to_index:
            index_document(
                doc.doc_id,
                user_id,
                doc.markdown,
                scope=_COMPILE_SCOPE,
                project=doc.project,
            )

        authored, failed = _author_personal_documents(
            user_id, to_author, chat_model=chat_model, concurrency=concurrency
        )

        # A document was touched this run if it was (re)indexed or its distill
        # was attempted (whether it authored or failed). Failed docs stay
        # un-authored so a later run retries them; they are not "skipped".
        touched_ids = {doc.doc_id for doc in to_index} | {doc.doc_id for doc in to_author}
        skipped = sum(1 for doc in docs if doc.doc_id not in touched_ids)

        result = CompileResult(
            indexed=len(to_index), authored=authored, skipped=skipped, failed=failed
        )
        span.add_meta(
            documents=len(docs),
            indexed=result.indexed,
            authored=result.authored,
            skipped=result.skipped,
            failed=result.failed,
        )
    return result


def personal_compile_has_pending_work(user_id: UUID, *, limit: int | None = None) -> bool:
    """Cheaply detect whether compile would index or author owner documents."""

    docs = _list_personal_documents(user_id, limit=limit)
    if not docs:
        return False
    chunked_doc_ids = _doc_ids_with_chunks(user_id)
    authored_refs = _authored_source_refs(root=None, scope=_COMPILE_SCOPE, owner_id=user_id)
    return any(
        doc.doc_id not in chunked_doc_ids
        or (str(doc.doc_id) not in authored_refs and _should_author_personal_doc(doc))
        for doc in docs
    )


def _list_personal_documents(user_id: UUID, *, limit: int | None) -> list[_PersonalDoc]:
    stmt = (
        select(
            documents.c.doc_id,
            documents.c.source,
            documents.c.title,
            documents.c.markdown,
            documents.c.project,
        )
        .where(documents.c.scope == _COMPILE_SCOPE, documents.c.user_id == user_id)
        .order_by(documents.c.created_at, documents.c.doc_id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    with session() as s:
        rows = s.execute(stmt).all()
    return [
        _PersonalDoc(
            doc_id=r.doc_id,
            source=r.source,
            title=r.title,
            markdown=r.markdown,
            project=r.project,
        )
        for r in rows
    ]


def _should_author_personal_doc(doc: _PersonalDoc) -> bool:
    """Skip wiki authoring for metadata-only Drive blobs with no extractable text."""
    if doc.source == "gws_drive":
        markdown = doc.markdown or ""
        if "Content: skipped:" in markdown and "(content unavailable)" in markdown:
            return False
    return True


def _doc_ids_with_chunks(user_id: UUID) -> set[UUID]:
    """doc_ids that already have at least one personal-scope corpus chunk for this
    owner. Joining through the chunk's owning embedding keeps the owner boundary
    on the chunk-presence check."""
    stmt = (
        select(corpus_chunks.c.doc_id)
        .join(documents, documents.c.doc_id == corpus_chunks.c.doc_id)
        .where(
            corpus_chunks.c.scope == _COMPILE_SCOPE,
            documents.c.user_id == user_id,
        )
        .group_by(corpus_chunks.c.doc_id)
        .having(func.count() > 0)
    )
    with session() as s:
        return {r.doc_id for r in s.execute(stmt).all()}


def _author_personal_documents(
    user_id: UUID,
    docs: list[_PersonalDoc],
    *,
    chat_model: ChatModel | None,
    concurrency: int,
) -> tuple[int, int]:
    """Parallel distill + serial, input-ordered consolidate for the given docs.

    Distill is I/O-bound LLM extraction and reads the doc inside its own session,
    so it is safe to run concurrently; consolidate page merges read prior on-disk
    state and must stay serial + deterministic, so they run in submission order.
    Mirrors `rebuild_all_parallel`.

    A distill failure for one document is isolated: its index is recorded as
    failed, the consolidate cursor advances past it (no page write), and the rest
    of the batch proceeds. Returns `(authored, failed)`."""
    if not docs:
        return 0, 0

    workers = max(1, concurrency)
    authored = 0
    failed = 0
    pending_results: dict[int, DistillResult] = {}
    failed_indices: set[int] = set()
    next_to_consolidate = 0

    def _consolidate_ready() -> None:
        nonlocal next_to_consolidate, authored
        while next_to_consolidate in pending_results or next_to_consolidate in failed_indices:
            if next_to_consolidate in failed_indices:
                # Failed distill: skip consolidate, advance the cursor so later
                # in-order docs are not blocked.
                next_to_consolidate += 1
                continue
            doc = docs[next_to_consolidate]
            result = pending_results.pop(next_to_consolidate)
            next_to_consolidate += 1
            consolidate(
                result.source,
                result.claims,
                user_id=user_id,
                scope=_COMPILE_SCOPE,
                owner_id=user_id,
                project=doc.project,
            )
            # _COMPILE_SCOPE='personal' → persist_distilled_entities는 no-op
            # (company-only 경계, owner-scope entity는 K7). 호출은 경계 회귀 고정용.
            persist_distilled_entities(
                result.entities, source=result.source, user_id=user_id, scope=_COMPILE_SCOPE
            )
            authored += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        next_to_submit = 0
        futures: dict[Future, int] = {}

        def _submit_more() -> None:
            nonlocal next_to_submit
            while next_to_submit < len(docs) and len(futures) < workers:
                doc = docs[next_to_submit]
                futures[
                    pool.submit(distill_document, doc.doc_id, user_id, chat_model=chat_model)
                ] = next_to_submit
                next_to_submit += 1

        _submit_more()
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for fut in done:
                index = futures.pop(fut)
                try:
                    pending_results[index] = fut.result()
                except Exception as exc:  # one doc's distill must not kill the batch
                    failed += 1
                    failed_indices.add(index)
                    with audit("collector.compile.distill_failed") as span:
                        span.add_meta(
                            user_id=str(user_id),
                            doc_id=str(docs[index].doc_id),
                            error=type(exc).__name__,
                        )
                _consolidate_ready()
            _submit_more()

    return authored, failed
