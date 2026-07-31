#!/usr/bin/env python
"""Operator cleanup: delete legacy scope=company mail docs + derived wiki/embeddings.

Before the individual-mailbox boundary (docs/p6-mail-individual-scope.md), an
employee's company-domain mail was ingested as scope=company (whole-company
readable). This helper finds those legacy `mail_nova`/`mail_acme`
scope=company documents and deletes them along with:

- their corpus chunks (corpus_chunks.doc_id) — deleted FIRST because the row
  also references embeddings(embedding_id) with no ON DELETE CASCADE, so a
  documents/embeddings delete first hits a corpus_chunks FK violation,
- their corpus embeddings (kind=corpus_chunk, ref_id=doc_id),
- the document rows themselves,
- the wiki SOURCE pages authored 1:1 from each doc (kind=source, slug
  src-mail-*, title `Mail:%`), via wiki_store.delete_item, and
- downstream non-source wiki pages (claim/page/task) whose provenance is
  ENTIRELY these mail source slugs (purely mail-derived → safe to delete with
  no shared-knowledge loss), via wiki_store.delete_item. Mixed-provenance pages
  (cite both a mail source and a non-mail source) are NOT deleted; they are
  logged with a "needs wiki-rebuild to reconcile" message so the next
  `make wiki-rebuild` can drop the stale mail citation without losing the page.

After cleanup, re-ingest re-creates the docs as scope=personal bound to the owner.

DRY-RUN BY DEFAULT: prints what it would delete. Pass --apply to actually delete.
This script is operator-gated (DB snapshot first) and is never auto-run.

Idempotent: re-running after a partial deletion is safe — already-deleted wiki
slugs return False from delete_item, and missing doc_ids delete 0 rows.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import delete, select

from orthus.audit import audit
from orthus.db import session
from orthus.tables import corpus_chunks, documents, embeddings, wiki_links, wiki_pages
from orthus.wiki import store as wiki_store

MAIL_COMPANY_SOURCES = ("mail_nova", "mail_acme")
# Mail source pages are authored 1:1 per doc with slug src-mail-* / title "Mail:%"
# (orthus.mail.ingest._mail_title -> "Mail: <subject>" -> source_slug_from_title).
_MAIL_SOURCE_SLUG_PREFIX = "src-mail%"
_MAIL_SOURCE_TITLE_PREFIX = "Mail:%"
# wiki_pages.kind ("source"/"concept"/"claim"/"task") -> wiki_store key.
# Non-source/non-claim/non-task pages are stored under the "page" key.
_STORE_KIND = {"source": "source", "claim": "claim", "task": "task"}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    sources = tuple(args.sources) if args.sources else MAIL_COMPANY_SOURCES

    docs = _company_mail_docs(sources)
    if not docs:
        print(f"no scope=company mail documents found for sources={sources}")
        return 0

    doc_ids = [doc_id for doc_id, _title in docs]
    source_slugs = _mail_source_slugs()
    mail_only, mixed = _downstream_page_slugs(source_slugs)

    print(f"sources={sources} mode={'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"  documents (scope=company):     {len(doc_ids)}")
    print(f"  wiki source pages (src-mail*): {len(source_slugs)}")
    print(f"  downstream pages (mail-only):  {len(mail_only)}")
    print(f"  downstream pages (mixed):      {len(mixed)} (needs wiki-rebuild, NOT deleted)")
    for doc_id, title in docs:
        print(f"    doc {doc_id} {title[:80]!r}")
    if mixed:
        print("  mixed-provenance pages left for wiki-rebuild to reconcile:")
        for kind, slug in sorted(mixed):
            print(f"    [{kind}] {slug}")

    if not args.apply:
        print("dry-run only; pass --apply to delete")
        return 0

    # Source pages are themselves the (kind, slug) targets to delete; merge them
    # with the mail-only downstream pages and delete all via wiki_store.delete_item.
    source_page_kinds = {("source", slug) for slug in source_slugs}
    deleted_pages = _delete_wiki_pages(source_page_kinds | mail_only)
    deleted_chunks, deleted_embeddings, deleted_docs = _delete_docs_and_embeddings(doc_ids)
    print(
        f"deleted: documents={deleted_docs} corpus_chunks={deleted_chunks} "
        f"corpus_embeddings={deleted_embeddings} wiki_pages={deleted_pages} "
        f"(mixed-provenance pages kept={len(mixed)}; run `make wiki-rebuild`)"
    )
    return 0


def _company_mail_docs(sources: tuple[str, ...]) -> list[tuple]:
    with session() as s:
        rows = s.execute(
            select(documents.c.doc_id, documents.c.title).where(
                documents.c.source.in_(sources),
                documents.c.scope == "company",
            )
        ).all()
    return [(row.doc_id, row.title) for row in rows]


def _mail_source_slugs() -> set[str]:
    """Slugs of the kind=source wiki pages authored from the mail docs.

    Read the stored slugs directly rather than recomputing
    source_slug_from_title(title, doc_id): the stored kebab(title) can drift
    from the live document title (re-titling, normalization changes), which is
    what made the recompute miss every src-mail-* page in prod. A mail source
    page is identified by its stable slug prefix (src-mail-*) plus the "Mail:"
    title pattern both written by orthus.mail.ingest.
    """
    with session() as s:
        rows = s.execute(
            select(wiki_pages.c.slug).where(
                wiki_pages.c.kind == "source",
                wiki_pages.c.scope == "company",
                wiki_pages.c.slug.like(_MAIL_SOURCE_SLUG_PREFIX),
                wiki_pages.c.title.like(_MAIL_SOURCE_TITLE_PREFIX),
            )
        ).all()
    return {row.slug for row in rows}


def _downstream_page_slugs(
    source_slugs: set[str],
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Partition downstream pages that cite any mail source into mail-only vs mixed.

    A downstream page is any non-source page that is the src_page_id of a
    wiki_links row whose dst_slug points at a mail source (dst_slug in
    `source_slugs`). Its provenance is the set of all its src-* citations
    (dst_slug LIKE 'src-%'):

    - mail-only  -> every src-* citation is a mail source slug. Safe to delete:
      removing it loses no shared knowledge.
    - mixed      -> it also cites a non-mail source. NOT deleted; the next
      `make wiki-rebuild` reconciles it by dropping the stale mail citation.

    Returns (mail_only, mixed) as sets of (kind, slug).
    """
    if not source_slugs:
        return set(), set()
    with session() as s:
        prov_rows = s.execute(
            select(wiki_links.c.src_page_id, wiki_links.c.dst_slug).where(
                wiki_links.c.dst_slug.like("src-%"),
            )
        ).all()
        by_page: dict = {}
        for src_page_id, dst_slug in prov_rows:
            by_page.setdefault(src_page_id, set()).add(dst_slug)

        # Only pages that cite at least one mail source are candidates.
        candidate_ids = [pid for pid, citations in by_page.items() if citations & source_slugs]
        if not candidate_ids:
            return set(), set()

        rows = s.execute(
            select(wiki_pages.c.page_id, wiki_pages.c.kind, wiki_pages.c.slug).where(
                wiki_pages.c.page_id.in_(candidate_ids),
                wiki_pages.c.kind != "source",
                wiki_pages.c.scope == "company",
            )
        ).all()

    mail_only: set[tuple[str, str]] = set()
    mixed: set[tuple[str, str]] = set()
    for page_id, kind, slug in rows:
        citations = by_page.get(page_id, set())
        nonmail = citations - source_slugs
        if nonmail:
            mixed.add((kind, slug))
        else:
            mail_only.add((kind, slug))
    return mail_only, mixed


def _delete_wiki_pages(kind_slugs: set[tuple[str, str]]) -> int:
    deleted = 0
    for kind, slug in sorted(kind_slugs):
        # wiki_pages.kind is "source"/"claim"/"task"/"concept"/... ; wiki_store
        # keys are "source"/"claim"/"task"/"page". Anything else (e.g. "concept")
        # is stored under the "page" key.
        store_kind = _STORE_KIND.get(kind, "page")
        if wiki_store.delete_item(store_kind, slug, scope="company", owner_id=None):
            deleted += 1
    return deleted


def _delete_docs_and_embeddings(doc_ids: list) -> tuple[int, int, int]:
    with audit("mail.company_scope_cleanup") as span:
        with session() as s:
            # corpus_chunks.doc_id ON DELETE CASCADE -> documents, but
            # corpus_chunks.embedding_id -> embeddings has NO cascade. Deleting
            # embeddings (or documents) first leaves dangling embedding_id refs
            # and hits corpus_chunks_embedding_id_fkey. Delete chunks first.
            chunks = s.execute(
                delete(corpus_chunks).where(corpus_chunks.c.doc_id.in_(doc_ids))
            ).rowcount
            emb = s.execute(
                delete(embeddings).where(
                    embeddings.c.kind == "corpus_chunk",
                    embeddings.c.ref_id.in_(doc_ids),
                )
            ).rowcount
            docs = s.execute(delete(documents).where(documents.c.doc_id.in_(doc_ids))).rowcount
            s.commit()
        span.add_meta(
            documents=docs, corpus_chunks=chunks, corpus_embeddings=emb, doc_count=len(doc_ids)
        )
    return chunks, emb, docs


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete (default is dry-run printing what would be deleted)",
    )
    parser.add_argument(
        "--source",
        dest="sources",
        action="append",
        help=f"mail source to clean (repeatable); default {MAIL_COMPANY_SOURCES}",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
