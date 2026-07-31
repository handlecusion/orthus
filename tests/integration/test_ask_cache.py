"""Phase 3-A semantic answer cache (MA.7a) — docs/company-agent-orchestration.md §P3A.

Covers the acceptance gates in §P3A.11 / invariants §P3A.12:
- flag off → no cache touched (불변식 27)
- store gate: company + grounded + single-mode only; personal/all/token/decomposed/gap/
  non-grounded never stored (불변식 22/26)
- watermark invalidation + TTL backstop (불변식 24)
- partition isolation (project/node_id) (불변식 23)
- deterministic question_key normalization + is_cacheable_result (불변식 25)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update

import orthus.router as router
import orthus.router.cache as cache_mod
from orthus.db import session
from orthus.models.adapters.mock import MockEmbedding
from orthus.router.cache import (
    _embed_question,
    cache_lookup,
    cache_store,
    is_cacheable_result,
    knowledge_watermark,
    question_key,
)
from orthus.schemas.canonical import (
    AssistantResult,
    ChatTurn,
    RoutedAnswer,
    WikiAnswer,
    WikiGap,
)
from orthus.settings import get_settings
from orthus.tables import ask_cache, audit_log, wiki_pages


# --- helpers -------------------------------------------------------------------


def _wiki(
    q: str, body: str = "회사 매출은 1.2억입니다.", gap: WikiGap | None = None
) -> RoutedAnswer:
    return RoutedAnswer(
        question=q,
        mode="wiki",
        wiki=WikiAnswer(question=q, answer=body, sources=[], gap=gap),
    )


def _structured(status: str) -> RoutedAnswer:
    # model_construct: AssistantResult's full ValidationResult is irrelevant to the
    # cacheability check (it only reads .status), so skip nested validation here.
    res = AssistantResult.model_construct(question="q", status=status)
    return RoutedAnswer(question="q", mode="structured", structured=res)


def _insert_company_page(project: str = "atlas") -> None:
    with session() as s:
        s.execute(
            insert(wiki_pages).values(
                page_id=uuid4(),
                slug=f"p-{uuid4().hex[:8]}",
                kind="page",
                path="company/wiki/p.md",
                title="p",
                content_hash=uuid4().hex,
                schema_version=1,
                scope="company",
                project=project,
                owner_id=None,
            )
        )
        s.commit()


def _store(q: str, routed: RoutedAnswer | None = None, *, scope: str = "company", project=None):
    """cache_store with the current watermark filled in. cache_store requires an explicit
    watermark (no store-time-read fallback — that path was the F1 TOCTOU); a direct test has no
    answer-compute window, so the live watermark is the correct stamp — exactly what router.answer
    snapshots before grounding."""
    cache_store(
        q,
        routed if routed is not None else _wiki(q),
        scope=scope,
        project=project,
        watermark=knowledge_watermark(scope, project),
    )


# --- pure functions (no DB writes) ---------------------------------------------


def test_question_key_normalizes_whitespace_and_case():
    assert question_key("지난주 매출?") == question_key("  지난주   매출?  ")
    assert question_key("ABC def") == question_key("abc  DEF")
    assert question_key("질문 A") != question_key("질문 B")


def test_is_cacheable_result_gate():
    # wiki grounded → cacheable
    assert is_cacheable_result(_wiki("q")) is True
    # wiki "근거 없음" / empty body → not cacheable
    assert is_cacheable_result(_wiki("q", body="근거 없음 — 데이터가 부족합니다.")) is False
    assert is_cacheable_result(_wiki("q", body="")) is False
    # wiki with a gap → not cacheable (틀린 '모름'을 굳히지 않음)
    gap = WikiGap(detected=True, reason="insufficient_grounding", missing_topic="q", message="add")
    assert is_cacheable_result(_wiki("q", gap=gap)) is False
    # structured → NEVER cacheable: notion_rows substrate is not covered by the wiki_pages
    # watermark (would serve stale aggregates). Both executed and rejected are excluded.
    assert is_cacheable_result(_structured("executed")) is False
    assert is_cacheable_result(_structured("rejected")) is False
    # graph-success (mode 'graph') → not cacheable (KG substrate not watermark-covered)
    assert (
        is_cacheable_result(RoutedAnswer(question="q", mode="graph", wiki=_wiki("q").wiki)) is False
    )
    # decomposed / agent_work → never cacheable
    assert is_cacheable_result(RoutedAnswer(question="q", mode="decomposed")) is False
    assert is_cacheable_result(RoutedAnswer(question="q", mode="agent_work")) is False


# --- watermark (B′) ------------------------------------------------------------


def test_watermark_changes_on_page_insert(clean):
    wm0 = knowledge_watermark("company", "atlas")
    _insert_company_page("atlas")
    wm1 = knowledge_watermark("company", "atlas")
    assert wm0 != wm1  # count + max(updated_at) both move


def test_watermark_partition_isolated_by_project(clean):
    base = knowledge_watermark("company", "nova")
    _insert_company_page("atlas")  # different project
    assert knowledge_watermark("company", "nova") == base  # unaffected


# --- store / lookup round-trip -------------------------------------------------


def test_store_then_lookup_hits(clean):
    q = "회사 작년 매출은?"
    _store(q, _wiki(q), scope="company", project="atlas")
    hit = cache_lookup(q, scope="company", project="atlas")
    assert hit is not None
    assert hit.mode == "wiki"
    assert hit.wiki is not None and hit.wiki.answer == "회사 매출은 1.2억입니다."
    # normalized-equal question also hits
    assert cache_lookup("  회사   작년 매출은?  ", scope="company", project="atlas") is not None


def test_lookup_miss_on_watermark_change(clean):
    q = "회사 직원 수는?"
    _store(q, _wiki(q), scope="company", project="atlas")
    assert cache_lookup(q, scope="company", project="atlas") is not None
    _insert_company_page("atlas")  # company knowledge changed → stale
    assert cache_lookup(q, scope="company", project="atlas") is None


def test_lookup_miss_on_ttl_expiry(clean):
    q = "회사 위치는?"
    _store(q, _wiki(q), scope="company", project="atlas")
    with session() as s:
        s.execute(update(ask_cache).values(created_at=datetime.now(UTC) - timedelta(days=2)))
        s.commit()
    # default TTL 86400s (1 day) → 2-day-old row expires
    assert cache_lookup(q, scope="company", project="atlas") is None


def test_lookup_partition_isolated_by_project_and_node(clean):
    q = "회사 비전은?"
    _store(q, _wiki(q), scope="company", project="atlas")
    assert cache_lookup(q, scope="company", project="nova") is None  # other project
    settings = get_settings()
    original = settings.node_id
    try:
        settings.node_id = "other-node"
        assert cache_lookup(q, scope="company", project="atlas") is None  # other node
    finally:
        settings.node_id = original


def test_store_upserts_same_partition(clean):
    q = "회사 대표 이름은?"
    _store(q, _wiki(q, body="대표는 A입니다."), scope="company", project="atlas")
    _store(q, _wiki(q, body="대표는 B입니다."), scope="company", project="atlas")
    with session() as s:
        n = s.execute(select(ask_cache)).all()
    assert len(n) == 1  # upsert, not a second row
    hit = cache_lookup(q, scope="company", project="atlas")
    assert hit is not None and hit.wiki is not None and hit.wiki.answer == "대표는 B입니다."


# --- wrapper integration (answer() hooks) --------------------------------------


@pytest.fixture
def route_counter(monkeypatch):
    """Replace the routing ladder with a counter so we can observe cache hits as
    'route NOT re-invoked'. Returns a grounded company wiki answer by default."""
    state = {"calls": 0, "result": None}

    def fake_route(user_id, question, **kwargs):
        state["calls"] += 1
        return state["result"] or _wiki(question)

    monkeypatch.setattr(router, "_route_answer", fake_route)
    return state


def test_flag_off_never_caches(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", False)
    router.answer(user_id, "회사 매출?", scope="company", project="atlas")
    router.answer(user_id, "회사 매출?", scope="company", project="atlas")
    assert route_counter["calls"] == 2  # no hit
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []


def test_flag_on_company_grounded_hits_second_call(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    r1 = router.answer(user_id, "회사 매출?", scope="company", project="atlas")
    r2 = router.answer(user_id, "회사 매출?", scope="company", project="atlas")
    assert route_counter["calls"] == 1  # second served from cache
    assert r1.model_dump(mode="json") == r2.model_dump(mode="json")
    with session() as s:
        assert len(s.execute(select(ask_cache)).all()) == 1


def test_flag_on_personal_scope_not_cached(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    router.answer(user_id, "내 메모?", scope="personal", project="atlas")
    router.answer(user_id, "내 메모?", scope="personal", project="atlas")
    assert route_counter["calls"] == 2  # scope != company → cache_on False
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []


def test_flag_on_token_caller_not_cached(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    router.answer(user_id, "회사 매출?", scope="company", project="atlas", allow_cache=False)
    router.answer(user_id, "회사 매출?", scope="company", project="atlas", allow_cache=False)
    assert route_counter["calls"] == 2
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []


def test_flag_on_non_grounded_not_stored(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    route_counter["result"] = _wiki("회사 비밀?", body="근거 없음 — 데이터 부족.")
    router.answer(user_id, "회사 비밀?", scope="company", project="atlas")
    router.answer(user_id, "회사 비밀?", scope="company", project="atlas")
    assert route_counter["calls"] == 2  # not cacheable → recompute
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []


def test_flag_on_decomposed_not_stored(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    route_counter["result"] = RoutedAnswer(question="복합?", mode="decomposed")
    router.answer(user_id, "복합?", scope="company", project="atlas")
    router.answer(user_id, "복합?", scope="company", project="atlas")
    assert route_counter["calls"] == 2
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []


def test_flag_on_recomputes_after_knowledge_change(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    router.answer(user_id, "회사 매출?", scope="company", project="atlas")
    assert route_counter["calls"] == 1
    _insert_company_page("atlas")  # watermark advances
    router.answer(user_id, "회사 매출?", scope="company", project="atlas")
    assert route_counter["calls"] == 2  # stale → recompute + re-store


def test_store_stamps_pregrounding_watermark_not_stale_hit(clean, user_id, monkeypatch):
    # F1: a wiki edit landing in the answer-compute window must NOT over-stamp the cached row
    # with a newer watermark (which would make the stale answer HIT). answer() snapshots the
    # watermark BEFORE grounding, so a mid-compute edit makes the NEXT lookup a conservative
    # MISS + recompute. (With a store-time watermark read this would be a stale HIT → calls==1.)
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    calls = {"n": 0}

    def fake_route(user_id, question, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            _insert_company_page("atlas")  # knowledge changes DURING grounding → watermark moves
        return _wiki(question)

    monkeypatch.setattr(router, "_route_answer", fake_route)
    router.answer(user_id, "회사 매출?", scope="company", project="atlas")  # stamps pre-edit wm
    router.answer(user_id, "회사 매출?", scope="company", project="atlas")
    assert calls["n"] == 2  # stamp predates the mid-compute edit → MISS, not a stale HIT


def test_cache_hit_emits_no_router_answer_span(clean, user_id, route_counter, monkeypatch):
    # Hit accounting is delegated to the ask.cache(result=hit) span; a hit must NOT emit a
    # router.answer span (that span is non-uniform across the ladder; see answer()).
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    router.answer(user_id, "회사 매출?", scope="company", project="atlas")  # miss → store
    with session() as s:  # isolate the hit's spans from the store's
        s.execute(audit_log.delete())
        s.commit()
    router.answer(user_id, "회사 매출?", scope="company", project="atlas")  # HIT
    assert route_counter["calls"] == 1
    with session() as s:
        router_spans = s.execute(
            select(audit_log.c.meta).where(audit_log.c.node == "router.answer")
        ).all()
        cache_hits = s.execute(
            select(audit_log.c.meta).where(
                audit_log.c.node == "ask.cache", audit_log.c.phase == "exit"
            )
        ).all()
    assert router_spans == []  # no router.answer span on a hit
    assert any(r.meta.get("result") == "hit" for r in cache_hits)  # hit accounted on ask.cache


def test_cache_hit_publishes_terminal_stream_frame(clean, user_id, route_counter, monkeypatch):
    # F4: a hit bypasses the engine that an open SSE expects to terminate it, so answer()
    # publishes the terminal frame itself (else the stream hangs to keepalive/disconnect).
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    import orthus.agentwork.stream as agent_stream

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        agent_stream, "publish_threadsafe", lambda key, frame: published.append((key, frame))
    )
    router.answer(
        user_id, "회사 매출?", scope="company", project="atlas", stream_id="sid-1"
    )  # miss
    router.answer(
        user_id, "회사 매출?", scope="company", project="atlas", stream_id="sid-2"
    )  # HIT
    assert route_counter["calls"] == 1  # second served from cache
    # only the hit publishes a terminal frame (the miss routes through the fake engine which does not)
    assert published == [(f"{user_id}:sid-2", {"type": "turn_complete"})]


def test_cache_hit_no_stream_frame_without_stream_id(clean, user_id, route_counter, monkeypatch):
    # F4 guard: no stream_id → nothing published (no phantom frame to a non-existent stream).
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    import orthus.agentwork.stream as agent_stream

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        agent_stream, "publish_threadsafe", lambda key, frame: published.append((key, frame))
    )
    router.answer(user_id, "회사 매출?", scope="company", project="atlas")  # miss → store
    router.answer(user_id, "회사 매출?", scope="company", project="atlas")  # HIT, no stream_id
    assert route_counter["calls"] == 1 and published == []


def test_flag_on_structured_not_cached(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    route_counter["result"] = _structured("executed")  # notion_rows substrate, watermark-uncovered
    router.answer(user_id, "열린 티켓 수?", scope="company", project="atlas")
    router.answer(user_id, "열린 티켓 수?", scope="company", project="atlas")
    assert route_counter["calls"] == 2
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []


def test_flag_on_transient_graph_demote_not_cached(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    # graph→wiki demote during a KG outage carries the degraded-KG banner → must not freeze
    route_counter["result"] = _wiki("관계?").model_copy(
        update={"warnings": [router._GRAPH_DEGRADED_MESSAGE]}
    )
    router.answer(user_id, "관계?", scope="company", project="atlas")
    router.answer(user_id, "관계?", scope="company", project="atlas")
    assert route_counter["calls"] == 2
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []


def test_flag_on_context_wiki_slug_not_cached(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    router.answer(
        user_id, "이 페이지?", scope="company", project="atlas", context_wiki_slug="회사/개요"
    )
    router.answer(
        user_id, "이 페이지?", scope="company", project="atlas", context_wiki_slug="회사/개요"
    )
    assert route_counter["calls"] == 2  # context-grounded → request-scoped, not cached
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []


def test_flag_on_history_not_cached(clean, user_id, route_counter, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    hist = [ChatTurn(role="user", text="이전 질문")]
    router.answer(user_id, "그건 언제야?", scope="company", project="atlas", history=hist)
    router.answer(user_id, "그건 언제야?", scope="company", project="atlas", history=hist)
    assert route_counter["calls"] == 2  # follow-up depends on session history → not cached
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []


# --- guards (defense in depth) -------------------------------------------------


def test_cache_store_rejects_non_company(clean):
    # call cache_store directly to exercise its own scope guard (raised before watermark use)
    with pytest.raises(ValueError):
        cache_store("q", _wiki("q"), scope="personal", project="atlas", watermark="0:0")
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []  # nothing written


def test_cache_lookup_non_company_returns_none(clean):
    assert cache_lookup("q", scope="personal", project="atlas") is None


def test_store_nulls_stream_id(clean):
    q = "회사 슬로건은?"
    ans = _wiki(q).model_copy(update={"stream_id": "sid-from-request-A"})
    _store(q, ans, scope="company", project="atlas")
    hit = cache_lookup(q, scope="company", project="atlas")
    assert hit is not None and hit.stream_id is None  # prior request's SSE id not replayed


# --- MA.7b semantic (embedding) match ------------------------------------------
#
# §P3A.4 / 불변식 25: a paraphrase that the exact key misses is matched by cosine
# similarity ≥ τ on a deterministic embedding, behind a SEPARATE sub-flag. τ is a fixed
# constant, so hit/miss stays deterministic. Tests self-calibrate τ off the actual mock
# cosine (± a margin) so they don't hard-code the mock's internals.

# a genuine paraphrase: same intent, reordered + different surface form → exact key differs
_Q_STORE = "회사 작년 매출은 얼마였나요?"
_Q_PARAPHRASE = "작년 회사 매출 알려줘"
_Q_UNRELATED = "직원 복지 제도는 어떻게 되나요"


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # mock vectors are unit-norm → dot == cosine


@pytest.fixture
def semantic_on(monkeypatch):
    """Both flags on: master cache + MA.7b embedding match."""
    s = get_settings()
    monkeypatch.setattr(s, "ask_semantic_cache_enabled", True)
    monkeypatch.setattr(s, "ask_cache_semantic_match_enabled", True)
    return s


def test_semantic_hit_on_paraphrase_within_threshold(clean, semantic_on, monkeypatch):
    sim = _cos(_embed_question(_Q_STORE), _embed_question(_Q_PARAPHRASE))
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", sim - 0.01)
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    assert question_key(_Q_STORE) != question_key(_Q_PARAPHRASE)  # exact key can't match
    hit = cache_lookup(_Q_PARAPHRASE, scope="company", project="atlas")
    assert hit is not None  # semantic absorbed the paraphrase
    assert hit.wiki is not None and hit.wiki.answer == "회사 매출은 1.2억입니다."


def test_semantic_miss_above_threshold(clean, semantic_on, monkeypatch):
    sim = _cos(_embed_question(_Q_STORE), _embed_question(_Q_PARAPHRASE))
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", sim + 0.01)
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    assert cache_lookup(_Q_PARAPHRASE, scope="company", project="atlas") is None  # below τ


def test_semantic_miss_on_unrelated_question(clean, semantic_on):
    # default τ=0.95; an unrelated question (cosine ~0) never false-hits
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    assert cache_lookup(_Q_UNRELATED, scope="company", project="atlas") is None


def test_semantic_subflag_off_is_exact_only(clean, monkeypatch):
    # master cache on, MA.7b sub-flag OFF, τ=0 (would match anything IF semantic ran)
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", True)
    monkeypatch.setattr(get_settings(), "ask_cache_semantic_match_enabled", False)
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", 0.0)
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    # paraphrase misses (no semantic query); store wrote NO embedding/redacted text
    assert cache_lookup(_Q_PARAPHRASE, scope="company", project="atlas") is None
    with session() as s:
        row = s.execute(
            select(ask_cache.c.question_embedding, ask_cache.c.question_redacted)
        ).first()
    assert row.question_embedding is None and row.question_redacted is None


def test_semantic_cross_partition_isolated(clean, semantic_on, monkeypatch):
    # τ=0 → would match ANY row in the same partition; isolation must come from the key, not τ
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", 0.0)
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    assert cache_lookup(_Q_PARAPHRASE, scope="company", project="nova") is None  # other project
    settings = get_settings()
    original = settings.node_id
    try:
        settings.node_id = "other-node"
        assert cache_lookup(_Q_PARAPHRASE, scope="company", project="atlas") is None  # other node
    finally:
        settings.node_id = original


def test_semantic_miss_on_watermark_change(clean, semantic_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", 0.0)
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    assert cache_lookup(_Q_PARAPHRASE, scope="company", project="atlas") is not None
    _insert_company_page("atlas")  # company knowledge changed → stale, even for semantic
    assert cache_lookup(_Q_PARAPHRASE, scope="company", project="atlas") is None


def test_semantic_miss_on_ttl_expiry(clean, semantic_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", 0.0)
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    with session() as s:
        s.execute(update(ask_cache).values(created_at=datetime.now(UTC) - timedelta(days=2)))
        s.commit()
    assert cache_lookup(_Q_PARAPHRASE, scope="company", project="atlas") is None  # TTL backstop


def test_semantic_store_redacts_pii(clean, semantic_on):
    # PII in the question must not reach question_redacted (불변식 6 / §P3A.7)
    q = "김민수 010-1234-5678 에게 회사 매출 물어봐"
    _store(q, _wiki(q), scope="company", project="atlas")
    with session() as s:
        row = s.execute(select(ask_cache.c.question_redacted)).first()
    assert row.question_redacted is not None
    assert "1234-5678" not in row.question_redacted  # raw phone digits masked
    assert "****" in row.question_redacted


def test_semantic_exact_still_preferred(clean, semantic_on, monkeypatch):
    # exact key hit short-circuits before any embedding work — identical question still hits
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", 0.0)
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    hit = cache_lookup(_Q_STORE, scope="company", project="atlas")
    assert hit is not None and hit.wiki is not None


def test_semantic_wrapper_hit_skips_recompute(
    clean, semantic_on, user_id, route_counter, monkeypatch
):
    # through router.answer: a paraphrase second call is served from the semantic cache
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", 0.0)
    router.answer(user_id, _Q_STORE, scope="company", project="atlas")
    assert route_counter["calls"] == 1
    router.answer(user_id, _Q_PARAPHRASE, scope="company", project="atlas")
    assert route_counter["calls"] == 1  # paraphrase replayed from cache, ladder not re-run


# --- MA.7b robustness / observability (code-review follow-ups) ------------------


class _BoomEmbedding:
    """An embedder whose .embed() always raises — simulates a provider outage."""

    model_version = "boom:1024"

    def embed(self, texts):
        raise RuntimeError("embedding provider down")


class _CountingEmbedding:
    """Wraps the mock embedder and counts embed() calls (to assert the cold-path embed skip)."""

    model_version = "count:1024"

    def __init__(self):
        self.calls = 0
        self._inner = MockEmbedding()

    def embed(self, texts):
        self.calls += 1
        return self._inner.embed(texts)


def test_semantic_cold_partition_skips_embed(clean, semantic_on, monkeypatch):
    # a never-asked question against a partition with no embedded rows must NOT call the
    # embedding provider — the cheap existence check gates the embed (cold-path guard)
    counter = _CountingEmbedding()
    monkeypatch.setattr(cache_mod, "get_embedding_model", lambda: counter)
    assert cache_lookup("회사 한 번도 안 물어본 질문", scope="company", project="atlas") is None
    assert counter.calls == 0  # exact miss + empty partition → embed skipped


def test_semantic_store_degrades_on_embed_error(clean, semantic_on, monkeypatch):
    # an embedding failure must NOT abort caching an already-produced answer; it stores an
    # exact-only row (NULL embedding) and the answer is still served on the exact path
    monkeypatch.setattr(cache_mod, "get_embedding_model", lambda: _BoomEmbedding())
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")  # no raise
    with session() as s:
        row = s.execute(select(ask_cache.c.question_embedding)).first()
    assert row is not None and row.question_embedding is None  # exact-only row stored
    assert cache_lookup(_Q_STORE, scope="company", project="atlas") is not None  # exact hit
    # the degraded store still emits its `store` audit span (operators must see failure-path stores)
    assert "store" in _ask_cache_results("ask.cache")


def test_semantic_lookup_degrades_on_embed_error(clean, semantic_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", 0.0)
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")  # real embedder
    monkeypatch.setattr(cache_mod, "get_embedding_model", lambda: _BoomEmbedding())
    # paraphrase: exact miss → semantic embed raises → degrade to MISS, never raise
    assert cache_lookup(_Q_PARAPHRASE, scope="company", project="atlas") is None


def test_semantic_embedding_preserved_when_subflag_toggled_off(clean, semantic_on, monkeypatch):
    # store WITH the sub-flag on → embedding present
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    with session() as s:
        before = s.execute(
            select(ask_cache.c.question_embedding, ask_cache.c.question_redacted)
        ).first()
    assert before.question_embedding is not None and before.question_redacted is not None
    # toggle the sub-flag OFF and re-store the same question (e.g. a watermark-driven recompute)
    monkeypatch.setattr(get_settings(), "ask_cache_semantic_match_enabled", False)
    _store(_Q_STORE, _wiki(_Q_STORE, body="갱신된 답"), scope="company", project="atlas")
    with session() as s:
        after = s.execute(
            select(
                ask_cache.c.question_embedding,
                ask_cache.c.question_redacted,
                ask_cache.c.answer_json,
            )
        ).first()
    # BOTH semantic columns PRESERVED, not wiped to NULL — recall survives a flag toggle
    assert after.question_embedding is not None
    assert after.question_redacted is not None
    assert after.answer_json["wiki"]["answer"] == "갱신된 답"  # answer/watermark still refreshed


def test_semantic_skips_corrupt_nearest_row(clean, semantic_on, monkeypatch):
    # a corrupt (undecodable) row that is the NEAREST neighbor must not poison a valid,
    # farther, still-in-threshold hit (top-K scan, not LIMIT 1).
    # Deterministic: the partition-scoped query filters via the `uq_ask_cache_key` btree and
    # sorts the partition's rows by distance EXACTLY (the planner does not use the approximate
    # ivfflat index for a selective-partition query), so the corrupt row at distance 0 always
    # ranks first and the valid row is always a candidate.
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", 0.0)
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")  # valid, farther
    qvec = _embed_question(_Q_PARAPHRASE)  # the query vector
    wm = knowledge_watermark("company", "atlas")
    settings = get_settings()
    with session() as s:
        s.execute(
            insert(ask_cache).values(
                id=uuid4(),
                owner_id="company",
                scope="company",
                project="atlas",
                federation=False,
                node_id=settings.node_id,
                question_key="corrupt-nearest",
                question_embedding=qvec,  # distance 0 → nearest
                question_redacted="x",
                watermark=wm,
                answer_json={"mode": "wiki"},  # missing required `question` → ValidationError
                created_at=datetime.now(UTC),
            )
        )
        s.commit()
    hit = cache_lookup(_Q_PARAPHRASE, scope="company", project="atlas")
    assert hit is not None  # skipped the corrupt nearest, returned the valid farther row
    assert hit.wiki is not None and hit.wiki.answer == "회사 매출은 1.2억입니다."


def _ask_cache_metas(node: str = "ask.cache") -> list[dict]:
    with session() as s:
        return [
            r.meta
            for r in s.execute(
                select(audit_log.c.meta).where(
                    audit_log.c.node == node, audit_log.c.phase == "exit"
                )
            ).all()
        ]


def _ask_cache_results(node: str = "ask.cache") -> list[str]:
    return [m.get("result") for m in _ask_cache_metas(node)]


def test_cache_and_embed_emit_audit_spans(clean, semantic_on):
    # AGENTS.md 코드 컨벤션 / 원칙 3: 임베딩 호출 각각에 audit span; cache results are audited too
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    cache_lookup(_Q_STORE, scope="company", project="atlas")  # exact hit
    results = _ask_cache_results("ask.cache")
    assert "store" in results and "hit" in results
    with session() as s:
        embed_spans = s.execute(
            select(audit_log).where(
                audit_log.c.node == "ask.cache.embed", audit_log.c.phase == "exit"
            )
        ).all()
    assert embed_spans  # the store-time embedding call was audited


def test_semantic_miss_audit_distinguishes_stage(clean, semantic_on, monkeypatch):
    # a semantic-stage miss is audited as miss_semantic (not the bare exact "miss")
    monkeypatch.setattr(get_settings(), "ask_cache_similarity_threshold", 0.99)  # nothing matches
    _store(_Q_STORE, _wiki(_Q_STORE), scope="company", project="atlas")
    with session() as s:  # drop the store-time audit rows so we only see the lookup's
        s.execute(audit_log.delete())
        s.commit()
    assert cache_lookup(_Q_PARAPHRASE, scope="company", project="atlas") is None
    # the span records the semantic-stage result AND keeps the exact-stage reason as `exact=`
    # meta, so exact-stage vs semantic-stage misses stay distinguishable
    metas = _ask_cache_metas("ask.cache")
    assert any(m.get("result") == "miss_semantic" and m.get("exact") == "miss" for m in metas)


# --- MA.7c GC (TTL/stale row sweep) --------------------------------------------
#
# §P3A.10 / §P3A.3.3: the lazy on-read path never DELETEs, so stale (watermark-advanced)
# and TTL-expired rows accumulate. `gc()` is the optional active sweep that bounds the table.
# Gate: 누적 행 bound 확인 + GC가 정상 hit 무영향 (a live row a lookup would HIT survives GC).


def _age_all_rows(days: int) -> None:
    with session() as s:
        s.execute(update(ask_cache).values(created_at=datetime.now(UTC) - timedelta(days=days)))
        s.commit()


def test_gc_deletes_ttl_expired(clean):
    _store("회사 위치는?", _wiki("q"), scope="company", project="atlas")
    _age_all_rows(2)  # default TTL 1 day → 2-day-old row is expired
    report = cache_mod.gc()
    assert report["deleted_ttl"] == 1
    assert report["remaining"] == 0
    with session() as s:
        assert s.execute(select(ask_cache)).all() == []


def test_gc_deletes_watermark_stale(clean):
    _store("회사 매출은?", _wiki("q"), scope="company", project="atlas")
    _insert_company_page("atlas")  # watermark advances → the stored row is now stale
    report = cache_mod.gc()
    assert report["deleted_stale"] == 1
    assert report["deleted_ttl"] == 0  # fresh row, not TTL-expired
    assert report["remaining"] == 0


def test_gc_keeps_live_row_and_lookup_still_hits(clean):
    # 수용 게이트: a live row (current watermark, within TTL) is never deleted → a lookup that
    # would HIT before GC still HITs after.
    q = "회사 대표 이름은?"
    _store(q, _wiki(q), scope="company", project="atlas")
    assert cache_lookup(q, scope="company", project="atlas") is not None  # HIT before GC
    report = cache_mod.gc()
    assert report["deleted_ttl"] == 0 and report["deleted_stale"] == 0
    assert report["remaining"] == 1
    assert cache_lookup(q, scope="company", project="atlas") is not None  # still HIT after GC


def test_gc_bounds_accumulation_across_watermark_advances(clean):
    # Each new question + watermark advance orphans the prior rows. Without GC they pile up;
    # gc() converges the table to just the live row(s).
    for i in range(5):
        _store(f"질문 {i}?", _wiki("q"), scope="company", project="atlas")
        _insert_company_page("atlas")  # advance watermark → previous rows become stale
    _store("최신 질문?", _wiki("q"), scope="company", project="atlas")  # the only live row
    with session() as s:
        assert len(s.execute(select(ask_cache)).all()) == 6  # 5 stale + 1 live accumulated
    report = cache_mod.gc()
    assert report["deleted_stale"] == 5
    assert report["remaining"] == 1  # bounded down to the live row


def test_gc_partition_isolated_by_project(clean):
    # stale rows in one project are deleted by THAT project's watermark; a live row in another
    # project (whose watermark is unchanged) is untouched.
    _store("아틀라스 질문?", _wiki("q"), scope="company", project="atlas")
    _store("노바 질문?", _wiki("q"), scope="company", project="nova")
    _insert_company_page("atlas")  # only atlas's watermark advances
    report = cache_mod.gc()
    assert report["deleted_stale"] == 1  # atlas row only
    assert report["remaining"] == 1
    assert cache_lookup("노바 질문?", scope="company", project="nova") is not None  # nova live


def test_gc_runs_regardless_of_cache_flag(clean, monkeypatch):
    # GC clears dead rows whether or not the cache master flag is on (the lookup/store hooks stay
    # flag-gated, but the standalone sweep does not need the cache enabled).
    _store("회사 비전은?", _wiki("q"), scope="company", project="atlas")
    _age_all_rows(2)
    monkeypatch.setattr(get_settings(), "ask_semantic_cache_enabled", False)
    report = cache_mod.gc()
    assert report["deleted_ttl"] == 1 and report["remaining"] == 0


def test_gc_emits_audit_span(clean):
    _store("회사 슬로건은?", _wiki("q"), scope="company", project="atlas")
    _insert_company_page("atlas")
    with session() as s:  # isolate the gc span from store-time spans
        s.execute(audit_log.delete())
        s.commit()
    cache_mod.gc()
    metas = _ask_cache_metas("ask.cache.gc")
    assert any(m.get("deleted_stale") == 1 and "remaining" in m for m in metas)


def test_gc_cli_main(clean):
    assert cache_mod.main(["gc"]) == 0  # happy path
    assert cache_mod.main([]) == 0  # default command is gc
    assert cache_mod.main(["bogus"]) == 2  # unknown command rejected
