"""Wiki-page grounding search (docs/llm-wiki.md §8).

Top-k over `wiki_chunks` (the self-authored claim/page bodies) via pgvector,
joined back to `wiki_pages` for slug/title/kind and to `wiki_links` for the
provenance chain. Mirrors `corpus.pipeline.search`, but the answer path grounds
ONLY on compiled wiki pages — never raw corpus chunks (§8 raw-chunk RAG removed).

Tenant isolation (P2.1, docs/architecture-v2.md §2): a user sees the shared company
layer plus their OWN personal layer, never another user's personal content. The
scope dimension lives on `embeddings.scope` (+ `embeddings.user_id` for ownership)."""

from __future__ import annotations

import re
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, case, exists, func, or_, select

from orthus.audit import audit
from orthus.db import session
from orthus.models.registry import get_embedding_model
from orthus.schemas.canonical import WikiSourceRef
from orthus.settings import get_settings
from orthus.tables import (
    connector_accounts,
    documents,
    embeddings,
    wiki_chunks,
    wiki_links,
    wiki_pages,
)
from orthus.wiki.slugs import source_slug_from_title

# Link relations that record where a page/claim's knowledge came from.
_PROVENANCE_RELS = ("derived_from", "supports")
_WORD = re.compile(r"[0-9A-Za-z가-힣_./+-]+")
_LEXICAL_STOPWORDS = {
    "내용",
    "요약",
    "알려줘",
    "정리해줘",
    "어떤",
    "뭐야",
    "무슨",
    "있어",
    "있는",
    "문서",
    "기록",
    "작업",
    "목록",
    "리스트",
    "상태",
    "이슈",
    "프로젝트",
    "메시지",
    "slack",
}
# Particles stripped from a query token's tail before lexical matching. ORDER MATTERS:
# `_normalize_term` returns on the first `endswith` hit, so a longer suffix must precede
# any shorter suffix it contains ("에서는" before "에서", "이랑" before "랑"), or the shorter
# one strips first and the compound is never fully removed.
_KOREAN_SUFFIXES = (
    "에서는",
    "에게는",
    "으로",
    "에서",
    "에는",
    "에게",
    "까지",
    "부터",
    "처럼",
    "라고",
    "이고",
    "이랑",
    "하고",
    "한테",
    "보다",
    "가",
    "이",
    "은",
    "는",
    "을",
    "를",
    "와",
    "과",
    "랑",
    "에",
    "의",
)
_LEXICAL_SYNONYMS = {
    "직원": ("팀", "팀원", "멤버", "구성원"),
    "팀원": ("직원", "멤버", "구성원"),
    "멤버": ("직원", "팀원", "구성원"),
    "구성원": ("직원", "팀원", "멤버"),
}


def _scope_filter(user_id: UUID, scope: str):
    """SQL predicate over `embeddings` enforcing tenant visibility.

    - company: visible to everyone (no user_id restriction).
    - personal: visible only to its owner (embeddings.user_id == user_id).
    `scope='all'` (default) = company OR own-personal."""
    company = embeddings.c.scope == "company"
    own_personal = (embeddings.c.scope == "personal") & (embeddings.c.user_id == user_id)
    if scope == "company":
        return company
    if scope == "personal":
        return own_personal
    return or_(company, own_personal)


def retrieve(
    user_id: UUID,
    query: str,
    *,
    k: int = 5,
    scope: str = "all",
    project: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    source: str | None = None,
    page_slugs: set[str] | None = None,
) -> list[WikiSourceRef]:
    """Top-k wiki chunks for a query, cosine similarity over pgvector.

    `scope` ('all' | 'company' | 'personal') sets tenant visibility (see
    `_scope_filter`); the default 'all' returns company + the user's own personal.
    `project` (None | 'atlas' | 'nova' | 'orbit' | 'company') narrows to a single
    company→project bucket when set; None (default) returns all projects (P2).
    `since`/`until` and `source` narrow by the originating document metadata through
    the compiled wiki provenance graph. They never switch answer grounding back to
    raw document chunks.
    `page_slugs` (K4b) restricts grounding to a fixed set of wiki page/claim slugs (the
    pages a KG path resolved to) while keeping every existing visibility guard
    (`_scope_filter`, `_not_blocked_company_page`, provenance filters). It only narrows
    the candidate set — ranking/grounding stays the single compiled-wiki-page path
    (불변식 5). Callers must pass a non-empty set; None means no restriction.

    Returns one `WikiSourceRef` per hit, carrying the owning page's slug/title/
    kind, the chunk content as excerpt, the cosine score, and the page's
    provenance slugs (derived_from / supports targets)."""
    embedder = get_embedding_model()
    settings = get_settings()
    with audit("wiki.retrieve") as span:
        qvec = embedder.embed([query])[0]
        span.add_meta(
            model_version=embedder.model_version,
            k=k,
            scope=scope,
            project=project,
            since=since.isoformat() if since else None,
            until=until.isoformat() if until else None,
            source=source,
        )
    distance = embeddings.c.vec.cosine_distance(qvec).label("distance")
    with session() as s:
        where = [embeddings.c.kind == "wiki_chunk", _scope_filter(user_id, scope)]
        if project is not None:
            where.append(embeddings.c.project == project)
        if page_slugs:
            # K4b graph grounding: restrict to the KG-resolved pages. Gated on truthy so
            # an empty set never silently widens to all pages (callers short-circuit first).
            where.append(wiki_pages.c.slug.in_(page_slugs))
        if since is not None or until is not None or _clean_source(source) is not None:
            matching_slugs = _matching_provenance_slugs(
                s,
                user_id,
                scope=scope,
                project=project,
                since=since,
                until=until,
                source=source,
            )
            if not matching_slugs:
                return []
            where.append(_matches_provenance_slugs(matching_slugs))
        blocked_company_slugs = _blocked_company_provenance_slugs(s)
        if blocked_company_slugs:
            where.append(_not_blocked_company_page(blocked_company_slugs))
        # Larger pools so the true page is not truncated before scoring, and a unique
        # tiebreak (embedding_id) so an equal-distance cut is deterministic across calls.
        vector_limit = max(k * 8, 40)
        lexical_limit = max(k * 40, 200)
        stmt = (
            select(
                wiki_pages.c.page_id,
                wiki_pages.c.slug,
                wiki_pages.c.title,
                wiki_pages.c.kind,
                wiki_pages.c.scope,
                wiki_chunks.c.content,
                distance,
            )
            .join(embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id)
            .join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
            .where(*where)
            .order_by(distance, embeddings.c.embedding_id)
            .limit(vector_limit)
        )
        vector_rows = [
            _candidate_from_row(r, 1.0 - float(r.distance), "vector") for r in s.execute(stmt).all()
        ]
        lexical_rows = _lexical_candidates(s, query, where, limit=lexical_limit)
        rows = _merge_candidates(vector_rows + lexical_rows, k)
        prov = _provenance_for(s, [r["page_id"] for r in rows])
    return [
        WikiSourceRef(
            page_slug=r["slug"],
            title=r["title"],
            kind=r["kind"],
            excerpt=r["content"],
            # Blend can push the internal ranking score slightly past 1.0; the exposed
            # similarity stays in [0,1] (ranking order is preserved — merge sorts on the
            # raw combined score before this clamp).
            score=min(r["score"], 1.0),
            provenance=prov.get(r["page_id"], []),
            source_scope=r["scope"],
            source_node_id=settings.node_id,
        )
        for r in rows
    ]


def _clean_source(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _candidate_from_row(row, score: float, mode: str) -> dict:
    return {
        "page_id": row.page_id,
        "slug": row.slug,
        "title": row.title,
        "kind": row.kind,
        "scope": row.scope,
        "content": row.content,
        "score": score,
        "mode": mode,
    }


def _lexical_candidates(s, query: str, where: list, *, limit: int) -> list[dict]:
    terms = _lexical_terms(query)
    if not terms:
        return []
    lexical_where = [_lexical_match_expr(terms)]
    # Deterministic, relevance-aware truncation: a query term that hits a page's slug
    # or title is a far stronger signal than a content-only hit, and slug/title matches
    # are a small bounded set. Order them ahead of content-only rows (then by slug/content
    # for a stable cut) so the truly-relevant page is never randomly truncated out of the
    # LIMIT pool — the old query had no ORDER BY, so Postgres returned an arbitrary subset
    # and ranks flickered run-to-run.
    slug_hit = or_(*[wiki_pages.c.slug.ilike(_like(t), escape="\\") for t in terms])
    title_hit = or_(*[wiki_pages.c.title.ilike(_like(t), escape="\\") for t in terms])
    priority = case((slug_hit, 0), (title_hit, 1), else_=2)
    stmt = (
        select(
            wiki_pages.c.page_id,
            wiki_pages.c.slug,
            wiki_pages.c.title,
            wiki_pages.c.kind,
            wiki_pages.c.scope,
            wiki_chunks.c.content,
        )
        .join(embeddings, wiki_chunks.c.embedding_id == embeddings.c.embedding_id)
        .join(wiki_pages, wiki_chunks.c.page_id == wiki_pages.c.page_id)
        .where(*where, *lexical_where)
        .order_by(priority, wiki_pages.c.slug, wiki_chunks.c.content)
        .limit(limit)
    )
    rows = s.execute(stmt).all()

    # Build per-query IDF map from the fetched candidate pool (no extra SQL).
    # df[t] = number of candidate rows whose slug|title|content contains term t.
    n = max(len(rows), 1)
    df: dict[str, int] = {}
    for t in terms:
        count = sum(
            1
            for r in rows
            if t in (r.slug or "").lower()
            or t in (r.title or "").lower()
            or t in (r.content or "").lower()
        )
        df[t] = count
    import math

    idf: dict[str, float] = {
        t: max(0.2, min(2.0, math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0)) for t in terms
    }

    return [
        _candidate_from_row(row, _hybrid_lexical_score(row, terms, idf), "lexical") for row in rows
    ]


def _lexical_terms(query: str) -> list[str]:
    terms: list[str] = []
    for raw in _WORD.findall(query.lower()):
        term = _normalize_term(raw)
        if len(term) < 2 or term in _LEXICAL_STOPWORDS:
            continue
        if term not in terms:
            terms.append(term)
        for synonym in _LEXICAL_SYNONYMS.get(term, ()):
            if synonym not in terms:
                terms.append(synonym)
    return terms[:12]


def _normalize_term(raw: str) -> str:
    term = raw.strip("._-/+")
    for suffix in _KOREAN_SUFFIXES:
        if len(term) > len(suffix) + 1 and term.endswith(suffix):
            return term[: -len(suffix)]
    return term


def _lexical_match_expr(terms: list[str]):
    predicates = []
    for term in terms:
        pattern = f"%{_escape_like(term)}%"
        predicates.extend(
            [
                wiki_pages.c.slug.ilike(pattern, escape="\\"),
                wiki_pages.c.title.ilike(pattern, escape="\\"),
                wiki_chunks.c.content.ilike(pattern, escape="\\"),
            ]
        )
    return or_(*predicates)


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like(term: str) -> str:
    return f"%{_escape_like(term)}%"


_PHRASE_BONUS = 0.20
_DISTINCTIVE_COVER_BONUS = 0.10
_DISTINCTIVE_IDF_THRESHOLD = 1.0
_ENTITY_IDF_THRESHOLD = 1.3
# Hex hash suffix appended to bilingual slugs (e.g. "minsu-kim-김민수" has no
# hash, but some slugs carry a short hex tail like "slug-abc123").
_SLUG_HASH_RE = re.compile(r"-[0-9a-f]{4,}$")


def _slug_bare(slug: str) -> str:
    """Strip a trailing hex-hash segment from a slug, return lowercased bare form."""
    return _SLUG_HASH_RE.sub("", slug.lower())


def _hybrid_lexical_score(row, terms: list[str], idf: dict[str, float] | None = None) -> float:
    slug = (row.slug or "").lower()
    title = (row.title or "").lower()
    content = (row.content or "").lower()
    # When idf is not provided (e.g. unit tests calling the old signature), fall
    # back to uniform weight 1.0 so existing behaviour is preserved.
    idf_w = idf if idf is not None else {t: 1.0 for t in terms}
    weighted_matches = 0.0
    matched_idf_sum = 0.0
    total_idf_sum = sum(idf_w.get(t, 1.0) for t in terms)
    for term in terms:
        w = idf_w.get(term, 1.0)
        matched = False
        if term in slug:
            weighted_matches += 3.0 * w
            matched = True
        if term in title:
            weighted_matches += 3.0 * w
            matched = True
        if term in content:
            weighted_matches += 1.0 * w
            matched = True
        if matched:
            matched_idf_sum += w
    # Normalise by the maximum possible score under current IDF weights.
    max_score = max(total_idf_sum * 4.0, 1.0)
    lexical = min(weighted_matches / max_score, 1.0)
    # IDF-weighted coverage: rare matched terms count more than common ones.
    coverage = matched_idf_sum / max(total_idf_sum, 1.0)
    base = 0.45 + (0.25 * lexical) + (0.30 * coverage)

    bonus = 0.0

    # --- Change A: phrase / contiguous-span match boost ---
    # Build all contiguous sub-sequences of >=2 consecutive matched query terms
    # (as slug-style joined strings) and check if any appear in slug or title.
    matched_terms = [t for t in terms if t in slug or t in title or t in content]
    slug_title = slug + " " + title
    phrase_hit = False

    # Check contiguous spans of consecutive matched terms (by original order).
    matched_positions = [i for i, t in enumerate(terms) if t in matched_terms]
    for span_len in range(len(matched_positions), 1, -1):
        for start in range(len(matched_positions) - span_len + 1):
            span_indices = matched_positions[start : start + span_len]
            # Must be consecutive positions in the original terms list.
            if span_indices == list(range(span_indices[0], span_indices[0] + span_len)):
                phrase = "-".join(terms[i] for i in span_indices)
                if phrase in slug or phrase in title:
                    phrase_hit = True
                    break
        if phrase_hit:
            break

    if phrase_hit:
        bonus += _PHRASE_BONUS

    # --- Change B: entity/short-title anchor ---
    # A page whose entire title (or bare slug) equals a single rare query term
    # is a near-certain entity match — route it through the phrase-bonus path.
    if not phrase_hit:
        slug_bare = _slug_bare(slug)
        for term in terms:
            w = idf_w.get(term, 1.0)
            if w >= _ENTITY_IDF_THRESHOLD:
                if title == term or slug_bare == term:
                    bonus += _PHRASE_BONUS
                    phrase_hit = True  # noqa: F841 (used as sentinel above)
                    break

    # Distinctive-coverage bonus: all high-idf terms appear in slug+title combined.
    if not phrase_hit:
        distinctive = [t for t in terms if idf_w.get(t, 1.0) >= _DISTINCTIVE_IDF_THRESHOLD]
        if distinctive and all(t in slug_title for t in distinctive):
            bonus += _DISTINCTIVE_COVER_BONUS

    return min(base + bonus, 1.0)


_BLEND_BONUS = 0.15


def _merge_candidates(candidates: list[dict], k: int) -> list[dict]:
    # Collect best vector and best lexical score per page_id separately.
    best_vector: dict[UUID, dict] = {}
    best_lexical: dict[UUID, dict] = {}
    for c in candidates:
        key = c["page_id"]
        mode = c["mode"]
        if mode == "vector":
            if key not in best_vector or c["score"] > best_vector[key]["score"]:
                best_vector[key] = c
        else:
            if key not in best_lexical or c["score"] > best_lexical[key]["score"]:
                best_lexical[key] = c

    all_keys = set(best_vector) | set(best_lexical)
    merged: list[dict] = []
    for key in all_keys:
        vec_row = best_vector.get(key)
        lex_row = best_lexical.get(key)
        vec = vec_row["score"] if vec_row is not None else 0.0
        lex = lex_row["score"] if lex_row is not None else 0.0
        # Normalise the lexical score to [0,1] by stripping the 0.45 floor, then add a
        # co-evidence bonus a lexical-only sibling (vec≈0) cannot earn.
        lex_norm = (lex - 0.45) / 0.55 if lex > 0.0 else 0.0
        base = max(vec, lex)
        combined = base + _BLEND_BONUS * min(vec, lex_norm)
        # Use metadata from the higher raw-score arm for excerpt quality.
        row = (
            (vec_row if vec >= lex else lex_row) if (vec_row and lex_row) else (vec_row or lex_row)
        )
        result = dict(row)
        result["score"] = combined
        merged.append(result)

    return sorted(
        merged,
        key=lambda r: (-round(r["score"], 3), -_kind_priority(r["kind"]), r["slug"]),
    )[:k]


def _kind_priority(kind: str) -> int:
    if kind == "page":
        return 2
    if kind == "claim":
        return 1
    return 0


def _document_scope_filter(user_id: UUID, scope: str):
    company = documents.c.scope == "company"
    own_personal = (documents.c.scope == "personal") & (documents.c.user_id == user_id)
    if scope == "company":
        return company
    if scope == "personal":
        return own_personal
    return or_(company, own_personal)


def _matching_provenance_slugs(
    s,
    user_id: UUID,
    *,
    scope: str,
    project: str | None,
    since: datetime | None,
    until: datetime | None,
    source: str | None,
) -> set[str]:
    """Source/claim/page slugs reachable from documents matching metadata filters."""
    doc_time = func.coalesce(
        documents.c.source_last_edited_at, documents.c.updated_at, documents.c.created_at
    )
    where = [_document_scope_filter(user_id, scope)]
    if project is not None:
        where.append(documents.c.project == project)
    cleaned_source = _clean_source(source)
    if cleaned_source is not None:
        where.append(documents.c.source == cleaned_source)
    if since is not None:
        where.append(doc_time >= since)
    if until is not None:
        where.append(doc_time < until)

    rows = s.execute(select(documents.c.title, documents.c.doc_id).where(*where)).all()
    source_slugs = {source_slug_from_title(title, doc_id) for title, doc_id in rows}
    if not source_slugs:
        return set()
    return _expand_visible_provenance_slugs(s, user_id, source_slugs, scope=scope)


def _matches_provenance_slugs(slugs: set[str]):
    """Filter retrieved wiki page/claim chunks to a provenance-reachable slug set."""
    linked = exists(
        select(1).where(
            wiki_links.c.src_page_id == wiki_pages.c.page_id,
            wiki_links.c.rel.in_(_PROVENANCE_RELS),
            wiki_links.c.dst_slug.in_(slugs),
        )
    )
    return or_(wiki_pages.c.slug.in_(slugs), linked)


def _provenance_for(s, page_ids: list[UUID]) -> dict[UUID, list[str]]:
    """Map each page_id to its provenance dst_slugs (derived_from / supports)."""
    if not page_ids:
        return {}
    rows = s.execute(
        select(wiki_links.c.src_page_id, wiki_links.c.dst_slug).where(
            wiki_links.c.src_page_id.in_(page_ids),
            wiki_links.c.rel.in_(_PROVENANCE_RELS),
        )
    ).all()
    out: dict[UUID, list[str]] = {}
    for src_page_id, dst_slug in rows:
        bucket = out.setdefault(src_page_id, [])
        if dst_slug not in bucket:
            bucket.append(dst_slug)
    return out


def _not_blocked_company_page(blocked_slugs: set[str]):
    """SQL predicate that hides company pages derived from invalid company sources.

    Stale production data can contain company-scoped wiki pages that were authored
    from personal-only connectors before node policy hardening. Scope alone cannot
    distinguish those rows, so retrieval also follows the compiled wiki provenance
    graph and excludes affected company source/claim/page slugs.
    """
    blocked_link = exists(
        select(1).where(
            wiki_links.c.src_page_id == wiki_pages.c.page_id,
            wiki_links.c.rel.in_(_PROVENANCE_RELS),
            wiki_links.c.dst_slug.in_(blocked_slugs),
        )
    )
    return or_(
        wiki_pages.c.scope != "company",
        and_(wiki_pages.c.slug.not_in(blocked_slugs), ~blocked_link),
    )


def _blocked_company_provenance_slugs(s) -> set[str]:
    """Return source/claim/page slugs from unsupported company connector imports."""
    source_slugs = _unsupported_company_source_slugs(s)
    if not source_slugs:
        return set()
    return _expand_provenance_slugs(s, source_slugs)


def _unsupported_company_source_slugs(s) -> set[str]:
    supported = _supported_company_connector_slugs()
    rows = s.execute(
        select(documents.c.title, documents.c.doc_id)
        .select_from(
            documents.outerjoin(
                connector_accounts,
                documents.c.source_account_id == connector_accounts.c.account_id,
            )
        )
        .where(
            documents.c.scope == "company",
            documents.c.source_account_id.is_not(None),
            or_(
                connector_accounts.c.account_id.is_(None),
                connector_accounts.c.account_kind != "company",
                connector_accounts.c.connector_slug.not_in(supported),
            ),
        )
    ).all()
    return {source_slug_from_title(title, doc_id) for title, doc_id in rows}


def _supported_company_connector_slugs() -> set[str]:
    from orthus.connectors.registry import (
        list_connector_manifests,
        register_default_connector_providers,
        registered_connector_slugs,
    )

    if not registered_connector_slugs():
        register_default_connector_providers()
    return {
        manifest.slug
        for manifest in list_connector_manifests()
        if manifest.supports_account_kind("company")
    }


def _expand_provenance_slugs(s, source_slugs: set[str]) -> set[str]:
    blocked = set(source_slugs)
    frontier = set(source_slugs)
    # claim/page -> source, then page -> claim. This matches store.write_* links.
    # Do not add source pages discovered through a contaminated consolidated page:
    # source slugs are already seeded from unsupported documents, and expanding
    # back into every source related to a broad page would hide clean promoted
    # company sources that share a generic page slug.
    for _ in range(2):
        rows = s.execute(
            select(wiki_pages.c.slug)
            .select_from(
                wiki_pages.join(wiki_links, wiki_pages.c.page_id == wiki_links.c.src_page_id)
            )
            .where(
                wiki_pages.c.scope == "company",
                wiki_pages.c.kind != "source",
                wiki_links.c.rel.in_(_PROVENANCE_RELS),
                wiki_links.c.dst_slug.in_(frontier),
            )
        ).scalars()
        new = set(rows) - blocked
        if not new:
            break
        blocked.update(new)
        frontier = new
    return blocked


def _expand_visible_provenance_slugs(
    s, user_id: UUID, source_slugs: set[str], *, scope: str
) -> set[str]:
    """Expand source slugs to visible claim/page slugs through direct provenance."""
    expanded = set(source_slugs)
    frontier = set(source_slugs)
    for _ in range(2):
        rows = s.execute(
            select(wiki_pages.c.slug)
            .select_from(
                wiki_pages.join(wiki_links, wiki_pages.c.page_id == wiki_links.c.src_page_id)
            )
            .where(
                _wiki_page_scope_filter(user_id, scope),
                wiki_links.c.rel.in_(_PROVENANCE_RELS),
                wiki_links.c.dst_slug.in_(frontier),
            )
        ).scalars()
        new = set(rows) - expanded
        if not new:
            break
        expanded.update(new)
        frontier = new
    return expanded


def _wiki_page_scope_filter(user_id: UUID, scope: str):
    company = wiki_pages.c.scope == "company"
    own_personal = (wiki_pages.c.scope == "personal") & (wiki_pages.c.owner_id == user_id)
    if scope == "company":
        return company
    if scope == "personal":
        return own_personal
    return or_(company, own_personal)
