from sqlalchemy import func, select

import orthus.documents as documents_module
from orthus.corpus import chunk_markdown, search
from orthus.db import session
from orthus.documents import save_editor_document
from orthus.tables import corpus_chunks, embeddings


def test_chunk_markdown_packs_paragraphs():
    md = "\n\n".join([f"para {i} " + "x" * 300 for i in range(5)])
    chunks = chunk_markdown(md, target_chars=800)
    assert len(chunks) >= 2
    assert all(c.strip() for c in chunks)


def test_chunk_markdown_splits_oversized_paragraph():
    # A newline-free paragraph far larger than the embedding token limit (HTML
    # marketing mail dumped as text) must not become one over-limit chunk.
    md = "word " * 20000  # ~100k chars, single paragraph
    chunks = chunk_markdown(md, target_chars=800, max_chars=2000)
    assert len(chunks) > 1
    assert all(len(c) <= 2000 for c in chunks)
    assert all(c.strip() for c in chunks)


def test_chunk_markdown_hard_cuts_unbroken_run():
    # No whitespace to break on → still capped, never an over-limit chunk.
    md = "x" * 50000
    chunks = chunk_markdown(md, target_chars=800, max_chars=2000)
    assert chunks
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == md


def test_save_editor_document_creates_chunks_and_embeddings(user_id):
    md = (
        "# 휴가 정책\n\n연차는 입사 1년차부터 15일 부여된다.\n\n"
        "병가는 연 5일까지 무급으로 사용할 수 있다.\n\n"
        "재택근무는 주 2회까지 사전 승인 후 가능하다."
    )
    doc_id = save_editor_document(user_id, "휴가 정책", [{"type": "paragraph"}], md)

    with session() as s:
        n_chunks = s.execute(
            select(func.count()).select_from(corpus_chunks).where(corpus_chunks.c.doc_id == doc_id)
        ).scalar_one()
        n_emb = s.execute(
            select(func.count()).select_from(embeddings).where(embeddings.c.ref_id == doc_id)
        ).scalar_one()
    assert n_chunks >= 1
    assert n_emb == n_chunks  # one embedding per chunk


def test_reindex_on_resave_does_not_duplicate(user_id):
    doc_id = save_editor_document(user_id, "t", [{}], "first body content here")
    save_editor_document(user_id, "t", [{}], "second body totally different", doc_id=doc_id)
    with session() as s:
        n = s.execute(
            select(func.count()).select_from(corpus_chunks).where(corpus_chunks.c.doc_id == doc_id)
        ).scalar_one()
        contents = [
            r[0]
            for r in s.execute(
                select(corpus_chunks.c.content).where(corpus_chunks.c.doc_id == doc_id)
            ).all()
        ]
    assert n == 1
    assert "second body" in contents[0]


def test_topk_search_finds_relevant_chunk(user_id, monkeypatch):
    monkeypatch.setattr(documents_module, "_author_into_wiki", lambda *args, **kwargs: None)
    save_editor_document(user_id, "휴가", [{}], "연차 휴가는 입사 1년차부터 15일이 부여됩니다.")
    save_editor_document(user_id, "보안", [{}], "사내 VPN 접속은 2단계 인증이 필요합니다.")
    hits = search(user_id, "연차 휴가 며칠?", k=2)
    assert hits
    assert "연차" in hits[0].content
