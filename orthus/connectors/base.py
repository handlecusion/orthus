"""Source-agnostic connector interface (prompt §4.1).

All connectors produce `InternalDocument` via `iter_documents`; `run_import`
drives the common upsert + audit loop so connector implementations stay pure.
"""

from __future__ import annotations

import sys
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from orthus.audit import audit
from orthus.db import session
from orthus.documents import source_document_identity_clauses, upsert_source_document
from orthus.models.base import ChatModel
from orthus.models.orchestration import TASK_DISTILL, get_chat_model_for
from orthus.schemas.canonical import InternalDocument
from orthus.tables import documents
from orthus.connectors.state import upsert_connector_item
from orthus.kg.entities import persist_distilled_entities
from orthus.wiki.consolidate import consolidate
from orthus.wiki.distill import DistillResult, distill_document


class Connector(ABC):
    """Abstract base for all document source connectors."""

    @abstractmethod
    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        """Yield normalized documents, optionally filtered to those edited after `since`."""


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    doc_ids: list[UUID] = field(default_factory=list)


def run_import(
    connector: Connector,
    user_id: UUID,
    since: datetime | None = None,
    *,
    scope: str = "company",
    source_account_id: UUID | None = None,
    continue_on_error: bool = False,
) -> ImportReport:
    """Drive a full or incremental import, wrapped in a single audit span.

    Tallies:
      - created: new document rows inserted
      - updated: existing rows whose source_last_edited_at advanced
      - skipped: existing rows that were already up to date (changed=False)
      - errors: docs that raised (only when continue_on_error; logged + skipped)

    `continue_on_error=True` (bulk seed) keeps going past a per-doc failure
    (e.g. an LLM timeout during T1 authoring) instead of aborting the whole run.
    """
    report = ImportReport()

    with audit("connector.import") as span:
        for doc in connector.iter_documents(since):
            try:
                # Peek whether this (source, source_external_id) already exists so we can
                # distinguish "created" (new row) from "updated" (existing row touched).
                pre_existed = False
                if doc.source_external_id is not None:
                    with session() as s:
                        pre_existed = (
                            s.execute(
                                select(documents.c.doc_id).where(
                                    *source_document_identity_clauses(
                                        user_id, doc, scope, source_account_id
                                    )
                                )
                            ).first()
                            is not None
                        )

                doc_id, changed = upsert_source_document(
                    user_id, doc, scope=scope, source_account_id=source_account_id
                )
                _track_connector_item(source_account_id, doc, doc_id)
                report.doc_ids.append(doc_id)

                if not changed:
                    report.skipped += 1
                elif pre_existed:
                    report.updated += 1
                else:
                    report.created += 1
            except Exception as e:  # noqa: BLE001 — bulk seed resilience
                if not continue_on_error:
                    raise
                report.errors += 1
                ext = doc.source_external_id or "(no id)"
                print(
                    f"[import] skip '{doc.title}' ({ext}): {type(e).__name__}: {e}",
                    file=sys.stderr,
                )

        span.add_meta(
            created=report.created,
            updated=report.updated,
            skipped=report.skipped,
            errors=report.errors,
            total=len(report.doc_ids),
        )

    return report


@dataclass
class _ChangedDoc:
    """A doc that the serial stage upserted+indexed and that still needs wiki
    authoring (distill in stage 2, consolidate in stage 3)."""

    doc_id: UUID
    title: str
    scope: str
    owner_id: UUID | None
    project: str


def run_parallel_import(
    connector: Connector,
    user_id: UUID,
    since: datetime | None = None,
    *,
    scope: str = "company",
    owner_id: UUID | None = None,
    source_account_id: UUID | None = None,
    continue_on_error: bool = False,
    concurrency: int = 8,
    chat_model: ChatModel | None = None,
) -> ImportReport:
    """Bulk import with the slow per-doc DISTILL parallelized.

    Same created/updated/skipped/errors semantics as `run_import`, but split into
    three stages so the LLM distill (the ~bottleneck) runs concurrently while the
    order-sensitive page merges stay serial:

      Stage 1 (SERIAL, Notion rate-limited): iterate `iter_documents`, upsert the
        `documents` row + run corpus indexing (chunk+embed) via
        `upsert_source_document(defer_authoring=True)`. Collect changed docs.
      Stage 2 (PARALLEL, ThreadPoolExecutor): `distill_document` per changed doc —
        pure LLM extraction, reads the doc + calls the model, no shared DB writes
        → (WikiSource, claims). Each thread gets its own orthus.db.session() inside
        distill_document, and audit's correlation_id is a per-thread contextvar, so
        threads don't share session/audit state.
      Stage 3 (SERIAL, ordered): `consolidate` per doc — page merges + conflict
        detection read prior on-disk state and MUST stay deterministic, so this is
        never parallelized. Iterating distill results in stage-1 (input) order keeps
        the merge sequence reproducible.

    `continue_on_error=True`: a doc that fails to fetch/upsert (stage 1) or distill
    (stage 2) is counted in `errors` and skipped; the rest proceed."""
    chat = chat_model or get_chat_model_for(TASK_DISTILL)
    report = ImportReport()
    changed: list[_ChangedDoc] = []
    resolved_owner_id = user_id if scope == "personal" and owner_id is None else owner_id

    with audit("connector.import_parallel") as span:
        # Stage 1: serial upsert + index (no authoring yet).
        for doc in connector.iter_documents(since):
            try:
                pre_existed = False
                if doc.source_external_id is not None:
                    with session() as s:
                        pre_existed = (
                            s.execute(
                                select(documents.c.doc_id).where(
                                    *source_document_identity_clauses(
                                        user_id, doc, scope, source_account_id
                                    )
                                )
                            ).first()
                            is not None
                        )

                doc_id, was_changed = upsert_source_document(
                    user_id,
                    doc,
                    scope=scope,
                    source_account_id=source_account_id,
                    defer_authoring=True,
                )
                _track_connector_item(source_account_id, doc, doc_id)
                report.doc_ids.append(doc_id)

                if not was_changed:
                    report.skipped += 1
                    continue
                if pre_existed:
                    report.updated += 1
                else:
                    report.created += 1
                if doc.wiki_authoring:
                    changed.append(
                        _ChangedDoc(
                            doc_id,
                            doc.title,
                            scope=scope,
                            owner_id=resolved_owner_id,
                            project=doc.project,
                        )
                    )
            except Exception as e:  # noqa: BLE001 — bulk seed resilience
                if not continue_on_error:
                    raise
                report.errors += 1
                ext = doc.source_external_id or "(no id)"
                print(
                    f"[import] skip '{doc.title}' ({ext}): {type(e).__name__}: {e}",
                    file=sys.stderr,
                )

        # Stage 2: parallel distill (I/O-bound LLM; threads are fine).
        def _distill(cd: _ChangedDoc):
            return distill_document(cd.doc_id, user_id, chat_model=chat)

        distilled: list[tuple[_ChangedDoc, DistillResult]] = []
        if changed:
            with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
                futures = {pool.submit(_distill, cd): cd for cd in changed}
                results: dict[UUID, DistillResult] = {}
                for fut, cd in futures.items():
                    try:
                        results[cd.doc_id] = fut.result()
                    except Exception as e:  # noqa: BLE001 — bulk seed resilience
                        if not continue_on_error:
                            raise
                        report.errors += 1
                        print(
                            f"[import] distill failed '{cd.title}': {type(e).__name__}: {e}",
                            file=sys.stderr,
                        )
            # Re-order results into stage-1 input order so consolidate is deterministic.
            for cd in changed:
                if cd.doc_id in results:
                    distilled.append((cd, results[cd.doc_id]))

        # Stage 3: serial, ordered consolidate + entity persist (conflict detection
        # and cross-row entity checks read prior state — must not run in parallel).
        for cd, result in distilled:
            consolidate(
                result.source,
                result.claims,
                user_id=user_id,
                scope=cd.scope,
                owner_id=cd.owner_id,
                project=cd.project,
            )
            persist_distilled_entities(
                result.entities, source=result.source, user_id=user_id, scope=cd.scope
            )

        span.add_meta(
            created=report.created,
            updated=report.updated,
            skipped=report.skipped,
            errors=report.errors,
            total=len(report.doc_ids),
            concurrency=concurrency,
            scope=scope,
            source_account_id=str(source_account_id) if source_account_id else None,
        )

    return report


def _track_connector_item(
    source_account_id: UUID | None, doc: InternalDocument, doc_id: UUID
) -> None:
    """Mirror account-local item identity for cursor/dedupe diagnostics."""
    if source_account_id is None or doc.source_external_id is None:
        return
    content_hash = hashlib.sha256(doc.markdown.encode("utf-8")).hexdigest()
    external_version = doc.source_last_edited_at.isoformat() if doc.source_last_edited_at else None
    upsert_connector_item(
        source_account_id,
        doc.source_external_id,
        external_version=external_version,
        content_hash=content_hash,
        doc_id=doc_id,
    )
