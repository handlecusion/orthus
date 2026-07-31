"""Author: orchestration entrypoint for the self-authoring pipeline.

`author_from_document` is the T1 trigger (corpus ingest -> distill -> consolidate).
`rebuild_all` re-authors every document and backs `make wiki-rebuild`. All store
writes happen inside distill/consolidate; this layer only orchestrates + audits."""

from __future__ import annotations

import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import hashlib
import re
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from orthus.audit import audit
from orthus.db import session
from orthus.models.base import ChatModel
from orthus.schemas.canonical import WikiClaim, WikiSource, WikiSourceRef
from orthus.tables import documents
from orthus.wiki.consolidate import consolidate
from orthus.wiki.distill import DistillResult, _headline_fallback, distill_document
from orthus.wiki import store

# persist_distilled_entities는 함수-로컬로 import한다 — orthus.kg.entities가
# orthus.wiki(store)를 import하고 orthus.wiki.__init__이 이 author 모듈을 import해서,
# top-level import면 import 사이클이 닫힌다(kg projection이 kg.entities를 먼저
# import하는 경로에서 ImportError). 호출 시점엔 모든 모듈이 로드돼 안전하다.

_WS = re.compile(r"\s+")
_FATAL_DISTILL_PATTERNS = (
    "usage limit",
    "session limit",
    "limit reached",
    "quota",
    "too many requests",
    "rate limit",
    "429",
)


def _norm_question(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def _qa_hash(question: str) -> str:
    return hashlib.sha256(_norm_question(question).encode("utf-8")).hexdigest()[:8]


def qa_claim_slug(question: str) -> str:
    """`author_from_qa`가 같은 question에 대해 만드는 claim slug (결정론).

    WTR.2 resolve가 호출 결과(counts)에서 직접 얻을 수 없는 produced claim
    slug를 기록하기 위해 공유한다 — slug 공식은 `author_from_qa` 본문과 동일.
    """
    return f"qa-{_qa_hash(question)}-claim"


def author_from_document(
    doc_id: UUID,
    user_id: UUID,
    *,
    chat_model: ChatModel | None = None,
    root: Path | None = None,
    scope: str = "company",
    owner_id: UUID | None = None,
    project: str = "atlas",
) -> dict[str, int]:
    """Distill one document into a source + claims, then consolidate into pages
    and conflict tasks. Returns {sources, claims, pages, tasks} counts.

    `scope`/`owner_id` tag the authored wiki pages for tenant isolation (P2.1);
    `project` tags them with the company→project bucket (P2)."""
    with audit("wiki.author") as span:
        with session() as s:
            row = s.execute(
                select(documents.c.source, documents.c.title, documents.c.markdown).where(
                    documents.c.doc_id == doc_id
                )
            ).first()
        if row is not None:
            allowed, reason = _should_author_document(row.source, row.title, row.markdown)
            if not allowed:
                span.add_meta(doc_id=str(doc_id), skipped=True, skip_reason=reason)
                return {"sources": 0, "claims": 0, "pages": 0, "tasks": 0}
        from orthus.kg.entities import persist_distilled_entities

        result = distill_document(doc_id, user_id, chat_model=chat_model)
        pages, tasks = consolidate(
            result.source,
            result.claims,
            user_id=user_id,
            root=root,
            scope=scope,
            owner_id=owner_id,
            project=project,
        )
        persist_distilled_entities(
            result.entities,
            source=result.source,
            user_id=user_id,
            root=root,
            scope=scope,
        )
        counts = {
            "sources": 1,
            "claims": len(result.claims),
            "pages": len(pages),
            "tasks": len(tasks),
        }
        span.add_meta(doc_id=str(doc_id), **counts)
    return counts


@dataclass(frozen=True)
class _RebuildDoc:
    doc_id: UUID
    user_id: UUID
    scope: str
    owner_id: UUID | None
    project: str


def _is_fatal_distill_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(pattern in text for pattern in _FATAL_DISTILL_PATTERNS)


def _should_author_document(source: str, title: str, markdown: str) -> tuple[bool, str | None]:
    if source == "slack":
        from orthus.connectors.slack import should_author_slack_markdown

        return should_author_slack_markdown(title, markdown)
    if source.startswith("mail_"):
        from orthus.mail.authoring import should_author_mail_markdown

        return should_author_mail_markdown(title, markdown)
    return True, None


def _authored_source_refs(*, root: Path | None, scope: str, owner_id: UUID | None) -> set[str]:
    refs: set[str] = set()
    for slug in store.list_slugs("source", root=root, scope=scope, owner_id=owner_id):
        source = store.load_source(slug, root=root, scope=scope, owner_id=owner_id)
        if source is not None and source.source_ref:
            refs.add(source.source_ref)
    return refs


def author_from_qa(
    user_id: UUID,
    question: str,
    answer: str,
    source_refs: list[WikiSourceRef],
    *,
    chat_model: ChatModel | None = None,
    root: Path | None = None,
    scope: str = "personal",
    owner_id: UUID | None = None,
) -> dict[str, int]:
    """T2 (compounding): persist a grounded Q&A answer as a low-confidence claim.

    Fully deterministic (no LLM call — `chat_model` accepted for signature parity
    with the T1 trigger). The qa_id/qa_slug/claim_slug derive from a normalized
    question hash, so re-asking the SAME question overwrites in place (same slug +
    same text => consolidate emits no conflict task). A DIFFERENT answer to the same
    question keeps the same claim_slug but changes the claim text — that is the
    legitimate conflict path, handled by consolidate (task, no silent overwrite).

    P2.1: Q&A learning is PERSONAL by default (scope='personal', owner=user_id) —
    one user's grounded answers should not silently mutate the shared company wiki.
    `owner_id` defaults to `user_id` when scope is personal.

    Self-amplification guard: `related_pages` is taken ONLY from concept-page hits
    (`kind == "page"`), never claim-kind hits. consolidate folds each related slug
    into a concept page (seeding one if missing), so feeding claim slugs back would
    spawn spurious pages and let prior qa claims compound their own provenance on
    each re-ask. Restricting to pages keeps the qa claim's grounding stable and
    prevents the qa claim (itself kind=claim) from ever becoming a related page."""
    qa_id = f"qa-{_qa_hash(question)}"
    qa_slug = qa_id
    claim_slug = qa_claim_slug(question)
    related: list[str] = []
    for ref in source_refs:
        if ref.kind == "page" and ref.page_slug not in related:
            related.append(ref.page_slug)
    today = date.today()
    if owner_id is None and scope == "personal":
        owner_id = user_id

    with audit("wiki.author_qa") as span:
        source = WikiSource(
            slug=qa_slug,
            title=question[:80],
            source_type="qa_session",
            source_ref=qa_id,
            ingested_at=datetime.now(UTC),
            summary=answer,
            key_concepts=[],
            key_claims=[claim_slug],
            terminology=[],
            related_pages=related,
            open_questions=[],
        )
        claim = WikiClaim(
            slug=claim_slug,
            claim=f"Q: {question.strip()} A: {answer.strip()}",
            evidence=answer,
            supporting=[qa_slug, *related],
            conflicting=[],
            confidence="low",
            last_reviewed=today,
            related_pages=related,
            notes=None,
            # 결정론 폴백만(LLM 미호출 — docstring "Fully deterministic" 유지).
            # 헤드라인은 question 한 줄만 쓴다: claim 본문은 "Q: … A: …" 형식이라
            # 그대로 폴백에 넣으면 `_QA_PREFIX`가 선두 `Q:`만 걷어내고 inline `A:`가
            # 라벨에 남는다(plan §5.2 "Q/A 제거"). question은 이미 `title`로도 쓰는
            # 사람이 읽는 요약이라 그래프 노드 라벨로 적합하고 slug 노출을 막는다.
            display_title=_headline_fallback(question),
        )
        pages, tasks = consolidate(
            source, [claim], user_id=user_id, root=root, scope=scope, owner_id=owner_id
        )
        counts = {
            "sources": 1,
            "claims": 1,
            "pages": len(pages),
            "tasks": len(tasks),
        }
        span.add_meta(qa_slug=qa_slug, claim_slug=claim_slug, **counts)
    return counts


def rebuild_all(
    user_id: UUID | None = None,
    *,
    chat_model: ChatModel | None = None,
    root: Path | None = None,
    source_prefix: str | None = None,
) -> dict[str, int]:
    """Re-author every document (optionally filtered by user). Returns aggregate
    {documents, sources, claims, pages, tasks} counts.

    Each document is re-authored under its own stored `scope`; personal docs are
    owned by their `user_id` (company docs have no owner)."""
    stmt = select(
        documents.c.doc_id,
        documents.c.user_id,
        documents.c.scope,
        documents.c.project,
        documents.c.source,
        documents.c.title,
        documents.c.markdown,
    )
    if user_id is not None:
        stmt = stmt.where(documents.c.user_id == user_id)
    if source_prefix is not None:
        stmt = stmt.where(documents.c.source.like(f"{source_prefix}%"))
    with session() as s:
        rows = s.execute(stmt).all()

    totals = {"documents": 0, "sources": 0, "claims": 0, "pages": 0, "tasks": 0, "skipped": 0}
    with audit("wiki.rebuild") as span:
        for doc_id, owner, scope, project, source, title, markdown in rows:
            allowed, _reason = _should_author_document(source, title, markdown)
            if not allowed:
                totals["skipped"] += 1
                continue
            owner_id = owner if scope == "personal" else None
            counts = author_from_document(
                doc_id,
                owner,
                chat_model=chat_model,
                root=root,
                scope=scope,
                owner_id=owner_id,
                project=project,
            )
            totals["documents"] += 1
            for k in ("sources", "claims", "pages", "tasks"):
                totals[k] += counts[k]
        span.add_meta(n_docs=totals["documents"], **totals)
    return totals


def rebuild_all_parallel(
    user_id: UUID | None = None,
    *,
    chat_model: ChatModel | None = None,
    root: Path | None = None,
    source_prefix: str | None = None,
    concurrency: int = 4,
    continue_on_error: bool = True,
    skip_authored: bool = False,
) -> dict[str, int]:
    """Re-author every document with parallel LLM distill and ordered writes.

    Distill is I/O-bound and safe to run concurrently. Consolidate writes and
    page merges stay serial and input-ordered to avoid slug/page conflicts.
    """
    stmt = select(
        documents.c.doc_id,
        documents.c.user_id,
        documents.c.scope,
        documents.c.project,
        documents.c.source,
        documents.c.title,
        documents.c.markdown,
    )
    if user_id is not None:
        stmt = stmt.where(documents.c.user_id == user_id)
    if source_prefix is not None:
        stmt = stmt.where(documents.c.source.like(f"{source_prefix}%"))
    with session() as s:
        rows = s.execute(stmt).all()

    skipped_by_gate = 0
    docs: list[_RebuildDoc] = []
    for doc_id, owner, scope, project, source, title, markdown in rows:
        allowed, _reason = _should_author_document(source, title, markdown)
        if not allowed:
            skipped_by_gate += 1
            continue
        docs.append(
            _RebuildDoc(
                doc_id=doc_id,
                user_id=owner,
                scope=scope,
                owner_id=owner if scope == "personal" else None,
                project=project,
            )
        )
    if skip_authored:
        authored_refs_by_scope: dict[tuple[str, UUID | None], set[str]] = {}
        authored: set[tuple[str, UUID | None, str]] = set()
        for doc in docs:
            key = (doc.scope, doc.owner_id, str(doc.doc_id))
            if key in authored:
                continue
            scope_key = (doc.scope, doc.owner_id)
            refs = authored_refs_by_scope.get(scope_key)
            if refs is None:
                refs = _authored_source_refs(root=root, scope=doc.scope, owner_id=doc.owner_id)
                authored_refs_by_scope[scope_key] = refs
            if str(doc.doc_id) in refs:
                authored.add(key)
        before = len(docs)
        docs = [doc for doc in docs if (doc.scope, doc.owner_id, str(doc.doc_id)) not in authored]
        print(
            f"[rebuild] skip authored documents={before - len(docs)} remaining={len(docs)}",
            file=sys.stderr,
        )
    totals = {
        "documents": 0,
        "sources": 0,
        "claims": 0,
        "pages": 0,
        "tasks": 0,
        "errors": 0,
        "skipped": skipped_by_gate,
    }
    chat = chat_model or None

    with audit("wiki.rebuild_parallel") as span:

        def _distill(doc: _RebuildDoc):
            return distill_document(doc.doc_id, doc.user_id, chat_model=chat)

        workers = max(1, concurrency)
        pending_results: dict[int, DistillResult | None] = {}
        next_to_consolidate = 0

        def _consolidate_one(doc: _RebuildDoc, result: DistillResult) -> None:
            from orthus.kg.entities import persist_distilled_entities

            pages, tasks = consolidate(
                result.source,
                result.claims,
                user_id=doc.user_id,
                root=root,
                scope=doc.scope,
                owner_id=doc.owner_id,
                project=doc.project,
            )
            persist_distilled_entities(
                result.entities,
                source=result.source,
                user_id=doc.user_id,
                root=root,
                scope=doc.scope,
            )
            totals["documents"] += 1
            totals["sources"] += 1
            totals["claims"] += len(result.claims)
            totals["pages"] += len(pages)
            totals["tasks"] += len(tasks)
            if totals["documents"] % 25 == 0:
                print(f"[rebuild] consolidate written={totals['documents']}", file=sys.stderr)

        def _consolidate_ready() -> None:
            nonlocal next_to_consolidate
            while next_to_consolidate in pending_results:
                doc = docs[next_to_consolidate]
                result = pending_results.pop(next_to_consolidate)
                next_to_consolidate += 1
                if result is None:
                    continue
                _consolidate_one(doc, result)

        if docs:
            print(
                f"[rebuild] distill start documents={len(docs)} concurrency={workers}",
                file=sys.stderr,
            )
            completed = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                next_to_submit = 0
                futures: dict[Future, tuple[int, _RebuildDoc]] = {}

                def _submit_more() -> None:
                    nonlocal next_to_submit
                    while next_to_submit < len(docs) and len(futures) < workers:
                        doc = docs[next_to_submit]
                        futures[pool.submit(_distill, doc)] = (next_to_submit, doc)
                        next_to_submit += 1

                _submit_more()
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for fut in done:
                        index, doc = futures.pop(fut)
                        try:
                            pending_results[index] = fut.result()
                        except Exception as exc:  # noqa: BLE001 - clean rebuild should salvage good docs.
                            if _is_fatal_distill_error(exc):
                                for pending in futures:
                                    pending.cancel()
                                raise RuntimeError(
                                    f"wiki_distill_fatal:{doc.doc_id}: {exc}"
                                ) from exc
                            if not continue_on_error:
                                raise
                            totals["errors"] += 1
                            pending_results[index] = None
                            print(
                                f"[rebuild] distill failed {doc.doc_id}: "
                                f"{type(exc).__name__}: {exc}",
                                file=sys.stderr,
                            )
                        completed += 1
                        _consolidate_ready()
                        if completed % 10 == 0 or completed == len(docs):
                            print(
                                f"[rebuild] distill progress {completed}/{len(docs)} "
                                f"errors={totals['errors']} written={totals['documents']}",
                                file=sys.stderr,
                            )
                    _submit_more()

        span.add_meta(n_docs=len(docs), concurrency=workers, **totals)
    return totals
