"""Corpus pipeline: document markdown -> normalize -> chunk -> embed -> pgvector.

Both ingestion sources (Notion import, editor save) flow through `index_document`,
so RAG (wiki) and the 비서's semantic grounding read one unified corpus."""

from __future__ import annotations

import re
import uuid
from uuid import UUID

from sqlalchemy import delete, insert, select, text

from orthus.audit import audit
from orthus.db import session
from orthus.models.registry import get_embedding_model
from orthus.schemas.canonical import SCHEMA_VERSION, ChunkHit
from orthus.tables import corpus_chunks, embeddings

_TARGET_CHARS = 800
# Hard per-chunk ceiling. A chunk is one embedding input; the embeddings API
# rejects inputs over its token limit (8192 for text-embedding-3) with a 400 that
# fails the whole document — e.g. an HTML marketing mail dumped as one
# newline-free paragraph. 2000 chars stays well under that even for CJK text
# (~3 tokens/char worst case → ~6000 tokens).
_MAX_CHARS = 2000
_WS = re.compile(r"[ \t]+")


def _split_oversized(p: str, max_chars: int) -> list[str]:
    """Split a paragraph longer than max_chars into <=max_chars pieces, breaking
    on the last newline/space before the limit when possible, else hard-cutting."""
    pieces: list[str] = []
    while len(p) > max_chars:
        window = p[:max_chars]
        cut = max(window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = max_chars
        head = p[:cut].strip()
        if head:
            pieces.append(head)
        p = p[cut:].strip()
    if p:
        pieces.append(p)
    return pieces


def _normalize(md: str) -> str:
    lines = [_WS.sub(" ", ln).rstrip() for ln in md.replace("\r\n", "\n").split("\n")]
    # collapse 3+ blank lines to one blank line
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln == "":
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def chunk_markdown(
    md: str, target_chars: int = _TARGET_CHARS, *, max_chars: int = _MAX_CHARS
) -> list[str]:
    """Pack paragraphs (blank-line separated) into ~target_chars chunks.
    A single oversized paragraph becomes its own chunk; one longer than
    max_chars is hard-split so no chunk exceeds the embedding token limit."""
    md = _normalize(md)
    if not md:
        return []
    paras: list[str] = []
    for raw in md.split("\n\n"):
        p = raw.strip()
        if not p:
            continue
        paras.extend(_split_oversized(p, max_chars) if len(p) > max_chars else [p])
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) + 2 > target_chars:
            chunks.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    return chunks


def index_document(
    doc_id: UUID,
    user_id: UUID,
    markdown: str,
    *,
    scope: str = "company",
    project: str = "atlas",
) -> int:
    """(Re)index a document: drop prior chunks/embeddings, chunk, embed, store.
    Idempotent per doc — safe to call on every save. Returns chunk count.

    `scope` ('company' | 'personal') is stamped on the new corpus_chunks and
    their embeddings so retrieval can isolate per-tenant content (P2.1). `project`
    is likewise stamped so retrieval can scope to a company→project bucket (P2)."""
    pieces = chunk_markdown(markdown)
    embedder = get_embedding_model()

    with session() as s:
        # Clear prior chunks + their embeddings for this doc (reindex).
        old_emb_ids = [
            r[0]
            for r in s.execute(
                select(corpus_chunks.c.embedding_id).where(corpus_chunks.c.doc_id == doc_id)
            ).all()
            if r[0] is not None
        ]
        s.execute(delete(corpus_chunks).where(corpus_chunks.c.doc_id == doc_id))
        if old_emb_ids:
            s.execute(delete(embeddings).where(embeddings.c.embedding_id.in_(old_emb_ids)))
        s.commit()

        if not pieces:
            return 0

        with audit("corpus.embed") as span:
            vectors = embedder.embed(pieces)
            span.add_meta(model_version=embedder.model_version, n_chunks=len(pieces))

        for ordinal, (content, vec) in enumerate(zip(pieces, vectors)):
            emb_id = uuid.uuid4()
            s.execute(
                insert(embeddings).values(
                    embedding_id=emb_id,
                    user_id=user_id,
                    kind="corpus_chunk",
                    ref_id=doc_id,
                    vec=vec,
                    meta={"ordinal": ordinal},
                    schema_version=SCHEMA_VERSION,
                    model_version=embedder.model_version,
                    scope=scope,
                    project=project,
                )
            )
            s.execute(
                insert(corpus_chunks).values(
                    chunk_id=uuid.uuid4(),
                    doc_id=doc_id,
                    ordinal=ordinal,
                    content=content,
                    embedding_id=emb_id,
                    meta={},
                    scope=scope,
                    project=project,
                )
            )
        s.commit()
    return len(pieces)


def search(user_id: UUID, query: str, k: int = 5) -> list[ChunkHit]:
    """Top-k corpus chunks for a query, cosine similarity over pgvector."""
    embedder = get_embedding_model()
    with audit("corpus.search") as span:
        qvec = embedder.embed([query])[0]
        span.add_meta(model_version=embedder.model_version, k=k)
    distance = embeddings.c.vec.cosine_distance(qvec).label("distance")
    stmt = (
        select(
            corpus_chunks.c.chunk_id,
            corpus_chunks.c.doc_id,
            corpus_chunks.c.ordinal,
            corpus_chunks.c.content,
            distance,
        )
        .join(embeddings, corpus_chunks.c.embedding_id == embeddings.c.embedding_id)
        .where(embeddings.c.user_id == user_id, embeddings.c.kind == "corpus_chunk")
        .order_by(distance)
        .limit(k)
    )
    with session() as s:
        # ``idx_embeddings_vec`` is an IVFFlat ANN index. PostgreSQL applies the
        # user/kind predicates *after* the approximate scan, so a small filtered
        # partition can return no rows when candidates are dominated by other
        # partitions or dead tuples from the integration-suite churn. Corpus
        # retrieval requires exact top-k within this user's partition. Keep the
        # shared ANN index for other retrieval paths, but make this transaction
        # use the selective btree/seq path followed by an exact distance sort.
        s.execute(text("SET LOCAL enable_indexscan = off"))
        rows = s.execute(stmt).all()
    return [
        ChunkHit(
            chunk_id=r.chunk_id,
            doc_id=r.doc_id,
            ordinal=r.ordinal,
            content=r.content,
            score=1.0 - float(r.distance),
        )
        for r in rows
    ]
