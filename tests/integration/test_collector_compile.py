"""P8.4 acceptance: central compile for owner-scoped personal corpus + merged
wiki read plane.

docs/p8-central-consolidation.md §4 / §6 (P8.4 row).

The collector daemon pushes `documents` rows (scope='personal', user=owner) with
no corpus chunks; `POST /collector/compile` runs corpus index + wiki authoring on
the central (company) node. These tests assert:

1. compile e2e: pushed personal docs gain personal-scope corpus_chunks +
   embeddings and personal wiki_pages (owner_id == owner), with wiki-store files
   under personal/<owner>/; a second compile is a no-op (idempotent).
2. owner boundary on the merged read plane: with owner-scope ON, `/ask` scope=all
   as the owner grounds on the new personal page, user B never sees it; the
   `/wiki/pages/{slug}` company-miss fallback returns the owner's own personal
   page but 404s for user B and with the flag off.
3. `/wiki/ask` scope clamp parity with `/ask` `_resolve_scope`.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.collector.auth import hash_collector_token
from orthus.collector.compile import compile_personal_documents
from orthus.db import session
from orthus.models.adapters.mock import MockChat
from orthus.settings import get_settings
from orthus.tables import (
    audit_log,
    collector_tokens,
    corpus_chunks,
    documents,
    embeddings,
    users,
    wiki_pages,
)
from orthus.wiki import store

client = TestClient(app)

COMPANY_NODE = "company"


def _enable(monkeypatch, *, owner_scope: bool = False) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "collector_api_enabled", True)
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", COMPANY_NODE)
    monkeypatch.setattr(settings, "owner_scope_enabled", owner_scope)


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        s.commit()
    return uid


def _make_token(user_id, *, node_id: str = COMPANY_NODE) -> str:
    token = f"dct_{uuid.uuid4().hex}{uuid.uuid4().hex}"
    with session() as s:
        s.execute(
            insert(collector_tokens).values(
                token_id=uuid.uuid4(),
                user_id=user_id,
                node_id=node_id,
                name="test",
                token_hash=hash_collector_token(token),
            )
        )
        s.commit()
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _doc(ext_id: str, content: str, *, source: str = "gws_gmail") -> dict:
    return {
        "source": source,
        "source_external_id": ext_id,
        "title": content[:40],
        "content_md": content,
    }


def _push(token: str, docs: list[dict]) -> dict:
    res = client.post(
        "/collector/ingest/documents",
        json={"documents": docs},
        headers=_headers(token),
    )
    assert res.status_code == 200, res.text
    return res.json()


def _compile(token: str, **body) -> dict:
    res = client.post("/collector/compile", json=body or None, headers=_headers(token))
    assert res.status_code == 200, res.text
    return res.json()


# --- compile e2e + idempotency --------------------------------------------------


def test_compile_indexes_and_authors_owner_personal_docs(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    _push(
        token,
        [
            _doc("msg-1", "프로젝트 알파 회고 메모 한 줄."),
            _doc("msg-2", "베타 출시 일정 정리 메모."),
        ],
    )

    # Before compile: rows exist with NO corpus chunks.
    with session() as s:
        doc_count = len(s.execute(select(documents.c.doc_id)).all())
        chunk_count = len(s.execute(select(corpus_chunks.c.chunk_id)).all())
    assert doc_count == 2
    assert chunk_count == 0

    counts = _compile(token)
    assert counts["indexed"] == 2
    assert counts["authored"] == 2
    assert counts["skipped"] == 0

    with session() as s:
        chunk_rows = s.execute(
            select(corpus_chunks.c.scope).where(corpus_chunks.c.scope == "personal")
        ).all()
        emb_rows = s.execute(
            select(embeddings.c.scope, embeddings.c.user_id).where(
                embeddings.c.kind == "corpus_chunk"
            )
        ).all()
        page_rows = s.execute(
            select(wiki_pages.c.slug, wiki_pages.c.scope, wiki_pages.c.owner_id).where(
                wiki_pages.c.scope == "personal"
            )
        ).all()
    assert chunk_rows and all(r.scope == "personal" for r in chunk_rows)
    assert emb_rows and all(r.scope == "personal" and r.user_id == user_id for r in emb_rows)
    assert page_rows and all(r.scope == "personal" and r.owner_id == user_id for r in page_rows)

    # wiki-store markdown SoR lives under personal/<owner>/ in the tmp store root.
    page_dir = get_settings().wiki_store_path / "personal" / str(user_id) / "wiki"
    assert page_dir.is_dir()
    assert any(page_dir.glob("*.md"))


def test_compile_is_idempotent(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    _push(token, [_doc("msg-1", "감마 운영 노트 한 줄.")])

    first = _compile(token)
    assert first["indexed"] == 1 and first["authored"] == 1

    second = _compile(token)
    assert second == {"indexed": 0, "authored": 0, "skipped": 1, "failed": 0}

    # No duplicate corpus chunks from the second run.
    with session() as s:
        chunks = s.execute(select(corpus_chunks.c.chunk_id)).all()
    n_first = len(chunks)
    assert n_first > 0
    third = _compile(token)
    assert third == {"indexed": 0, "authored": 0, "skipped": 1, "failed": 0}
    with session() as s:
        chunks_after = s.execute(select(corpus_chunks.c.chunk_id)).all()
    assert len(chunks_after) == n_first


def test_compile_skips_unavailable_drive_authoring(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    unavailable_drive = """# demo.mp4

Source: gws_drive
File ID: drive-file-1
MIME: video/mp4
Content: skipped:unsupported_mime

## Content

(content unavailable)
"""
    _push(
        token,
        [
            _doc("drive-1", unavailable_drive, source="gws_drive"),
            _doc("mail-1", "Gmail personal note with useful text.", source="gws_gmail"),
        ],
    )

    first = _compile(token)
    assert first == {"indexed": 2, "authored": 1, "skipped": 0, "failed": 0}

    with session() as s:
        drive_doc = s.execute(
            select(documents.c.doc_id).where(documents.c.source_external_id == "drive-1")
        ).scalar_one()
        drive_chunks = s.execute(
            select(corpus_chunks.c.chunk_id).where(corpus_chunks.c.doc_id == drive_doc)
        ).all()
    assert drive_chunks, "metadata-only Drive blobs still enter personal corpus"
    source_refs = {
        source.source_ref
        for slug in store.list_slugs("source", scope="personal", owner_id=user_id)
        if (source := store.load_source(slug, scope="personal", owner_id=user_id)) is not None
    }
    assert str(drive_doc) not in source_refs

    second = _compile(token)
    assert second == {"indexed": 0, "authored": 0, "skipped": 2, "failed": 0}


def test_compile_records_audit(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    token = _make_token(user_id)
    _push(token, [_doc("msg-1", "델타 메모 한 줄.")])
    _compile(token)
    with session() as s:
        rows = s.execute(
            select(audit_log.c.phase, audit_log.c.meta).where(
                audit_log.c.node == "collector.compile"
            )
        ).all()
    assert any(r.phase == "exit" and r.meta.get("indexed") == 1 for r in rows)


def test_compile_only_touches_token_owner_docs(clean, user_id, monkeypatch):
    """User B's personal docs are never compiled by user A's token."""
    _enable(monkeypatch)
    user_b = _make_user()
    token_a = _make_token(user_id)
    token_b = _make_token(user_b)
    _push(token_a, [_doc("a-1", "A 소유 메모 한 줄.")])
    _push(token_b, [_doc("b-1", "B 소유 메모 한 줄.")])

    counts_a = _compile(token_a)
    assert counts_a["indexed"] == 1 and counts_a["authored"] == 1

    # B still has no chunks until B compiles.
    with session() as s:
        b_doc = s.execute(select(documents.c.doc_id).where(documents.c.user_id == user_b)).first()
        b_chunks = s.execute(
            select(corpus_chunks.c.chunk_id).where(corpus_chunks.c.doc_id == b_doc.doc_id)
        ).all()
    assert b_chunks == []


def test_compile_endpoint_flag_off_returns_404(clean):
    res = client.post("/collector/compile", json={}, headers=_headers("dct_dummy"))
    assert res.status_code == 404


def test_compile_endpoint_session_cookie_rejected(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    # No bearer token -> token auth fails closed.
    res = client.post("/collector/compile", json={})
    assert res.status_code == 401


# --- /ask merged read plane owner boundary --------------------------------------


def _ask_wiki_slugs(local_user_id, question: str, scope: str) -> set[str]:
    res = client.post(
        "/ask",
        json={"question": question, "scope": scope},
        headers={"X-User-Id": str(local_user_id)},
    )
    assert res.status_code == 200, res.text
    return {s["page_slug"] for s in res.json().get("wiki", {}).get("sources", [])}


def test_ask_scope_all_owner_sees_compiled_personal_page_others_do_not(clean, user_id, monkeypatch):
    _enable(monkeypatch, owner_scope=True)
    monkeypatch.setattr(get_settings(), "auth_mode", "demo")
    user_b = _make_user()
    slug = f"epsilon-{uuid.uuid4().hex[:8]}"
    _compile_one_personal_page(user_id, slug)

    a_slugs = _ask_wiki_slugs(user_id, f"{slug} 폴백 메모", "all")
    b_slugs = _ask_wiki_slugs(user_b, f"{slug} 폴백 메모", "all")
    assert slug in a_slugs, "owner grounds on the compiled personal page"
    assert slug not in b_slugs, "user B never grounds on A's personal page"


# --- /wiki/pages/{slug} owner fallback ------------------------------------------


def _compile_one_personal_page(user_id, slug: str) -> None:
    """Compile a single personal doc into a page with a unique scripted slug, so
    the fallback assertions cannot collide with any other test's company page."""
    token = _make_token(user_id)
    _push(token, [_doc(f"ext-{slug}", f"{slug} 폴백 메모 한 줄.")])
    chat = MockChat(rules=[(slug, _distill(slug, page_slug=slug, page_title=slug))])
    result = compile_personal_documents(user_id, chat_model=chat)
    assert result.authored == 1


def test_wiki_page_owner_fallback_flag_on(clean, user_id, monkeypatch):
    _enable(monkeypatch, owner_scope=True)
    monkeypatch.setattr(get_settings(), "auth_mode", "demo")
    user_b = _make_user()
    slug = f"zeta-{uuid.uuid4().hex[:8]}"
    _compile_one_personal_page(user_id, slug)

    # No company page with this slug exists, so the company node falls back to the
    # caller's own personal page only for the owner.
    owner_res = client.get(f"/wiki/pages/{slug}", headers={"X-User-Id": str(user_id)})
    other_res = client.get(f"/wiki/pages/{slug}", headers={"X-User-Id": str(user_b)})
    assert owner_res.status_code == 200, owner_res.text
    assert owner_res.json()["slug"] == slug
    assert other_res.status_code == 404


def test_wiki_page_owner_fallback_flag_off_404(clean, user_id, monkeypatch):
    _enable(monkeypatch, owner_scope=False)
    monkeypatch.setattr(get_settings(), "auth_mode", "demo")
    slug = f"eta-{uuid.uuid4().hex[:8]}"
    _compile_one_personal_page(user_id, slug)

    res = client.get(f"/wiki/pages/{slug}", headers={"X-User-Id": str(user_id)})
    assert res.status_code == 404


# --- /wiki/ask scope clamp parity -----------------------------------------------


def _wiki_ask_slugs(local_user_id, question: str, scope: str) -> set[str]:
    res = client.post(
        "/wiki/ask",
        json={"question": question, "scope": scope},
        headers={"X-User-Id": str(local_user_id)},
    )
    assert res.status_code == 200, res.text
    return {s["page_slug"] for s in res.json().get("sources", [])}


def test_wiki_ask_scope_clamp_parity_flag_off(clean, user_id, monkeypatch):
    """Company node flag OFF: an explicit all/personal request still grounds
    company-only, so the owner's compiled personal page is invisible via /wiki/ask."""
    _enable(monkeypatch, owner_scope=False)
    monkeypatch.setattr(get_settings(), "auth_mode", "demo")
    slug = f"theta-{uuid.uuid4().hex[:8]}"
    _compile_one_personal_page(user_id, slug)

    slugs = _wiki_ask_slugs(user_id, f"{slug} 폴백 메모", "all")
    assert slug not in slugs, "flag off clamps to company; personal page stays hidden"


def test_wiki_ask_scope_all_owner_sees_personal_flag_on(clean, user_id, monkeypatch):
    _enable(monkeypatch, owner_scope=True)
    monkeypatch.setattr(get_settings(), "auth_mode", "demo")
    user_b = _make_user()
    slug = f"iota-{uuid.uuid4().hex[:8]}"
    _compile_one_personal_page(user_id, slug)

    a_slugs = _wiki_ask_slugs(user_id, f"{slug} 폴백 메모", "all")
    b_slugs = _wiki_ask_slugs(user_b, f"{slug} 폴백 메모", "all")
    assert slug in a_slugs, "owner grounds on own personal page via /wiki/ask scope=all"
    assert slug not in b_slugs, "user B never grounds on A's personal page"


# --- direct function call with distinct mock pages ------------------------------


def test_compile_function_authors_distinct_pages(clean, user_id, monkeypatch):
    """Direct call with a scripted MockChat: distinct titles -> distinct source
    slugs, and a re-run skips already-compiled docs."""
    _enable(monkeypatch)
    token = _make_token(user_id)
    _push(
        token,
        [
            _doc("k-1", "카파 운영 한 줄."),
            _doc("k-2", "람다 운영 한 줄."),
        ],
    )

    chat = MockChat(
        rules=[
            (
                "카파",
                _distill("카파", page_slug="kappa", page_title="Kappa"),
            ),
            (
                "람다",
                _distill("람다", page_slug="lambda", page_title="Lambda"),
            ),
        ]
    )
    result = compile_personal_documents(user_id, chat_model=chat)
    assert result.indexed == 2 and result.authored == 2 and result.skipped == 0

    slugs = set(store.list_slugs("page", scope="personal", owner_id=user_id))
    assert {"kappa", "lambda"} <= slugs

    again = compile_personal_documents(user_id, chat_model=chat)
    assert again.indexed == 0 and again.authored == 0 and again.skipped == 2


def test_compile_isolates_failed_distill(clean, user_id, monkeypatch):
    """One doc whose distill raises is counted as failed; the rest of the batch
    still authors, and a re-run retries (authors) the previously-failed doc."""
    _enable(monkeypatch)
    token = _make_token(user_id)
    _push(
        token,
        [
            _doc("ok-1", "성공 메모 하나 한 줄."),
            _doc("boom", "실패할 메모 한 줄."),
            _doc("ok-2", "성공 메모 둘 한 줄."),
        ],
    )

    with session() as s:
        boom_doc_id = s.execute(
            select(documents.c.doc_id).where(documents.c.source_external_id == "boom")
        ).scalar_one()

    import orthus.collector.compile as compile_mod

    real_distill = compile_mod.distill_document

    def _flaky_distill(doc_id, uid, *, chat_model=None):
        if doc_id == boom_doc_id:
            raise RuntimeError("distill boom")
        return real_distill(doc_id, uid, chat_model=chat_model)

    monkeypatch.setattr(compile_mod, "distill_document", _flaky_distill)

    result = compile_personal_documents(user_id)
    assert result.indexed == 3
    assert result.authored == 2
    assert result.failed == 1
    assert result.skipped == 0

    # The two healthy docs authored personal pages; the failed doc did not.
    with session() as s:
        page_rows = s.execute(
            select(wiki_pages.c.slug).where(wiki_pages.c.scope == "personal")
        ).all()
    assert len(page_rows) >= 2

    # A failure span was recorded for the bad doc.
    with session() as s:
        fail_rows = s.execute(
            select(audit_log.c.meta).where(audit_log.c.node == "collector.compile.distill_failed")
        ).all()
    assert any(r.meta.get("doc_id") == str(boom_doc_id) for r in fail_rows)

    # Re-run with distill healthy: only the previously-failed doc still needs
    # authoring, so it authors now and the others skip.
    monkeypatch.setattr(compile_mod, "distill_document", real_distill)
    again = compile_personal_documents(user_id)
    assert again.indexed == 0
    assert again.authored == 1
    assert again.failed == 0
    assert again.skipped == 2


def _distill(term: str, *, page_slug: str, page_title: str) -> str:
    import json

    return json.dumps(
        {
            "summary": f"{term} 요약",
            "key_concepts": [],
            "terminology": [],
            "open_questions": [],
            "claims": [
                {
                    "claim": f"{term} 운영 메모.",
                    "evidence": f"{term} 운영 메모.",
                    "confidence": "medium",
                    "page": {"slug": page_slug, "title": page_title},
                    "conflicting": [],
                }
            ],
        }
    )
