"""Phase 3-A semantic answer cache (MA.7a) — docs/company-agent-orchestration.md §P3A.

company-scope, single-mode, grounded `/ask` answers are cached and replayed when the
same (normalized) question recurs AND the underlying company wiki knowledge is unchanged.

MA.7b adds an embedding-similarity fallback behind a *separate* sub-flag
(ORTHUS_ASK_CACHE_SEMANTIC_MATCH_ENABLED, default false): when the exact normalized key
misses, the incoming question is embedded and matched against the partition's stored
question embeddings by cosine similarity ≥ a fixed threshold τ. This absorbs paraphrases
("지난주 매출?" vs "지난주 매출 얼마야?") that the exact key cannot. With the sub-flag off the
store writes no embedding and the lookup runs no semantic query — exact-only, byte-identical
to MA.7a.

Invariants enforced here (§P3A.12):
- 22: company-scope only. personal/owner-scope/federated answers are NEVER stored — the
  caller (router.answer) only invokes store/lookup when scope=="company" and not federated.
- 23: the cache key is the full partition tuple (owner_id sentinel + scope + project +
  federation + node_id) + watermark + question_key. A missing partition field can't build a key.
  The MA.7b semantic query carries the SAME partition tuple, so a paraphrase never matches
  across owner/project/node/federation (cross-partition isolation).
- 24: a watermark mismatch (or expired TTL) is a MISS — stale answers never replay (the
  semantic query filters watermark==current and within TTL too). The cache is NOT a grounding
  bypass: it replays an already-compiled-wiki-grounded RoutedAnswer (불변식 2).
- 25: hit/miss is fully deterministic. Exact = normalized match, no LLM. MA.7b semantic is a
  fixed-threshold (τ, a constant — not LLM-judged) cosine match on a deterministic embedding.
- 26: only single-mode + grounded + non-error + non-gap answers are cacheable.
- 27: off behind ORTHUS_ASK_SEMANTIC_CACHE_ENABLED — router.answer skips both hooks entirely.

PII (§P3A.7): the semantic embedding is computed from `redact_pii_text(normalize_question(q))`
and that same redacted text is what lands in `question_redacted` — raw question PII never
reaches the row (the exact `question_key` is already a non-reversible sha256).

watermark (B′, §P3A.3.2): (row_count, MAX(updated_at)) over the company `wiki_pages`
partition. A modification advances MAX(updated_at); a deletion drops the count; equality
comparison invalidates on either (so "MAX 후퇴" from deleting the newest row is still caught
by the count drop). Encapsulated in `knowledge_watermark()` so a future swap to a monotonic
generation counter (안 C) leaves callers unchanged.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import Connection, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.audit import audit
from orthus.audit.redact import redact_pii_text
from orthus.db import session
from orthus.models.registry import get_embedding_model
from orthus.schemas.canonical import RoutedAnswer
from orthus.settings import get_settings
from orthus.tables import ask_cache, wiki_pages
from orthus.wiki.gap import normalize_question

__all__ = [
    "question_key",
    "knowledge_watermark",
    "is_cacheable_result",
    "cache_lookup",
    "cache_store",
    "gc",
]

# company answers carry no owner; a fixed sentinel keeps the partition tuple total (개인
# 답은 애초에 store되지 않으므로 실제 owner UUID는 키에 들어올 수 없다).
_COMPANY_OWNER_SENTINEL = "company"
_ALL_PROJECTS = "__all__"


def question_key(question: str) -> str:
    """sha256 of the normalized question — the cache never stores plaintext (PII, §P3A.7).

    Reuses `wiki.gap.normalize_question` so the cache key and the data-gap dedup key agree
    on what 'the same question' is (one normalizer, no drift). Deterministic, no LLM (불변식 25).
    """
    return hashlib.sha256(normalize_question(question).encode("utf-8")).hexdigest()


def _semantic_text(question: str) -> str:
    """The text MA.7b embeds and stores: redacted + normalized (§P3A.7, 불변식 6).

    Redaction strips PII before it can reach `question_redacted` or the embedding input;
    normalization (same `normalize_question` as the exact key) keeps the two layers agreeing
    on cosmetic differences so they don't disagree on what 'the same question' is.
    """
    return redact_pii_text(normalize_question(question))


def _embed_text(redacted: str) -> list[float]:
    """Embed already-redacted text inside an `audit()` span (AGENTS.md 코드 컨벤션 — 외부
    임베딩 호출은 audit span으로 감싼다; 원칙 3). The single embed shape shared by store and
    lookup so their vectors stay comparable (no divergence). Deterministic, no LLM (불변식 25)."""
    embedder = get_embedding_model()
    with audit("ask.cache.embed") as span:
        vec = embedder.embed([redacted])[0]
        span.add_meta(model_version=embedder.model_version)
    return vec


def _embed_question(question: str) -> list[float]:
    """Embed a raw question for the semantic match (redact+normalize, then `_embed_text`)."""
    return _embed_text(_semantic_text(question))


# How many nearest neighbors the semantic lookup RANKS (id+distance only): the closest
# valid+in-threshold row wins, but a corrupt (un-decodable) nearest row must not poison the
# whole lookup, so we rank a few and load the full answer_json only for the row we return.
_SEMANTIC_CANDIDATES = 5


def _project_key(project: str | None) -> str:
    return project if project else _ALL_PROJECTS


def _watermark_in_conn(conn: Connection, scope: str, project: str | None) -> str:
    """(row_count, MAX(updated_at)) over the company `wiki_pages` partition.

    company pages have owner_id NULL, so personal pages never shift the company
    watermark (partition isolation). project=None → all company projects.
    """
    stmt = select(func.count(), func.max(wiki_pages.c.updated_at)).where(
        wiki_pages.c.scope == scope,
        wiki_pages.c.owner_id.is_(None),
    )
    if project:
        stmt = stmt.where(wiki_pages.c.project == project)
    count, max_updated = conn.execute(stmt).one()
    stamp = max_updated.isoformat() if max_updated is not None else "0"
    return f"{count}:{stamp}"


def knowledge_watermark(scope: str, project: str | None) -> str:
    """Public watermark accessor (opens its own connection). See `_watermark_in_conn`."""
    with session() as s:
        return _watermark_in_conn(s.connection(), scope, project)


def is_cacheable_result(routed: RoutedAnswer) -> bool:
    """Cacheable iff a pure wiki-grounded company answer (§P3A.2 #5).

    Only `mode=="wiki"` is covered by the `wiki_pages` watermark, so ONLY wiki answers are
    cached. Deliberately excluded:
    - structured (mode=="structured"): grounds on `notion_rows`, which the wiki_pages
      watermark does not track → would serve stale aggregates.
    - graph-success (mode=="graph"): grounds on the KG (Neo4j), not covered by the watermark.
    - a transient graph→wiki demote during a KG outage also has mode=="wiki"; that variant is
      excluded by the caller (router.answer) which owns the degraded-KG warning tokens.
    gap / "근거 없음" / empty body → not a real grounded answer. agent_work / decomposed → never.
    """
    if routed.mode != "wiki" or routed.wiki is None:
        return False
    body = routed.wiki.answer or ""
    if not body or "근거 없음" in body:
        return False
    return routed.wiki.gap is None


def cache_lookup(question: str, *, scope: str, project: str | None) -> RoutedAnswer | None:
    """Return a cached RoutedAnswer iff a row matches the partition + question_key AND its
    watermark == current AND it is within TTL. Otherwise None (MISS).

    Exact normalized match (MA.7a) is tried first — cheap, deterministic, no embedding call.
    Only on an exact miss AND with the MA.7b sub-flag on does the semantic fallback embed the
    question and look for a paraphrase within the partition (§P3A.4).

    Selects the (indexed) exact row FIRST and only computes the `wiki_pages` watermark
    aggregate when a row exists or the semantic fallback will run — so the dominant cold-miss
    path (never-asked question, sub-flag off) pays no partition scan."""
    if scope != "company":  # 불변식 22 defense-in-depth: the cache is company-scope only
        return None
    settings = get_settings()
    qk = question_key(question)
    with session() as s:
        conn = s.connection()
        row = conn.execute(
            select(
                ask_cache.c.watermark,
                ask_cache.c.answer_json,
                ask_cache.c.created_at,
            ).where(
                ask_cache.c.owner_id == _COMPANY_OWNER_SENTINEL,
                ask_cache.c.scope == scope,
                ask_cache.c.project == _project_key(project),
                ask_cache.c.federation.is_(False),
                ask_cache.c.node_id == settings.node_id,
                ask_cache.c.question_key == qk,
            )
        ).first()
        semantic_on = settings.ask_cache_semantic_match_enabled
        # Watermark (안 B′, §P3A.3.2) is computed lazily and at most once: the cold-miss /
        # exact-only path (row is None, sub-flag off) never touches it. When the sub-flag is
        # on every lookup needs it (the semantic query filters watermark==current) — that
        # per-request index-only aggregate is the known 안 B′ cost; 안 C (generation counter)
        # would remove it and stays localized to `knowledge_watermark`.
        wm_cache: dict[str, str] = {}

        def current_wm() -> str:
            if "v" not in wm_cache:
                wm_cache["v"] = _watermark_in_conn(conn, scope, project)
            return wm_cache["v"]

        exact_reason = "miss"
        if row is not None:
            reason, answer = _exact_verdict(row, current_wm(), settings)
            if answer is not None:
                _audit_cache("hit", scope, project)
                return answer
            exact_reason = reason  # unusable exact row → fall through to semantic, if enabled
        if semantic_on:
            answer, semantic_reason = _semantic_lookup(
                conn, question, current_wm(), settings, scope, project
            )
            if answer is not None:
                _audit_cache("hit_semantic", scope, project)
                return answer
            # semantic ran but missed → report the semantic outcome, keeping the exact-stage
            # reason as meta so exact-stage vs semantic-stage misses stay distinguishable.
            _audit_cache(semantic_reason, scope, project, exact=exact_reason)
            return None
    _audit_cache(exact_reason, scope, project)
    return None


def _exact_verdict(row, current_wm: str, settings) -> tuple[str, RoutedAnswer | None]:
    """Validate an exact-key row against watermark/TTL/decode. Returns (miss-reason, None)
    when unusable so the caller can fall through to the semantic fallback, or ("", answer)
    on a usable hit."""
    if row.watermark != current_wm:
        return "miss_stale", None
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if datetime.now(UTC) - created > timedelta(seconds=settings.ask_cache_ttl_sec):
        return "miss_ttl", None
    try:
        return "", RoutedAnswer.model_validate(row.answer_json)
    except ValidationError:
        # schema drift / corrupt row → treat as miss (router recomputes + re-stores)
        return "miss_decode", None


def _semantic_lookup(
    conn: Connection, question: str, current_wm: str, settings, scope: str, project: str | None
) -> tuple[RoutedAnswer | None, str]:
    """MA.7b paraphrase match. Returns (answer, audit-reason): the closest stored embedding
    within the SAME partition whose watermark is current, is within TTL, and is within cosine
    distance ≤ (1 − τ).

    The ranking is EXACT, not approximate: there is no ivfflat index on `question_embedding`
    (dropped in migration 0078 precisely so this query can't become approximate). The partition
    predicate is selective, so Postgres filters via the partition btree (`uq_ask_cache_key`
    prefix) and distance-sorts that partition's bounded (MA.7c GC + TTL) row set exactly. An
    approximate index could skip a true in-threshold neighbor (false MISS) and make hit/miss
    depend on index state, breaking 불변식 25; the exact scan keeps it deterministic. We inspect
    the few nearest rows (not just the closest) so a single corrupt/undecodable nearest row
    can't poison a legitimate farther in-threshold hit, and we load the full `answer_json` ONLY
    for the row we actually return (the ranking query carries just id+distance). The explicit
    `distance > max_dist` post-filter bounds a hit to within τ. τ is a fixed constant.

    Cold-path guard: when the partition holds no live embedded row, skip the embedding-provider
    round-trip entirely — the dominant never-asked /ask path must not pay an embed call. An
    embedding failure also degrades to a normal miss (the cache must never fail the request)."""
    ttl_cutoff = datetime.now(UTC) - timedelta(seconds=settings.ask_cache_ttl_sec)
    partition = (
        ask_cache.c.owner_id == _COMPANY_OWNER_SENTINEL,
        ask_cache.c.scope == scope,
        ask_cache.c.project == _project_key(project),
        ask_cache.c.federation.is_(False),
        ask_cache.c.node_id == settings.node_id,
        ask_cache.c.question_embedding.isnot(None),
        ask_cache.c.watermark == current_wm,
        ask_cache.c.created_at > ttl_cutoff,
    )
    # Cheap btree existence check BEFORE embedding: no live embedded candidate → don't call the
    # embedding provider at all (skips the slowest op on the dominant cold-miss path).
    if conn.execute(select(ask_cache.c.id).where(*partition).limit(1)).first() is None:
        return None, "miss_semantic"
    try:
        qvec = _embed_question(question)
    except Exception:  # noqa: BLE001 — best-effort cache: degrade to miss, never fail /ask
        return None, "miss_embed_error"
    max_dist = 1.0 - settings.ask_cache_similarity_threshold
    distance = ask_cache.c.question_embedding.cosine_distance(qvec).label("distance")
    candidates = conn.execute(
        select(ask_cache.c.id, distance)
        .where(*partition)
        .order_by(distance)
        .limit(_SEMANTIC_CANDIDATES)
    ).all()
    saw_decode_error = False
    for cand in candidates:
        if cand.distance > max_dist:  # ordered ascending → no closer candidate remains
            break
        hit = conn.execute(select(ask_cache.c.answer_json).where(ask_cache.c.id == cand.id)).first()
        if hit is None:  # row evicted between the ranking read and this fetch → skip
            continue
        try:
            return RoutedAnswer.model_validate(hit.answer_json), ""
        except ValidationError:
            saw_decode_error = True  # skip this corrupt row, try the next in-threshold one
    return None, ("miss_decode_semantic" if saw_decode_error else "miss_semantic")


def cache_store(
    question: str,
    routed: RoutedAnswer,
    *,
    scope: str,
    project: str | None,
    watermark: str,
) -> None:
    """Upsert a grounded company answer keyed by the partition tuple + question_key. The
    caller MUST have verified `is_cacheable_result(routed)`.

    `watermark` is the knowledge version to STAMP the row with, and is REQUIRED: the caller
    must snapshot it (via `knowledge_watermark`) BEFORE grounding so a wiki edit that lands
    during the answer-compute window cannot over-stamp the row with a watermark newer than the
    knowledge it was grounded on (which would make the stale answer HIT, defeating 불변식 24).
    There is deliberately no store-time-read fallback — that path WAS the F1 TOCTOU, so a caller
    cannot silently reintroduce it by omitting the argument. Stamping the pre-grounding watermark
    only ever errs toward a future MISS (conservative recompute), never a stale HIT.
    """
    if scope != "company":  # 불변식 22 defense-in-depth: never store a non-company answer
        raise ValueError("ask_cache stores company-scope answers only")
    settings = get_settings()
    qk = question_key(question)
    now = datetime.now(UTC)
    # Drop the per-request stream_id so a cache hit never replays a prior request's SSE
    # channel id to a new caller (the live feedback stream is request-scoped).
    answer_json = routed.model_copy(update={"stream_id": None}).model_dump(mode="json")
    # MA.7b: compute the embedding + redacted text ONLY when the semantic sub-flag is on, so
    # exact-only mode does zero embedding work. Best-effort: a transient embedding-provider
    # failure must not abort the store of an already-produced answer — fall back to an
    # exact-only row (NULL embedding), which still serves the exact-match path.
    embedding: list[float] | None = None
    redacted: str | None = None
    if settings.ask_cache_semantic_match_enabled:
        try:
            redacted = _semantic_text(question)
            embedding = _embed_text(redacted)
        except Exception:  # noqa: BLE001 — degrade to an exact-only row, never fail the answer
            embedding = None
            redacted = None
    with session() as s:
        conn = s.connection()
        wm = watermark  # caller's pre-grounding snapshot (F1: no store-time over-stamp)
        stmt = pg_insert(ask_cache).values(
            id=uuid4(),
            owner_id=_COMPANY_OWNER_SENTINEL,
            scope=scope,
            project=_project_key(project),
            federation=False,
            node_id=settings.node_id,
            question_key=qk,
            question_embedding=embedding,
            question_redacted=redacted,
            watermark=wm,
            answer_json=answer_json,
            created_at=now,
        )
        # On conflict always refresh the answer/watermark/timestamp. Only OVERWRITE the
        # embedding columns when we actually computed one — so re-storing with the sub-flag off
        # (or after an embedding failure) PRESERVES a previously-good embedding rather than
        # wiping it to NULL and silently degrading semantic recall after a flag toggle.
        update_set = {
            "watermark": stmt.excluded.watermark,
            "answer_json": stmt.excluded.answer_json,
            "created_at": stmt.excluded.created_at,
        }
        if embedding is not None:
            update_set["question_embedding"] = stmt.excluded.question_embedding
            update_set["question_redacted"] = stmt.excluded.question_redacted
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "owner_id",
                "scope",
                "project",
                "federation",
                "node_id",
                "question_key",
            ],
            set_=update_set,
        )
        conn.execute(stmt)
        s.commit()
    _audit_cache("store", scope, project)


def _audit_cache(result: str, scope: str, project: str | None, **extra: str) -> None:
    """`ask.cache` span: result + partition (+ optional stage detail) only — no question text
    / answer body (§P3A.7/§P3A.9). `extra` carries e.g. `exact=miss_stale` to distinguish an
    exact-stage miss from a semantic-stage miss without leaking content."""
    with audit("ask.cache") as span:
        span.add_meta(result=result, scope=scope, project=project or _ALL_PROJECTS, **extra)


def gc(*, ttl_sec: int | None = None) -> dict[str, int]:
    """Delete expired (TTL backstop) + watermark-stale ask_cache rows (MA.7c, §P3A.10).

    The lazy on-read invalidation path (`cache_lookup`) never DELETEs — a stale or expired row
    is only overwritten when the SAME question recurs (the `cache_store` upsert). Questions that
    never recur accumulate forever, and every company wiki edit advances the partition watermark,
    orphaning ALL prior rows in that partition. This sweep bounds the table by deleting:

      1. TTL-expired rows (`created_at + TTL < now`) — watermark-independent, safe for any row.
      2. watermark-stale rows (`watermark != current partition watermark`) — the dominant
         accumulation source. Computed per company partition on THIS node only: store/lookup key
         on `node_id` + the company owner sentinel, so only those rows exist here and only those
         have a locally-computable wiki_pages watermark.

    A live row (current watermark AND within TTL) is never deleted, so a lookup that would HIT
    before GC still HITs after (수용 게이트: GC가 정상 hit 무영향). Deterministic — no LLM, no
    embedding call. Runs regardless of `ORTHUS_ASK_SEMANTIC_CACHE_ENABLED` (clearing dead rows is
    harmless when the cache is off); the lookup/store hooks stay flag-gated as before.

    Returns counts: `{deleted_ttl, deleted_stale, remaining}`.
    """
    settings = get_settings()
    ttl = settings.ask_cache_ttl_sec if ttl_sec is None else ttl_sec
    ttl_cutoff = datetime.now(UTC) - timedelta(seconds=ttl)
    with session() as s:
        conn = s.connection()
        # 1) TTL backstop — watermark-independent, applies to every row (incl. any foreign-node
        #    rows that have no locally-computable watermark).
        deleted_ttl = (
            conn.execute(delete(ask_cache).where(ask_cache.c.created_at < ttl_cutoff)).rowcount or 0
        )
        # 2) watermark-stale — per company partition on this node, drop rows whose stored
        #    watermark != the partition's current knowledge version. Distinct project keys keep
        #    the per-partition aggregate to one call per project actually present.
        company_partition = (
            ask_cache.c.owner_id == _COMPANY_OWNER_SENTINEL,
            ask_cache.c.scope == "company",
            ask_cache.c.federation.is_(False),
            ask_cache.c.node_id == settings.node_id,
        )
        project_keys = (
            conn.execute(select(ask_cache.c.project).where(*company_partition).distinct())
            .scalars()
            .all()
        )
        deleted_stale = 0
        for project_key in project_keys:
            project = None if project_key == _ALL_PROJECTS else project_key
            current_wm = _watermark_in_conn(conn, "company", project)
            deleted_stale += (
                conn.execute(
                    delete(ask_cache).where(
                        *company_partition,
                        ask_cache.c.project == project_key,
                        ask_cache.c.watermark != current_wm,
                    )
                ).rowcount
                or 0
            )
        remaining = conn.execute(select(func.count()).select_from(ask_cache)).scalar() or 0
        s.commit()
    with audit("ask.cache.gc") as span:
        span.add_meta(deleted_ttl=deleted_ttl, deleted_stale=deleted_stale, remaining=remaining)
    return {"deleted_ttl": deleted_ttl, "deleted_stale": deleted_stale, "remaining": remaining}


def main(argv: list[str] | None = None) -> int:
    """CLI: `python -m orthus.router.cache gc` (operator/launchd run, §P3A.10 MA.7c).

    Mirrors the `python -m orthus.kg.outbox trim` precedent — a standalone sweep with no runtime
    coupling. Runs regardless of the cache flag (clearing dead rows is always safe)."""
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "gc"
    if cmd != "gc":
        print(f"unknown command: {cmd} (usage: python -m orthus.router.cache gc)", file=sys.stderr)
        return 2
    report = gc()
    print(
        f"ask_cache gc: deleted_ttl={report['deleted_ttl']} "
        f"deleted_stale={report['deleted_stale']} remaining={report['remaining']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
