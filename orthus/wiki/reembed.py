"""Rebuild only the wiki retrieval embeddings from compiled wiki markdown.

This does not distill documents or rewrite the wiki store. It reads existing
claim/page markdown, rechunks the body, embeds with the configured
ORTHUS_EMBEDDING provider, then swaps wiki_chunks/embeddings rows per page batch.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import delete, insert, select

from orthus.audit import audit
from orthus.corpus.pipeline import chunk_markdown
from orthus.db import session
from orthus.models.registry import get_embedding_model
from orthus.schemas.canonical import SCHEMA_VERSION
from orthus.settings import get_settings
from orthus.tables import documents, embeddings, users, wiki_chunks, wiki_pages
from orthus.wiki.store import _body_of

_EMBEDDED_KINDS = ("claim", "page")


@dataclass(frozen=True)
class _PageRow:
    page_id: UUID
    slug: str
    kind: str
    path: str
    scope: str
    project: str


@dataclass(frozen=True)
class _ChunkJob:
    page_id: UUID
    user_id: UUID
    ordinal: int
    content: str
    scope: str
    project: str


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-embed compiled wiki claim/page chunks.")
    parser.add_argument("--scope", default="company", help="scope to re-embed")
    parser.add_argument("--project", default=None, help="optional project filter")
    parser.add_argument("--kind", action="append", choices=_EMBEDDED_KINDS, help="claim/page")
    parser.add_argument("--limit", type=int, default=None, help="limit pages for smoke runs")
    parser.add_argument("--batch-size", type=int, default=64, help="texts per embedding API call")
    parser.add_argument("--page-batch-size", type=int, default=128, help="pages per DB swap batch")
    parser.add_argument(
        "--skip-current", action="store_true", help="skip pages already on current model"
    )
    parser.add_argument("--dry-run", action="store_true", help="count chunks without writing DB")
    args = parser.parse_args()

    totals = reembed_wiki(
        scope=args.scope,
        project=args.project,
        kinds=tuple(args.kind or _EMBEDDED_KINDS),
        limit=args.limit,
        batch_size=args.batch_size,
        page_batch_size=args.page_batch_size,
        skip_current=args.skip_current,
        dry_run=args.dry_run,
    )
    print("wiki reembed complete: " + ", ".join(f"{k}={v}" for k, v in totals.items()))


def reembed_wiki(
    *,
    scope: str = "company",
    project: str | None = None,
    kinds: tuple[str, ...] = _EMBEDDED_KINDS,
    limit: int | None = None,
    batch_size: int = 64,
    page_batch_size: int = 128,
    skip_current: bool = False,
    dry_run: bool = False,
) -> dict[str, int | str]:
    if batch_size <= 0:
        raise ValueError("--batch-size must be positive")
    if page_batch_size <= 0:
        raise ValueError("--page-batch-size must be positive")

    settings = get_settings()
    root = settings.wiki_store_path
    embedder = get_embedding_model()
    pages = _load_pages(scope=scope, project=project, kinds=kinds, limit=limit)

    totals = {
        "pages_seen": 0,
        "pages_reembedded": 0,
        "pages_skipped": 0,
        "chunks": 0,
        "missing_files": 0,
        "model_version": embedder.model_version,
    }
    with audit("wiki.reembed") as span:
        span.add_meta(
            scope=scope,
            project=project,
            kinds=list(kinds),
            model_version=embedder.model_version,
            dry_run=dry_run,
        )
        for page_batch in _batches(pages, page_batch_size):
            current_models = _page_model_versions([p.page_id for p in page_batch])
            jobs, missing, skipped = _chunk_jobs(
                root,
                page_batch,
                current_models=current_models,
                current_model=embedder.model_version,
                skip_current=skip_current,
            )
            totals["pages_seen"] += len(page_batch)
            totals["missing_files"] += missing
            totals["pages_skipped"] += skipped
            if not jobs:
                continue
            page_ids = {job.page_id for job in jobs}
            totals["chunks"] += len(jobs)
            if dry_run:
                totals["pages_reembedded"] += len(page_ids)
                continue
            vectors = _embed_jobs(embedder, jobs, batch_size=batch_size)
            _swap_chunk_rows(jobs, vectors, model_version=embedder.model_version)
            totals["pages_reembedded"] += len(page_ids)
            print(
                "wiki reembed progress: "
                f"pages={totals['pages_reembedded']}/{len(pages)} "
                f"chunks={totals['chunks']}"
            )
        span.add_meta(**totals)
    return totals


def _load_pages(
    *,
    scope: str,
    project: str | None,
    kinds: tuple[str, ...],
    limit: int | None,
) -> list[_PageRow]:
    where = [wiki_pages.c.scope == scope, wiki_pages.c.kind.in_(kinds)]
    if project is not None:
        where.append(wiki_pages.c.project == project)
    stmt = (
        select(
            wiki_pages.c.page_id,
            wiki_pages.c.slug,
            wiki_pages.c.kind,
            wiki_pages.c.path,
            wiki_pages.c.scope,
            wiki_pages.c.project,
        )
        .where(*where)
        .order_by(wiki_pages.c.kind.asc(), wiki_pages.c.slug.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    with session() as s:
        rows = s.execute(stmt).all()
    return [
        _PageRow(
            page_id=r.page_id,
            slug=r.slug,
            kind=r.kind,
            path=r.path,
            scope=r.scope,
            project=r.project,
        )
        for r in rows
    ]


def _page_model_versions(page_ids: list[UUID]) -> dict[UUID, set[str]]:
    if not page_ids:
        return {}
    with session() as s:
        rows = s.execute(
            select(embeddings.c.ref_id, embeddings.c.model_version).where(
                embeddings.c.kind == "wiki_chunk",
                embeddings.c.ref_id.in_(page_ids),
            )
        ).all()
    out: dict[UUID, set[str]] = {}
    for page_id, model_version in rows:
        out.setdefault(page_id, set()).add(model_version)
    return out


def _chunk_jobs(
    root: Path,
    pages: list[_PageRow],
    *,
    current_models: dict[UUID, set[str]],
    current_model: str,
    skip_current: bool,
) -> tuple[list[_ChunkJob], int, int]:
    user_ids = _page_user_ids([p.page_id for p in pages])
    jobs: list[_ChunkJob] = []
    missing = 0
    skipped = 0
    for page in pages:
        if skip_current and current_models.get(page.page_id) == {current_model}:
            skipped += 1
            continue
        path = root / page.path
        if not path.is_file():
            missing += 1
            continue
        body = _body_of(path.read_text(encoding="utf-8"))
        pieces = chunk_markdown(body)
        if not pieces:
            skipped += 1
            continue
        user_id = user_ids.get(page.page_id) or _fallback_user_id(page.scope)
        for ordinal, content in enumerate(pieces):
            jobs.append(
                _ChunkJob(
                    page_id=page.page_id,
                    user_id=user_id,
                    ordinal=ordinal,
                    content=content,
                    scope=page.scope,
                    project=page.project,
                )
            )
    return jobs, missing, skipped


def _page_user_ids(page_ids: list[UUID]) -> dict[UUID, UUID]:
    if not page_ids:
        return {}
    with session() as s:
        rows = s.execute(
            select(embeddings.c.ref_id, embeddings.c.user_id).where(
                embeddings.c.kind == "wiki_chunk",
                embeddings.c.ref_id.in_(page_ids),
            )
        ).all()
    out: dict[UUID, UUID] = {}
    for page_id, user_id in rows:
        out.setdefault(page_id, user_id)
    return out


def _fallback_user_id(scope: str) -> UUID:
    with session() as s:
        row = s.execute(
            select(documents.c.user_id).where(documents.c.scope == scope).limit(1)
        ).first()
        if row is not None:
            return row.user_id
        row = s.execute(select(users.c.user_id).limit(1)).first()
        if row is not None:
            return row.user_id
    raise RuntimeError("wiki_reembed_no_user_id")


def _embed_jobs(embedder, jobs: list[_ChunkJob], *, batch_size: int) -> list[list[float]]:
    vectors: list[list[float]] = []
    for offset in range(0, len(jobs), batch_size):
        batch = jobs[offset : offset + batch_size]
        batch_vectors = embedder.embed([job.content for job in batch])
        if len(batch_vectors) != len(batch):
            raise RuntimeError("wiki_reembed_embedding_count_mismatch")
        vectors.extend(batch_vectors)
    return vectors


def _swap_chunk_rows(
    jobs: list[_ChunkJob],
    vectors: list[list[float]],
    *,
    model_version: str,
) -> None:
    page_ids = {job.page_id for job in jobs}
    with session() as s:
        old_emb_ids = [
            r[0]
            for r in s.execute(
                select(wiki_chunks.c.embedding_id).where(wiki_chunks.c.page_id.in_(page_ids))
            ).all()
            if r[0] is not None
        ]
        s.execute(delete(wiki_chunks).where(wiki_chunks.c.page_id.in_(page_ids)))
        if old_emb_ids:
            s.execute(delete(embeddings).where(embeddings.c.embedding_id.in_(old_emb_ids)))
        for job, vec in zip(jobs, vectors, strict=True):
            emb_id = uuid4()
            s.execute(
                insert(embeddings).values(
                    embedding_id=emb_id,
                    user_id=job.user_id,
                    kind="wiki_chunk",
                    ref_id=job.page_id,
                    vec=vec,
                    meta={"ordinal": job.ordinal},
                    schema_version=SCHEMA_VERSION,
                    model_version=model_version,
                    scope=job.scope,
                    project=job.project,
                )
            )
            s.execute(
                insert(wiki_chunks).values(
                    chunk_id=uuid4(),
                    page_id=job.page_id,
                    ordinal=job.ordinal,
                    content=job.content,
                    embedding_id=emb_id,
                )
            )
        s.commit()


def _batches(items: list[_PageRow], size: int) -> list[list[_PageRow]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


if __name__ == "__main__":
    main()
