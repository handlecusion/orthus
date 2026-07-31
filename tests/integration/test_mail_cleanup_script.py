"""Regression tests for scripts/ops/mail_company_scope_cleanup.py.

Three bugs shipped in PR #321 and were only caught running against prod:

1. corpus_chunks FK violation: deleting embeddings/documents before corpus_chunks
   hit corpus_chunks_embedding_id_fkey (embedding_id has no ON DELETE CASCADE).
2. Source wiki pages (kind=source, src-mail-*) were never deleted — only
   downstream concept pages were targeted, so the mail markdown survived.
3. Downstream page matching recomputed source slugs via source_slug_from_title,
   which drifted from the stored src-mail-* dst_slugs, returning 0 traceable
   pages even though prod had hundreds of mail-only downstream pages.

These tests build a realistic fixture (mail document + corpus_chunks + corpus
embedding + kind=source wiki page + mail-only claim + MIXED claim citing both a
mail and a non-mail source) and assert the cleanup deletes the mail-only graph
while preserving the mixed-provenance page and reporting it for wiki-rebuild.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import insert, select

from orthus.schemas import canonical
from orthus.schemas.canonical import SCHEMA_VERSION
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import corpus_chunks, documents, embeddings, wiki_links, wiki_pages
from orthus.wiki import store as wiki_store
from orthus.wiki.slugs import source_slug_from_title
from tests.conftest import _truncate_data_tables

import scripts.ops.mail_company_scope_cleanup as cleanup


@pytest.fixture(autouse=True)
def _truncate_after_each():
    """Truncate data tables AFTER each test in this file.

    `clean` only truncates at test START, so the cases here that intentionally
    leave committed rows (dry-run = deletes nothing; mixed-claim = preserved by
    design) would otherwise leak company mail documents/wiki rows into later test
    files and break order-dependent suites. Truncating at teardown keeps this
    file self-contained regardless of collection order.
    """
    yield
    _truncate_data_tables()


def _insert_mail_doc(user_id: uuid.UUID, subject: str) -> tuple[uuid.UUID, str, str]:
    """Insert a scope=company mail document + corpus chunk + embedding.

    Returns (doc_id, title, source_slug). Mirrors orthus.mail.ingest: title is
    "Mail: <subject>" so the wiki source slug is src-mail-*.
    """
    doc_id = uuid.uuid4()
    title = f"Mail: {subject}"
    embedding_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(documents).values(
                doc_id=doc_id,
                user_id=user_id,
                title=title,
                block_json=[{}],
                markdown=f"본문: {subject}",
                source="mail_nova",
                schema_version=SCHEMA_VERSION,
                scope="company",
                project="company",
            )
        )
        s.execute(
            insert(embeddings).values(
                embedding_id=embedding_id,
                user_id=user_id,
                kind="corpus_chunk",
                ref_id=doc_id,
                vec=[0.0] * 1024,
                meta={},
                schema_version=SCHEMA_VERSION,
                model_version="mock",
                scope="company",
                project="company",
            )
        )
        s.execute(
            insert(corpus_chunks).values(
                chunk_id=uuid.uuid4(),
                doc_id=doc_id,
                ordinal=0,
                content=f"본문: {subject}",
                embedding_id=embedding_id,
                meta={},
                scope="company",
                project="company",
            )
        )
        s.commit()
    return doc_id, title, source_slug_from_title(title, doc_id)


def _write_source(slug: str, title: str, doc_id: uuid.UUID, user_id: uuid.UUID) -> None:
    wiki_store.write_source(
        canonical.WikiSource(
            slug=slug,
            title=title,
            source_type="corpus_doc",
            source_ref=str(doc_id),
            ingested_at=datetime.now(UTC),
            summary="요약",
        ),
        user_id=user_id,
    )


def _write_claim(slug: str, supporting: list[str], user_id: uuid.UUID) -> None:
    wiki_store.write_claim(
        canonical.WikiClaim(
            slug=slug,
            claim="주장",
            supporting=supporting,
            confidence="medium",
            last_reviewed=date.today(),
            evidence="근거",
        ),
        user_id=user_id,
    )


def _scenario(tmp_path, monkeypatch, user_id):
    """Build the full fixture and return identifiers used by the assertions."""
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path)

    doc_id, title, mail_src = _insert_mail_doc(user_id, "견적 문의")
    # A genuine non-mail company source (e.g. Notion) the mixed claim also cites.
    nonmail_src = "src-notion-policy-deadbeef"

    _write_source(mail_src, title, doc_id, user_id)
    _write_claim("claim-mail-only", [mail_src], user_id)
    _write_claim("claim-mixed", [mail_src, nonmail_src], user_id)

    return doc_id, mail_src


def test_cleanup_deletes_mail_graph_and_preserves_mixed(tmp_path, monkeypatch, user_id, capsys):
    doc_id, mail_src = _scenario(tmp_path, monkeypatch, user_id)

    rc = cleanup.main(["--apply", "--source", "mail_nova"])
    assert rc == 0
    out = capsys.readouterr().out

    with session() as s:
        doc_left = s.execute(select(documents.c.doc_id).where(documents.c.doc_id == doc_id)).first()
        chunk_left = s.execute(
            select(corpus_chunks.c.chunk_id).where(corpus_chunks.c.doc_id == doc_id)
        ).first()
        emb_left = s.execute(
            select(embeddings.c.embedding_id).where(embeddings.c.ref_id == doc_id)
        ).first()
        src_left = s.execute(
            select(wiki_pages.c.page_id).where(wiki_pages.c.slug == mail_src)
        ).first()
        mail_only_left = s.execute(
            select(wiki_pages.c.page_id).where(wiki_pages.c.slug == "claim-mail-only")
        ).first()
        mixed_left = s.execute(
            select(wiki_pages.c.page_id, wiki_pages.c.slug).where(
                wiki_pages.c.slug == "claim-mixed"
            )
        ).first()
        # The mixed page keeps ALL its provenance links (including the mail one) so
        # wiki-rebuild can reconcile it deterministically.
        mixed_links = {
            r.dst_slug
            for r in s.execute(
                select(wiki_links.c.dst_slug).where(wiki_links.c.src_page_id == mixed_left.page_id)
            ).all()
        }

    assert doc_left is None, "mail document must be deleted"
    assert chunk_left is None, "corpus_chunks must be deleted"
    assert emb_left is None, "corpus embedding must be deleted"
    assert src_left is None, "kind=source mail wiki page must be deleted"
    assert mail_only_left is None, "mail-only claim must be deleted"
    assert mixed_left is not None, "mixed-provenance claim must be preserved"
    assert mail_src in mixed_links and "src-notion-policy-deadbeef" in mixed_links

    # The wiki markdown files for deleted pages are gone; the mixed one remains.
    assert not (tmp_path / "company" / "sources" / f"{mail_src}.md").exists()
    assert not (tmp_path / "company" / "claims" / "claim-mail-only.md").exists()
    assert (tmp_path / "company" / "claims" / "claim-mixed.md").exists()

    # The mixed page is reported as needing a wiki-rebuild, not deleted.
    assert "needs wiki-rebuild" in out
    assert "claim-mixed" in out


def test_cleanup_corpus_chunks_fk_ordering_does_not_raise(tmp_path, monkeypatch, user_id):
    """The original prod failure: a chunk references the embedding, so deleting
    the embedding before the chunk raised corpus_chunks_embedding_id_fkey.
    Deleting chunks first must succeed."""
    doc_id, _mail_src = _scenario(tmp_path, monkeypatch, user_id)

    # Must not raise IntegrityError.
    rc = cleanup.main(["--apply", "--source", "mail_nova"])
    assert rc == 0

    with session() as s:
        assert (
            s.execute(
                select(corpus_chunks.c.chunk_id).where(corpus_chunks.c.doc_id == doc_id)
            ).first()
            is None
        )


def test_cleanup_is_idempotent(tmp_path, monkeypatch, user_id):
    """Re-running after a full deletion must not error and must delete nothing."""
    _scenario(tmp_path, monkeypatch, user_id)

    assert cleanup.main(["--apply", "--source", "mail_nova"]) == 0
    # Second run finds no scope=company mail docs; returns cleanly.
    assert cleanup.main(["--apply", "--source", "mail_nova"]) == 0


def test_cleanup_dry_run_deletes_nothing(tmp_path, monkeypatch, user_id):
    doc_id, mail_src = _scenario(tmp_path, monkeypatch, user_id)

    assert cleanup.main(["--source", "mail_nova"]) == 0  # no --apply

    with session() as s:
        assert (
            s.execute(select(documents.c.doc_id).where(documents.c.doc_id == doc_id)).first()
            is not None
        )
        assert (
            s.execute(select(wiki_pages.c.page_id).where(wiki_pages.c.slug == mail_src)).first()
            is not None
        )
