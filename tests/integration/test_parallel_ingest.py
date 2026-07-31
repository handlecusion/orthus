"""P2 ingest perf: parallel-distill import must produce the SAME wiki/notion_rows
result as the serial path, with consolidate kept serial+ordered (deterministic
conflict detection). Uses MockChat so distill output is deterministic.

Postgres must be up on localhost:5433 (run `make up && make migrate`)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select

from orthus.connectors.base import Connector, run_parallel_import
from orthus.db import session
from orthus.documents import upsert_source_document
from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import InternalDocument
from orthus.settings import get_settings
from orthus.tables import (
    corpus_chunks,
    documents,
    embeddings,
    notion_rows,
    structured_rows,
    wiki_chunks,
    wiki_links,
    wiki_pages,
)
from orthus.wiki import store
from orthus.wiki.author import rebuild_all_parallel


class _MemConnector(Connector):
    """Yields a fixed list of InternalDocuments (no network)."""

    def __init__(self, docs: list[InternalDocument]):
        self._docs = docs

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        yield from self._docs


def _distill_json(summary: str, claim: str, *, page_slug: str, page_title: str):
    return json.dumps(
        {
            "summary": summary,
            "key_concepts": [],
            "terminology": [],
            "open_questions": [],
            "claims": [
                {
                    "claim": claim,
                    "evidence": f"근거: {claim}",
                    "confidence": "high",
                    "page": {"slug": page_slug, "title": page_title},
                    "conflicting": [],
                }
            ],
        }
    )


def _make_docs(n: int) -> list[InternalDocument]:
    docs = []
    for i in range(n):
        project = "nova" if i % 2 else "atlas"
        docs.append(
            InternalDocument(
                title=f"Doc {i}",
                markdown=f"body number {i}",
                source="notion",
                source_external_id=f"ext-{i}",
                source_db_name=f"db-{project}",
                source_last_edited_at=datetime(2024, 1, 1, tzinfo=UTC),
                project=project,
                structured={
                    "db_id": f"db-{project}",
                    "db_name": f"db-{project}",
                    "row_id": f"ext-{i}",
                    "properties": {"name": f"Doc {i}"},
                },
            )
        )
    return docs


def _chat_for(docs: list[InternalDocument]) -> MockChat:
    """One distill rule per doc, keyed on the doc body so each yields a distinct
    page/claim. (MockChat picks the first matching needle.)"""
    rules = []
    for i, _d in enumerate(docs):
        rules.append(
            (
                f"body number {i}",
                _distill_json(
                    f"summary {i}",
                    f"claim about doc {i}",
                    page_slug=f"page-{i}",
                    page_title=f"Page {i}",
                ),
            )
        )
    return MockChat(rules=rules)


def _store_snapshot(root) -> dict:
    return {
        "sources": store.list_slugs("source", root=root),
        "claims": store.list_slugs("claim", root=root),
        "pages": store.list_slugs("page", root=root),
        "tasks": store.list_slugs("task", root=root),
    }


def _notion_projects() -> dict:
    with session() as s:
        rows = s.execute(
            select(notion_rows.c.project, func.count()).group_by(notion_rows.c.project)
        ).all()
    return {p: n for p, n in rows}


def _wipe_docs():
    with session() as s:
        for tbl in (
            corpus_chunks,
            wiki_links,
            wiki_chunks,
            wiki_pages,
            embeddings,
            notion_rows,
            documents,
        ):
            s.execute(delete(tbl))
        s.commit()


def test_parallel_matches_serial(user_id: UUID, tmp_path, monkeypatch):
    """Parallel-distill import yields the same wiki slugs + notion_rows projects as
    the true serial path (per-doc upsert_source_document with inline authoring)."""
    docs = _make_docs(6)
    settings = get_settings()
    chat = _chat_for(docs)

    # --- serial baseline: per-doc upsert with inline authoring (the slow path) ---
    serial_root = tmp_path / "serial"
    monkeypatch.setattr(settings, "wiki_store_path", serial_root)
    for d in docs:
        upsert_source_document(user_id, d, chat_model=chat)
    serial_snap = _store_snapshot(serial_root)
    serial_projects = _notion_projects()

    _wipe_docs()

    # --- parallel path into its own store root, concurrency > 1 ---
    parallel_root = tmp_path / "parallel"
    monkeypatch.setattr(settings, "wiki_store_path", parallel_root)
    rep_par = run_parallel_import(
        _MemConnector(docs),
        user_id,
        continue_on_error=True,
        concurrency=4,
        chat_model=chat,
    )

    assert rep_par.created == 6
    par_snap = _store_snapshot(parallel_root)
    # Source slugs are doc_id-keyed so they differ between insert cycles;
    # verify count parity instead of exact slug identity.
    assert len(par_snap["sources"]) == len(serial_snap["sources"])
    assert par_snap["claims"] == serial_snap["claims"]
    assert par_snap["pages"] == serial_snap["pages"]
    assert par_snap["tasks"] == serial_snap["tasks"]
    assert _notion_projects() == serial_projects
    assert serial_projects == {"atlas": 3, "nova": 3}


def test_parallel_concurrency_one_equals_many(user_id: UUID, tmp_path, monkeypatch):
    """Same parallel path at concurrency=1 and concurrency=8 yields identical wiki
    output -- consolidate ordering is independent of distill scheduling."""
    docs = _make_docs(5)
    settings = get_settings()

    root1 = tmp_path / "c1"
    monkeypatch.setattr(settings, "wiki_store_path", root1)
    run_parallel_import(
        _MemConnector(docs),
        user_id,
        continue_on_error=True,
        concurrency=1,
        chat_model=_chat_for(docs),
    )
    snap1 = _store_snapshot(root1)

    _wipe_docs()

    root8 = tmp_path / "c8"
    monkeypatch.setattr(settings, "wiki_store_path", root8)
    run_parallel_import(
        _MemConnector(docs),
        user_id,
        continue_on_error=True,
        concurrency=8,
        chat_model=_chat_for(docs),
    )
    snap8 = _store_snapshot(root8)

    # Source slugs are doc_id-keyed so they differ between insert cycles;
    # verify structural equality for content-derived slugs only.
    assert len(snap1["sources"]) == len(snap8["sources"])
    assert snap1["claims"] == snap8["claims"]
    assert snap1["pages"] == snap8["pages"]
    assert snap1["tasks"] == snap8["tasks"]


def test_parallel_preserves_conflict_no_overwrite(user_id: UUID, tmp_path, monkeypatch):
    """Two docs that distill the SAME page slug + SAME claim text must dedupe to a
    single claim with the original text (no silent overwrite) -- proving consolidate
    stays serial+deterministic under the parallel path."""
    settings = get_settings()
    root = tmp_path / "store"
    monkeypatch.setattr(settings, "wiki_store_path", root)

    docs = _make_docs(2)
    first_claim = "원본 주장"
    chat = MockChat(
        rules=[
            (
                "body number 0",
                _distill_json("s0", first_claim, page_slug="shared", page_title="Shared"),
            ),
            (
                "body number 1",
                json.dumps(
                    {
                        "summary": "s1",
                        "key_concepts": [],
                        "terminology": [],
                        "open_questions": [],
                        "claims": [
                            {
                                "claim": first_claim,
                                "evidence": "다른 근거",
                                "confidence": "low",
                                "page": {"slug": "shared", "title": "Shared"},
                                "conflicting": [],
                            }
                        ],
                    }
                ),
            ),
        ]
    )
    run_parallel_import(
        _MemConnector(docs),
        user_id,
        continue_on_error=True,
        concurrency=2,
        chat_model=chat,
    )

    claim_slugs = store.list_slugs("claim", root=root)
    shared = [store.load_claim(s, root=root) for s in claim_slugs]
    matching = [c for c in shared if c.claim == first_claim]
    assert len(matching) == 1
    assert store.exists("page", "shared", root=root)


def test_parallel_skips_failing_distill(user_id: UUID, tmp_path, monkeypatch):
    """A doc whose distill raises is counted as an error + skipped; others succeed
    (continue_on_error)."""
    docs = _make_docs(4)
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path / "store")

    class _BoomChat(MockChat):
        def complete(self, system, user, *, json_only=False):
            if "body number 2" in f"{system}\n{user}":
                raise RuntimeError("distill blew up")
            return super().complete(system, user, json_only=json_only)

    chat = _BoomChat(rules=_chat_for(docs).rules)
    report = run_parallel_import(
        _MemConnector(docs),
        user_id,
        continue_on_error=True,
        concurrency=4,
        chat_model=chat,
    )

    assert report.created == 4
    assert report.errors == 1
    pages = store.list_slugs("page", root=tmp_path / "store")
    assert "page-2" not in pages
    assert {"page-0", "page-1", "page-3"} <= set(pages)
    with session() as s:
        n = s.execute(select(func.count()).select_from(documents)).scalar_one()
    assert n == 4


def test_parallel_import_preserves_doc_but_skips_wiki_authoring(
    user_id: UUID, tmp_path, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path / "store")
    doc = InternalDocument(
        title="Slack C1: 확인",
        markdown="# Slack C1 1.0\n\n## Transcript\n\n확인\n",
        source="slack",
        source_external_id="slack:C1:1.0",
        source_last_edited_at=datetime(2024, 1, 1, tzinfo=UTC),
        project="company",
        structured_rows=[
            {
                "source": "slack",
                "record_type": "link",
                "record_key": "slack:C1:1.0:link:1",
                "properties": {"url": "https://example.com"},
            }
        ],
        wiki_authoring=False,
        wiki_skip_reason="slack_ack_only",
    )

    class _ShouldNotRun(MockChat):
        def complete(self, system, user, *, json_only=False):
            raise AssertionError("gated Slack doc should not distill")

    report = run_parallel_import(
        _MemConnector([doc]),
        user_id,
        continue_on_error=True,
        concurrency=2,
        chat_model=_ShouldNotRun(rules=[]),
    )

    assert report.created == 1
    assert report.errors == 0
    with session() as s:
        n = s.execute(select(func.count()).select_from(documents)).scalar_one()
        corpus_n = s.execute(select(func.count()).select_from(corpus_chunks)).scalar_one()
        rows_n = s.execute(select(func.count()).select_from(structured_rows)).scalar_one()
    assert n == 1
    assert corpus_n > 0
    assert rows_n == 1
    assert store.list_slugs("source", root=tmp_path / "store") == []


def test_parallel_propagates_when_not_continue(user_id: UUID, tmp_path, monkeypatch):
    """Without continue_on_error, a distill failure aborts the run."""
    docs = _make_docs(3)
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path / "store")

    class _BoomChat(MockChat):
        def complete(self, system, user, *, json_only=False):
            if "body number 1" in f"{system}\n{user}":
                raise RuntimeError("boom")
            return super().complete(system, user, json_only=json_only)

    chat = _BoomChat(rules=_chat_for(docs).rules)
    with pytest.raises(RuntimeError):
        run_parallel_import(
            _MemConnector(docs),
            user_id,
            continue_on_error=False,
            concurrency=4,
            chat_model=chat,
        )


def test_rebuild_parallel_fatal_usage_limit_aborts_even_continue(
    user_id: UUID, tmp_path, monkeypatch
):
    """Claude/session limit class failures must stop rebuild, not skip the rest."""
    _wipe_docs()
    docs = _make_docs(3)
    settings = get_settings()
    monkeypatch.setattr(settings, "wiki_store_path", tmp_path / "store")
    for doc in docs:
        upsert_source_document(user_id, doc, defer_authoring=True)

    class _LimitChat(MockChat):
        def complete(self, system, user, *, json_only=False):
            if "body number 1" in f"{system}\n{user}":
                raise RuntimeError("You've hit your session limit · resets 1am")
            return super().complete(system, user, json_only=json_only)

    with pytest.raises(RuntimeError, match="wiki_distill_fatal"):
        rebuild_all_parallel(
            concurrency=2,
            continue_on_error=True,
            chat_model=_LimitChat(rules=_chat_for(docs).rules),
        )


def test_rebuild_parallel_skip_authored_resume(user_id: UUID, tmp_path, monkeypatch):
    """Resume mode skips docs whose source_ref already exists in wiki-store."""
    _wipe_docs()
    docs = _make_docs(2)
    settings = get_settings()
    root = tmp_path / "store"
    monkeypatch.setattr(settings, "wiki_store_path", root)
    for doc in docs:
        upsert_source_document(user_id, doc, defer_authoring=True)

    first = rebuild_all_parallel(
        concurrency=2,
        continue_on_error=True,
        chat_model=_chat_for(docs),
    )
    assert first["documents"] == 2

    class _ShouldNotRun(MockChat):
        def complete(self, system, user, *, json_only=False):
            raise AssertionError("skip_authored should avoid LLM calls")

    resumed = rebuild_all_parallel(
        concurrency=2,
        continue_on_error=True,
        skip_authored=True,
        chat_model=_ShouldNotRun(rules=[]),
    )
    assert resumed["documents"] == 0
    assert len(store.list_slugs("source", root=root)) == 2


def test_rebuild_parallel_skips_existing_slack_noise(user_id: UUID, tmp_path, monkeypatch):
    _wipe_docs()
    settings = get_settings()
    root = tmp_path / "store"
    monkeypatch.setattr(settings, "wiki_store_path", root)
    doc = InternalDocument(
        title="Slack C1: https://example.com",
        markdown="# Slack C1 1.0\n\nSource: slack\nChannel: C1\nThread: 1.0\nMessages: 1\n\n## Transcript\n\nhttps://example.com\n",
        source="slack",
        source_external_id="slack:C1:1.0",
        source_last_edited_at=datetime(2024, 1, 1, tzinfo=UTC),
        project="company",
    )
    upsert_source_document(user_id, doc, defer_authoring=True)

    result = rebuild_all_parallel(concurrency=2, chat_model=MockChat(default='{"claims": []}'))

    assert result["documents"] == 0
    assert result["skipped"] == 1
    assert store.list_slugs("source", root=root) == []
