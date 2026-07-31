"""P2.2b acceptance: auto-router + unified entrypoint + scope-isolation hardening
(docs/architecture-v2.md §1/§7).

- Routing: a structured-style question is dispatched to the structured(PG) backend
  and returns rows; a knowledge question is dispatched to the wiki backend and
  returns a grounded answer.
- Scope hardening: the deterministic sqlglot rewrite filters another user's
  personal rows out at the executor level, even when the compiled SQL explicitly
  targets that personal db_name.

Postgres must be up on localhost:5433 (run `make up && make migrate`)."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import func, insert, select

from orthus.db import session
from orthus.kg.client import close_kg_driver, kg_available
from orthus.kg.entities import persist_entities
from orthus.kg.gate import KgQueryStatus, KgTemplateResult
from orthus.kg.rebuild import run_rebuild
from orthus.models.adapters.mock import MockChat
from orthus.router import _GRAPH_DEGRADED_MESSAGE, answer, classify
from orthus.router import graph as graph_module
from orthus.router.graph import bind_graph_params
from orthus.router.route import _GRAPH_TERMS, _rule_based_route
from orthus.schemas.canonical import (
    ExtractedEntity,
    KgGraphNode,
    WikiClaim,
    WikiPage,
    WikiSourceRef,
)
from orthus.settings import get_settings
from orthus.structured.query import query_structured
from orthus.tables import data_gaps, notion_rows, users
from orthus.wiki import store as wiki_store


def _make_user() -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name="U"))
        s.commit()
    return uid


def _seed_row(user_id, *, db_name, properties, scope, owner_id=None) -> None:
    with session() as s:
        s.execute(
            insert(notion_rows).values(
                row_id=uuid.uuid4(),
                db_id=f"db-{db_name}",
                db_name=db_name,
                properties=properties,
                scope=scope,
                owner_id=owner_id,
                user_id=user_id,
            )
        )
        s.commit()


# --- routing --------------------------------------------------------------------


class _CountingChat:
    """MockChat 위에 complete() 호출 횟수를 세는 얇은 래퍼 (LLM 호출 여부 검증용)."""

    model_id = "mock:counting"

    def __init__(self, default: str):
        self._inner = MockChat(default=default)
        self.calls = 0

    def complete(self, system, user, *, json_only=False):
        self.calls += 1
        return self._inner.complete(system, user, json_only=json_only)


def test_classify_wiki_default_forces_wiki_but_keeps_graph_in_blind_zone(monkeypatch):
    """default(routing_wiki_default=True): 규칙 사각지대에서 LLM의 structured/wiki 판정은
    무시하고 wiki로 강제하되, `graph` 판정은 살린다.

    홀드아웃(docs/routing-holdout-plan.md §13) — LLM 라우터는 wiki 질문의 절반을 structured로
    오라우팅해 "무조건 wiki" 상수보다 낮았다. 그래서 mock이 structured/junk를 스크립트해도 wiki다.
    단 홀드아웃은 graph 평면을 미측정했으므로 LLM `graph` enum은 통과시켜야 K8 conflict/관계
    질문(규칙 앵커 없는 NL)이 graph에 도달한다.
    """
    monkeypatch.delenv("ORTHUS_ROUTING_WIKI_DEFAULT", raising=False)
    get_settings.cache_clear()
    assert classify("q", chat_model=MockChat(default='{"route": "structured"}')) == "wiki"
    assert classify("q", chat_model=MockChat(default="not json at all")) == "wiki"
    # graph 판정은 wiki로 강제되지 않고 살아남는다(사각지대 conflict/관계 질문의 유일한 graph 경로).
    assert classify("q", chat_model=MockChat(default='{"route": "graph"}')) == "graph"
    # ★ graph를 살리려면 LLM을 반드시 호출해야 한다 — 커밋된 초안(HEAD)은 LLM 앞에서 wiki로
    # 조기 반환해 graph를 죽였다. LLM 호출 여부를 고정해 그 회귀를 다시 막는다(code review #6).
    counting = _CountingChat(default='{"route": "graph"}')
    assert classify("q", chat_model=counting) == "graph"
    assert counting.calls == 1, "사각지대에서 graph를 살리려면 LLM을 호출해야 한다"
    get_settings.cache_clear()


def test_classify_quantity_route_skips_llm(monkeypatch):
    """수량 신호는 LLM 앞에서 잡히므로 사각지대라도 LLM을 부르지 않는다(code review #6)."""
    monkeypatch.delenv("ORTHUS_ROUTING_WIKI_DEFAULT", raising=False)
    get_settings.cache_clear()
    counting = _CountingChat(default='{"route": "wiki"}')
    assert classify("취소된 항목이 얼마나 되나요", chat_model=counting) == "structured"
    assert counting.calls == 0, "수량 신호는 LLM 없이 결정된다"
    get_settings.cache_clear()


def test_classify_llm_path_when_wiki_default_off(monkeypatch):
    """revert switch(routing_wiki_default=False): 사각지대에서 기존처럼 LLM에 묻고 junk는 wiki로."""
    monkeypatch.setenv("ORTHUS_ROUTING_WIKI_DEFAULT", "false")
    get_settings.cache_clear()
    assert classify("q", chat_model=MockChat(default='{"route": "structured"}')) == "structured"
    assert classify("q", chat_model=MockChat(default='{"route": "wiki"}')) == "wiki"
    assert classify("q", chat_model=MockChat(default="not json at all")) == "wiki"
    get_settings.cache_clear()


def test_classify_quantity_terms_route_to_structured():
    """C2 후순위 수량 신호 — "얼마나 되나요"류 집계 질문. LLM 없이 structured."""
    assert classify("취소된 항목이 얼마나 되나요", chat_model=MockChat(default="not json")) == "structured"
    assert classify("밀린 업무가 얼마나 쌓여 있어", chat_model=MockChat(default="not json")) == "structured"
    # 수량어 + wiki어 공존 시 기존 규칙(wiki)이 먼저 결정한다(수량은 후순위).
    assert classify("환불 정책이 뭐야", chat_model=MockChat(default="not json")) == "wiki"


def test_classify_uses_deterministic_rules_for_obvious_routes():
    assert classify("아크메 직원 목록", chat_model=MockChat(default="not json")) == "structured"
    assert (
        classify("협업업무표 상태별 건수", chat_model=MockChat(default="not json")) == "structured"
    )
    assert (
        classify(
            "Atlas 정산 화면 금액 제대로 반영되도록 하기 작업은 뭐야?",
            chat_model=MockChat(default='{"route": "structured"}'),
        )
        == "wiki"
    )


def test_answer_routes_structured_returns_rows(user_id):
    """A structured-style question routes to the structured backend and executes."""
    for status in ["진행중", "진행중", "완료"]:
        _seed_row(user_id, db_name="티켓", properties={"상태": status}, scope="company")

    sql = (
        "SELECT properties->>'상태' AS status, count(*) AS n "
        "FROM notion_rows WHERE db_name = '티켓' GROUP BY 1"
    )
    # Script BOTH calls by distinct needles: "query router" appears only in the
    # classify system prompt; "Schema:" only in the compile prompt. So classify
    # gets {"route": "structured"} and compile gets the SQL — no keyword heuristic.
    chat = MockChat(
        rules=[
            ("query router", '{"route": "structured"}'),
            ("Schema:", '{"sql": "%s"}' % sql),
        ]
    )
    result = answer(user_id, "티켓 상태별 개수", chat_model=chat)

    assert result.mode == "structured"
    assert result.wiki is None
    assert result.structured is not None
    assert result.structured.status == "executed"
    counts = {row[0]: row[1] for row in result.structured.rows}
    assert counts == {"진행중": 2, "완료": 1}


def _rejected_structured(question, reason="unknown_column:properties2"):
    from uuid import uuid4

    from orthus.schemas.canonical import AssistantResult, ValidationResult

    return AssistantResult(
        query_id=uuid4(),
        question=question,
        compiled=None,
        validation=ValidationResult(rejected_reason=reason),
        status="rejected",
    )


def _executed_zero_rows(question):
    from uuid import uuid4

    from orthus.schemas.canonical import AssistantResult, ValidationResult

    return AssistantResult(
        query_id=uuid4(),
        question=question,
        compiled=None,
        validation=ValidationResult(rejected_reason=None),  # passed
        status="executed",
        row_count=0,
    )


def _wiki_with_source(question, answer="복구된 답변"):
    from orthus.schemas.canonical import WikiAnswer, WikiSourceRef

    return WikiAnswer(
        question=question,
        answer=answer,
        sources=[
            WikiSourceRef(
                page_slug="p", title="t", kind="page", excerpt="e", score=0.9
            )
        ],
    )


def test_c3a_gate_reject_recovers_via_wiki(monkeypatch, user_id):
    """C3-a: structured가 게이트 거부되면 wiki로 재질의해 서빙한다(routing holdout §13)."""
    import orthus.router as router_mod

    monkeypatch.setenv("ORTHUS_ROUTING_GATE_REJECT_WIKI_RECOVER", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(router_mod, "query_structured", lambda *a, **k: _rejected_structured("q"))
    monkeypatch.setattr(router_mod, "ask", lambda *a, **k: _wiki_with_source("q"))

    res = answer(user_id, "직원 목록", chat_model=MockChat(default="not json"), allow_decompose=False)

    assert res.mode == "wiki"
    assert res.wiki is not None and res.wiki.answer == "복구된 답변"
    get_settings.cache_clear()


def test_c3a_keeps_structured_when_wiki_has_no_source(monkeypatch, user_id):
    """회수 실패(wiki 무근거)면 게이트 거부 결과를 숨기지 않고 그대로 반환한다."""
    import orthus.router as router_mod

    monkeypatch.setenv("ORTHUS_ROUTING_GATE_REJECT_WIKI_RECOVER", "true")
    get_settings.cache_clear()
    empty_wiki = _wiki_with_source("q")
    empty_wiki = empty_wiki.model_copy(update={"sources": []})
    monkeypatch.setattr(router_mod, "query_structured", lambda *a, **k: _rejected_structured("q"))
    monkeypatch.setattr(router_mod, "ask", lambda *a, **k: empty_wiki)

    res = answer(user_id, "직원 목록", chat_model=MockChat(default="not json"), allow_decompose=False)

    assert res.mode == "structured"
    assert res.structured is not None and res.structured.status == "rejected"
    get_settings.cache_clear()


def test_c3a_does_not_fire_on_zero_rows(monkeypatch, user_id):
    """0행은 회수하지 않는다 — 진짜 '0건'을 wiki로 지어내면 TN 환각(C3-c 기각)."""
    import orthus.router as router_mod

    monkeypatch.setenv("ORTHUS_ROUTING_GATE_REJECT_WIKI_RECOVER", "true")
    get_settings.cache_clear()
    called = {"ask": False}

    def _ask_tripwire(*a, **k):
        called["ask"] = True
        return _wiki_with_source("q")

    monkeypatch.setattr(router_mod, "query_structured", lambda *a, **k: _executed_zero_rows("q"))
    monkeypatch.setattr(router_mod, "ask", _ask_tripwire)

    res = answer(user_id, "직원 목록", chat_model=MockChat(default="not json"), allow_decompose=False)

    assert res.mode == "structured"
    assert called["ask"] is False, "0행에서 wiki 재질의가 발동하면 안 된다"
    get_settings.cache_clear()


def test_c3a_does_not_recover_scope_rewrite_failed(monkeypatch, user_id):
    """scope_rewrite_failed는 fail-closed 보안 reject라 wiki로 회수하지 않는다(code review #4)."""
    import orthus.router as router_mod

    monkeypatch.setenv("ORTHUS_ROUTING_GATE_REJECT_WIKI_RECOVER", "true")
    get_settings.cache_clear()
    called = {"ask": False}

    def _ask_tripwire(*a, **k):
        called["ask"] = True
        return _wiki_with_source("q")

    monkeypatch.setattr(
        router_mod,
        "query_structured",
        lambda *a, **k: _rejected_structured("q", reason="scope_rewrite_failed:ValueError"),
    )
    monkeypatch.setattr(router_mod, "ask", _ask_tripwire)

    res = answer(user_id, "직원 목록", chat_model=MockChat(default="not json"), allow_decompose=False)

    assert res.mode == "structured"
    assert res.structured is not None and res.structured.status == "rejected"
    assert called["ask"] is False, "scope_rewrite_failed는 wiki로 회수하면 안 된다(보안 reject 은닉)"
    get_settings.cache_clear()


def test_classify_intent_read_route_matches_classify_in_blind_zone(monkeypatch):
    """classify_intent(read route)가 classify()와 동일한 C2 사각지대 정책을 쓴다(code review #2)."""
    from orthus.router.route import classify_intent

    monkeypatch.delenv("ORTHUS_ROUTING_WIKI_DEFAULT", raising=False)
    get_settings.cache_clear()
    # 수량 신호: LLM 앞에서 structured (classify와 동일)
    intent = classify_intent(
        "취소된 항목이 얼마나 되나요", allow_commands=False, chat_model=MockChat(default="not json")
    )
    assert intent.route == "structured"
    # 사각지대 LLM structured 판정 → wiki 강제 (classify와 동일)
    intent = classify_intent(
        "q", allow_commands=False, chat_model=MockChat(default='{"route": "structured"}')
    )
    assert intent.route == "wiki"
    # graph는 살린다 (classify와 동일)
    intent = classify_intent(
        "q", allow_commands=False, chat_model=MockChat(default='{"route": "graph"}')
    )
    assert intent.route == "graph"
    get_settings.cache_clear()


def test_answer_routes_wiki_returns_answer(user_id):
    """A knowledge question routes to the wiki backend and returns an answer."""
    result = answer(
        user_id,
        "온보딩 절차는 어떻게 진행되나요?",
        chat_model=MockChat(default="근거에 기반한 답변."),
    )
    assert result.mode == "wiki"
    assert result.structured is None
    assert result.wiki is not None
    assert result.wiki.answer


# --- scope-isolation hardening --------------------------------------------------


def test_scope_rewrite_hides_another_users_personal_rows(user_id):
    """User A has personal rows in db '개인메모' with a secret. User B issues a
    structured query that explicitly targets db_name='개인메모' — the injected scope
    predicate must filter A's personal rows out, returning ZERO. Company rows for
    the same db_name remain visible to all."""
    user_b = _make_user()
    # A's PERSONAL secret rows.
    _seed_row(
        user_id,
        db_name="개인메모",
        properties={"비밀": "오로라"},
        scope="personal",
        owner_id=user_id,
    )
    _seed_row(
        user_id, db_name="개인메모", properties={"비밀": "노바"}, scope="personal", owner_id=user_id
    )
    # A COMPANY row under the same db_name — shared, visible to everyone.
    _seed_row(user_id, db_name="개인메모", properties={"공지": "전체공개"}, scope="company")

    # B crafts a query that explicitly targets the personal db_name (an attacker
    # who learned the name). The compiled SQL has no scope predicate of its own.
    # query_structured is called directly (no classify), so the rule may match freely.
    crafted = "SELECT count(*) AS n FROM notion_rows WHERE db_name = '개인메모'"
    chat = MockChat(rules=[("개인메모", '{"sql": "%s"}' % crafted)])

    # B, scope='all': sees ONLY the company row (1), never A's 2 personal rows.
    res_b = query_structured(user_b, "개인메모 개수", chat_model=chat)
    assert res_b.status == "executed"
    assert res_b.rows[0][0] == 1, "B must not read A's personal rows"

    # A, scope='all': sees own 2 personal + 1 company = 3.
    res_a = query_structured(user_id, "개인메모 개수", chat_model=chat)
    assert res_a.status == "executed"
    assert res_a.rows[0][0] == 3, "A sees own personal + company rows"


# --- K4b graph branch -----------------------------------------------------------


@pytest.fixture
def graph_env(clean, kg_clean, tmp_path):
    """clean lays KG-off defaults + node_kind=company; kg_clean repoints to the :7688
    test container; wiki-store frontmatter is isolated per-test (mirrors test_kg_gate)."""
    settings = get_settings()
    prev_root = settings.wiki_store_path
    settings.wiki_store_path = tmp_path / "wiki-store"
    yield kg_clean
    settings.wiki_store_path = prev_root


def _write_page(user_id, slug, *, backlinks=(), scope="company", owner_id=None):
    return wiki_store.write_page(
        WikiPage(
            slug=slug,
            title=f"페이지 {slug}",
            definition=f"{slug} 정의",
            overview=f"{slug} 개요",
            backlinks=list(backlinks),
        ),
        user_id=user_id,
        scope=scope,
        owner_id=owner_id,
    )


def _seed_two_linked_pages(user_id):
    """page-y -[BACKLINK]-> page-x: a real knowledge edge path_between can find. Both
    materialized company pages with chunks (write_page → _persist → _reindex_chunks)."""
    _write_page(user_id, "page-x")
    _write_page(user_id, "page-y", backlinks=["page-x"])


def _relation_chat(subjects, *, default="page-x와 page-y는 백링크로 연결됩니다."):
    """MockChat scripting the bind extractor ('graph-query intent' is only in the bind
    system prompt) + a default for the wiki grounding answer. The relation question is
    rule-routed to graph, so classify needs no needle."""
    subj_json = "[" + ",".join('"%s"' % s for s in subjects) + "]"
    return MockChat(
        rules=[("graph-query intent", '{"intent":"relation","subjects":%s}' % subj_json)],
        default=default,
    )


def _missing_link_gap_count() -> int:
    """Count open missing_link data_gaps. The `clean` fixture wipes the table per test,
    so a whole-table count is the per-test delta basis."""
    with session() as s:
        return s.execute(
            select(func.count()).select_from(data_gaps).where(data_gaps.c.reason == "missing_link")
        ).scalar_one()


def test_route_graph_relation_question(graph_env, user_id, monkeypatch):
    """A relation question routes to graph, runs path_between, and grounds on the
    resolved compiled wiki pages (kg-impl §7.4). Also pins the load-bearing
    learn=False/record_gaps=False contract on the graph success path: a found connection
    must NOT compound a T2 claim or record a missing_link gap (kg-impl §7.3)."""
    _seed_two_linked_pages(user_id)
    run_rebuild()

    captured: dict = {}
    real_answer_from_hits = graph_module.answer_from_hits

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_answer_from_hits(*args, **kwargs)

    monkeypatch.setattr("orthus.router.graph.answer_from_hits", _spy)

    gaps_before = _missing_link_gap_count()
    res = answer(
        user_id, "page-x와 page-y는 무슨 관계야?", chat_model=_relation_chat(["page-x", "page-y"])
    )
    assert res.mode == "graph"
    assert res.graph is not None
    assert res.graph.template == "path_between"
    assert res.graph.intent == "relation"
    assert {"page-x", "page-y"}.issubset(set(res.graph.path_slugs))
    # Graph success path must not learn (compound T2) or record a missing_link gap.
    assert captured.get("learn") is False
    assert captured.get("record_gaps") is False
    assert _missing_link_gap_count() == gaps_before


def test_graph_answer_sources_are_wiki_pages(graph_env, user_id):
    """Acceptance criterion 3: a mode='graph' answer's sources are ALL compiled wiki
    page provenance (WikiSourceRef), restricted to the graph-resolved company pages."""
    _seed_two_linked_pages(user_id)
    run_rebuild()
    res = answer(
        user_id, "page-x와 page-y는 무슨 관계야?", chat_model=_relation_chat(["page-x", "page-y"])
    )
    assert res.mode == "graph"
    assert res.wiki is not None and res.wiki.sources
    assert all(isinstance(s, WikiSourceRef) for s in res.wiki.sources)
    assert all(s.kind in ("page", "claim") for s in res.wiki.sources)
    assert all(s.source_scope == "company" for s in res.wiki.sources)
    assert all(s.page_slug in {"page-x", "page-y"} for s in res.wiki.sources)


# --- K9.3a/b /ask entity intent (entity-rooted star + grounding) -----------------


def _seed_entity_pages(user_id):
    """page-a, page-b가 project:Orbit 엔티티를 공유 — /ask entity intent star의 최소 그래프.
    persist_entities를 page마다 호출해 kg_entities row(bind 단계 조회용) + MENTIONED_IN 투영을 만든다."""
    pa = _write_page(user_id, "page-a")
    pb = _write_page(user_id, "page-b")
    for pid, slug in ((pa, "page-a"), (pb, "page-b")):
        persist_entities(
            [ExtractedEntity(kind="project", name="Orbit")],
            page_id=pid,
            slug=slug,
            scope="company",
            user_id=user_id,
        )


def _entity_chat(subjects, *, default="공유 개체로 엮인 페이지들입니다."):
    """bind 추출기를 entity intent로 스크립트('graph-query intent' needle) + wiki grounding default.
    entity 질문은 '언급한 페이지' 앵커로 rule-route되어 graph에 들어오므로 classify needle 불요."""
    subj_json = "[" + ",".join('"%s"' % s for s in subjects) + "]"
    return MockChat(
        rules=[("graph-query intent", '{"intent":"entity","subjects":%s}' % subj_json)],
        default=default,
    )


def test_route_graph_entity_question(graph_env, user_id):
    """K9.3a/b — entity intent 질문이 entity_mentions로 바인딩돼 엔티티 중심 star를 반환하고,
    언급 page들로 grounding한다. graph(K9.3b)는 답 본문 위 시각 레이어다(불변식 5 — 본문은 여전히
    compiled wiki page). name_norm은 코드가 정규화('Orbit'→'orbit')하고 kg_entities로 확인한다."""
    _seed_entity_pages(user_id)
    run_rebuild()
    res = answer(user_id, "Orbit를 언급한 페이지 알려줘", chat_model=_entity_chat(["Orbit"]))
    assert res.mode == "graph"
    assert res.graph is not None
    assert res.graph.template == "entity_mentions"
    assert res.graph.intent == "entity"
    # entity star — 중심 엔티티 노드 + 언급 page spoke + MENTIONED_IN 다리.
    assert any(n.label == "Entity" for n in res.graph.nodes)
    assert any(e.rel == "MENTIONED_IN" for e in res.graph.edges)
    # grounding은 언급된 company page들에 제한된다(불변식 5).
    assert res.wiki is not None and res.wiki.sources
    assert all(s.page_slug in {"page-a", "page-b"} for s in res.wiki.sources)


def test_entity_question_unknown_name_demotes_to_wiki(graph_env, user_id):
    """name_norm이 company kg_entities에 없으면 bind가 None → wiki demote(3중 가드 동일). 존재하지
    않는 개체로 빈 graph 답을 내지 않는다(graph 미반환)."""
    _seed_entity_pages(user_id)
    run_rebuild()
    res = answer(
        user_id,
        "NonexistentThing을 언급한 페이지 알려줘",
        chat_model=_entity_chat(["NonexistentThing"]),
    )
    assert res.mode == "wiki"
    assert res.graph is None


def test_graph_grounding_excludes_distractor_page(graph_env, user_id):
    """invariant 5 (load-bearing): the graph answer grounds ONLY on the graph-resolved
    pages, never on a lexically-retrievable off-path page. A third company page ('page-z')
    whose title/definition/overview overlap the question text ('관계') is retrievable by the
    plain wiki path, but the retrieve(page_slugs=...) restriction must exclude it — so the
    grounding restriction cannot silently regress to a no-op (every other grounding test
    seeds only on-path pages, so this distractor makes the filter actually verifiable)."""
    _seed_two_linked_pages(user_id)
    # An off-path company page that lexically matches the question word '관계' but is NOT on
    # the page-y -> page-x KG path. The plain wiki retriever would happily return it.
    wiki_store.write_page(
        WikiPage(
            slug="page-z",
            title="page-x page-y 관계 설명",
            definition="page-x와 page-y의 관계를 다루는 페이지",
            overview="이 페이지는 page-x page-y 관계 개요를 담는다",
        ),
        user_id=user_id,
        scope="company",
        owner_id=None,
    )
    run_rebuild()
    res = answer(
        user_id, "page-x와 page-y는 무슨 관계야?", chat_model=_relation_chat(["page-x", "page-y"])
    )
    assert res.mode == "graph"
    assert res.wiki is not None and res.wiki.sources
    assert "page-z" not in {s.page_slug for s in res.wiki.sources}
    assert all(s.page_slug in {"page-x", "page-y"} for s in res.wiki.sources)


def test_resolve_subject_exact_title_tier(graph_env, user_id):
    """The exact-title resolution tier (subject matches a page TITLE, not its slug) has no
    e2e coverage — every relation test uses subject phrases equal to the slug. Seed a page
    whose title (not slug) equals the subject phrase and assert bind resolves to that slug."""
    wiki_store.write_page(
        WikiPage(
            slug="page-q",
            title="데이터 보관 정책",
            definition="page-q 정의",
            overview="page-q 개요",
        ),
        user_id=user_id,
        scope="company",
        owner_id=None,
    )
    _write_page(user_id, "page-x")
    binding = bind_graph_params(
        "데이터 보관 정책과 page-x는 무슨 관계야?",
        chat_model=_relation_chat(["데이터 보관 정책", "page-x"]),
    )
    assert binding is not None
    assert "page-q" in binding.params.values()


def test_resolve_subject_ambiguous_title_returns_none(graph_env, user_id):
    """The len(uniq)==1 ambiguity guard (the property preventing grounding on the WRONG
    page) has zero coverage. Seed TWO company pages with the SAME title; the duplicate-title
    subject must NOT resolve — neither dup-a nor dup-b ends up in params (guard returns None
    rather than silently picking the first row)."""
    for slug in ("dup-a", "dup-b"):
        wiki_store.write_page(
            WikiPage(
                slug=slug,
                title="중복 제목",
                definition=f"{slug} 정의",
                overview=f"{slug} 개요",
            ),
            user_id=user_id,
            scope="company",
            owner_id=None,
        )
    _write_page(user_id, "page-x")
    binding = bind_graph_params(
        "중복 제목과 page-x는 무슨 관계야?",
        chat_model=_relation_chat(["중복 제목", "page-x"]),
    )
    # The ambiguous subject resolves to nothing (n_unresolved>=1); only the unambiguous
    # page-x resolves, which on its own does not form a relation binding (demotes).
    if binding is not None:
        assert "dup-a" not in binding.params.values()
        assert "dup-b" not in binding.params.values()


def test_route_fallback_when_bind_fails(graph_env, user_id):
    """Graph candidate but subjects don't resolve → demote to wiki, no warning."""
    _seed_two_linked_pages(user_id)
    run_rebuild()
    res = answer(
        user_id,
        "page-x와 nonexistent-thing은 무슨 관계야?",
        chat_model=_relation_chat(["nonexistent-thing"]),
    )
    assert res.mode == "wiki"
    assert res.graph is None
    assert res.warnings == []


def test_route_fallback_when_kg_down(clean, user_id):
    """KG flag on but Neo4j unreachable → demote to wiki + 'kg_unavailable' warning,
    never raises (kg-impl §2.6/§7.4)."""
    settings = get_settings()
    settings.kg_enabled = True
    settings.kg_uri = "bolt://127.0.0.1:1"  # dead bolt port
    close_kg_driver()
    try:
        res = answer(
            user_id,
            "page-x와 page-y는 무슨 관계야?",
            chat_model=_relation_chat(["page-x", "page-y"]),
        )
    finally:
        settings.kg_enabled = False
        close_kg_driver()
    assert res.mode == "wiki"
    assert res.graph is None
    # The asker sees a fixed friendly message, NOT the raw 'kg_unavailable' gate token
    # (raw token stays on the audit span / fallback_reason telemetry only).
    assert res.warnings == [_GRAPH_DEGRADED_MESSAGE]
    assert "kg_unavailable" not in res.warnings


def test_route_graph_kg_off_no_warning(clean, user_id):
    """KG disabled (default) → graph candidate demotes to a plain wiki answer with NO
    warning (config, not degradation)."""
    res = answer(
        user_id, "page-x와 page-y는 무슨 관계야?", chat_model=_relation_chat(["page-x", "page-y"])
    )
    assert res.mode == "wiki"
    assert res.graph is None
    assert res.warnings == []


def test_graph_never_resolves_owner_scope_slug(graph_env, user_id):
    """A personal/owner-scope page must never be resolved as a graph subject (v1 graph
    is company-scope-only). The question demotes to wiki — pins the K7 boundary."""
    _write_page(user_id, "secret-personal", scope="personal", owner_id=user_id)
    # Seed the company subject so it genuinely resolves while secret-personal is dropped:
    # this exercises the two-named-one-resolves path (one personal/owner-scope subject
    # dropped, one real company page) and verifies the relation question demotes to wiki
    # rather than answering neighbors(page-x) as mode='graph'.
    _write_page(user_id, "page-x")
    run_rebuild()
    res = answer(
        user_id,
        "secret-personal과 page-x는 무슨 관계야?",
        chat_model=_relation_chat(["secret-personal", "page-x"]),
    )
    assert res.mode == "wiki"
    assert res.graph is None


def test_graph_grounding_failure_falls_open(graph_env, user_id, monkeypatch):
    """If grounding raises (e.g. PG hiccup between resolve and ground), the graph branch
    fails open to wiki — never a 500, never mode='graph' with no body."""
    _seed_two_linked_pages(user_id)
    run_rebuild()

    def _boom(*a, **k):
        raise RuntimeError("pg down mid-ground")

    monkeypatch.setattr("orthus.router.graph.retrieve", _boom)
    res = answer(
        user_id, "page-x와 page-y는 무슨 관계야?", chat_model=_relation_chat(["page-x", "page-y"])
    )
    assert res.mode == "wiki"
    assert res.graph is None


def test_graph_skipped_on_personal_node_federation(graph_env, user_id):
    """graph.py:108 is the SOLE structural guard enforcing company-scope-only /
    never-fires-under-federation. With node_kind='personal' + scope='all', try_graph_answer
    must return immediately (mode != 'graph', graph is None) and the federated wiki path is
    taken — exercising the guard that every company-node graph test bypasses."""
    _seed_two_linked_pages(user_id)
    run_rebuild()
    settings = get_settings()
    prev = settings.node_kind
    settings.node_kind = "personal"
    try:
        res = answer(
            user_id,
            "page-x와 page-y는 무슨 관계야?",
            scope="all",
            chat_model=_relation_chat(["page-x", "page-y"]),
        )
    finally:
        settings.node_kind = prev
    assert res.mode != "graph"
    assert res.graph is None


def test_graph_skipped_on_company_node_personal_scope(graph_env, user_id):
    """Company node but scope='personal' is outside _GRAPH_SCOPES, so the same structural
    guard demotes to wiki (graph is None)."""
    _seed_two_linked_pages(user_id)
    run_rebuild()
    res = answer(
        user_id,
        "page-x와 page-y는 무슨 관계야?",
        scope="personal",
        chat_model=_relation_chat(["page-x", "page-y"]),
    )
    assert res.mode == "wiki"
    assert res.graph is None


class _FakeDriver:
    def __init__(self):
        self.calls = 0

    def verify_connectivity(self):
        self.calls += 1


def test_kg_available_positive_cache(clean, monkeypatch):
    """K4b makes kg_available() a per-/ask hot-path call. A successful probe must be cached
    for _AVAIL_POS_TTL_S so a second call within the window does NOT re-probe;
    close_kg_driver() clears the cache so the next call re-probes."""
    from orthus.kg.client import _AVAIL_POS_TTL_S

    settings = get_settings()
    settings.kg_enabled = True
    close_kg_driver()
    fake = _FakeDriver()
    monkeypatch.setattr("orthus.kg.client.get_kg_driver", lambda: fake)
    base = 1000.0
    monkeypatch.setattr("orthus.kg.client.time.monotonic", lambda: base)
    try:
        assert kg_available() is True
        assert fake.calls == 1
        # Second call within _AVAIL_POS_TTL_S returns True without re-probing.
        assert kg_available() is True
        assert fake.calls == 1
        # Past the TTL the positive cache expires on its own (no close needed) → re-probe.
        # This locks the bounded fail-open property: a stale positive cannot outlive the TTL.
        monkeypatch.setattr("orthus.kg.client.time.monotonic", lambda: base + _AVAIL_POS_TTL_S + 1)
        assert kg_available() is True
        assert fake.calls == 2
        # close_kg_driver clears _avail_pos_until → next call re-probes.
        close_kg_driver()
        assert kg_available() is True
        assert fake.calls == 3
    finally:
        settings.kg_enabled = False
        close_kg_driver()


def test_kg_available_stale_positive_demotes(graph_env, user_id, monkeypatch):
    """Even with a (stale) positive availability, a down KG fails open via the graph branch:
    run_kg_template raises inside try_graph_answer and the question demotes to wiki within the
    bounded cap — never a 500, never mode='graph' (kg-impl §2.6 fail-open)."""
    _seed_two_linked_pages(user_id)
    run_rebuild()
    close_kg_driver()
    # Pin a fresh positive availability so kg_available() reports True without a real probe…
    monkeypatch.setattr("orthus.kg.client.get_kg_driver", lambda: _FakeDriver())
    assert kg_available() is True
    # …but the actual template run path is down: try_graph_answer must fail open to wiki.
    monkeypatch.setattr(
        "orthus.router.graph.run_kg_template",
        lambda **k: (_ for _ in ()).throw(RuntimeError("kg down mid-run")),
    )
    res = answer(
        user_id,
        "page-x와 page-y는 무슨 관계야?",
        chat_model=_relation_chat(["page-x", "page-y"]),
    )
    assert res.mode == "wiki"
    assert res.graph is None


def test_route_graph_demotes_when_no_groundable_pages(graph_env, user_id, monkeypatch):
    """invariant 5 defense: run_kg_template returns OK with nodes, but every node is
    non-groundable (placeholder / Document label), so _grounding_slugs yields nothing →
    demote to wiki (never mode='graph' with zero compiled-page sources)."""
    _seed_two_linked_pages(user_id)
    run_rebuild()

    ok_non_groundable = KgTemplateResult(
        status=KgQueryStatus.OK,
        nodes=[
            # placeholder WikiPage (materialized=False) and a Document — both non-groundable.
            KgGraphNode(id="page-x", label="WikiPage", slug="page-x", materialized=False),
            KgGraphNode(id="doc-1", label="Document", slug="page-y", materialized=True),
        ],
    )
    monkeypatch.setattr("orthus.router.graph.run_kg_template", lambda **k: ok_non_groundable)
    res = answer(
        user_id, "page-x와 page-y는 무슨 관계야?", chat_model=_relation_chat(["page-x", "page-y"])
    )
    assert res.mode == "wiki"
    assert res.graph is None


def test_route_graph_demotes_on_empty_grounding(graph_env, user_id, monkeypatch):
    """invariant 5 defense: valid groundable page_slugs, but the pages carry no indexed
    chunks (retrieve returns empty) → empty_grounding demote to wiki."""
    _seed_two_linked_pages(user_id)
    run_rebuild()

    ok_groundable = KgTemplateResult(
        status=KgQueryStatus.OK,
        nodes=[
            KgGraphNode(id="page-x", label="WikiPage", slug="page-x", materialized=True),
            KgGraphNode(id="page-y", label="WikiPage", slug="page-y", materialized=True),
        ],
    )
    monkeypatch.setattr("orthus.router.graph.run_kg_template", lambda **k: ok_groundable)
    monkeypatch.setattr("orthus.router.graph.retrieve", lambda *a, **k: [])
    res = answer(
        user_id, "page-x와 page-y는 무슨 관계야?", chat_model=_relation_chat(["page-x", "page-y"])
    )
    assert res.mode == "wiki"
    assert res.graph is None


def _write_claim(user_id, slug, *, conflicting=(), supporting=()):
    """A materialized company :WikiClaim with chunks (write_claim → _persist, embed_body)."""
    return wiki_store.write_claim(
        WikiClaim(
            slug=slug,
            claim=f"{slug} 주장 본문",
            confidence="high",
            last_reviewed=date(2026, 1, 1),
            evidence="근거 문장",
            conflicting=list(conflicting),
            supporting=list(supporting),
        ),
        user_id=user_id,
        scope="company",
    )


def _intent_chat(intent, subjects):
    """MockChat: classify→graph ('query router' needle) + bind extractor ('graph-query
    intent' needle) for conflict/provenance intents (no rule term routes them)."""
    subj_json = "[" + ",".join('"%s"' % s for s in subjects) + "]"
    return MockChat(
        rules=[
            ("query router", '{"route":"graph"}'),
            ("graph-query intent", '{"intent":"%s","subjects":%s}' % (intent, subj_json)),
        ],
        default="그래프 기반 답변입니다.",
    )


def test_provenance_intent_resolves_claim_not_page(graph_env, user_id):
    """conflict/provenance templates start from :WikiClaim, so their subjects must resolve
    to a kind='claim' slug — never a page. Pins the #1 fix (kind-aware resolution)."""
    _write_claim(user_id, "claim-fact-z")
    binding = bind_graph_params(
        "claim-fact-z 근거가 어디서 나왔어?",
        chat_model=_intent_chat("provenance", ["claim-fact-z"]),
    )
    assert binding is not None
    assert binding.template == "provenance_chain"
    assert binding.params["slug"] == "claim-fact-z"


def test_conflict_intent_resolves_claim(graph_env, user_id):
    """conflict intent binds conflicts_of with the resolved claim slug."""
    _write_claim(user_id, "claim-fact-z")
    binding = bind_graph_params(
        "claim-fact-z와 충돌하는 게 뭐야?",
        chat_model=_intent_chat("conflict", ["claim-fact-z"]),
    )
    assert binding is not None
    assert binding.template == "conflicts_of"
    assert binding.params["slug"] == "claim-fact-z"


def test_provenance_page_subject_does_not_bind_page(graph_env, user_id):
    """A provenance question whose only candidate is a PAGE (no matching claim) must NOT
    mis-resolve the page; binding is None → demote to wiki, not a dead claim query."""
    _write_page(user_id, "only-a-page")
    binding = bind_graph_params(
        "only-a-page 근거가 어디서 나왔어?",
        chat_model=_intent_chat("provenance", ["only-a-page"]),
    )
    assert binding is None


def test_route_conflict_question_grounds_on_claims(graph_env, user_id):
    """End-to-end: a conflict question resolves a claim, runs conflicts_of over Neo4j, and
    grounds on the compiled claim pages → mode='graph'. Before the #1 fix this always
    demoted (page-kind resolution could never match a :WikiClaim start node)."""
    _write_claim(user_id, "claim-a", conflicting=["claim-b"])
    _write_claim(user_id, "claim-b")
    run_rebuild()
    # Phrased to hit NO rule term (no "…주장" graph anchor, no "뭐야"/"무엇"/"설명" wiki term)
    # so classify falls through to the LLM `graph` enum — the production path for a conflict
    # question that doesn't happen to contain a "…주장" anchor. The _intent_chat "query router"
    # needle returns {"route":"graph"}, then the bind needle extracts intent=conflict.
    res = answer(
        user_id, "claim-a와 대립하는 입장이 있어?", chat_model=_intent_chat("conflict", ["claim-a"])
    )
    assert res.mode == "graph"
    assert res.graph is not None
    assert res.graph.template == "conflicts_of"
    assert res.graph.intent == "conflict"
    assert res.wiki is not None and res.wiki.sources
    assert all(s.page_slug in {"claim-a", "claim-b"} for s in res.wiki.sources)


def test_conflict_rule_anchors_route_without_overcapture():
    """Kept multi-word '…주장' conflict anchors route to graph; the removed bare fragments
    (과충돌/모순되는/…) must NOT over-capture ordinary explanatory wiki asks."""
    assert _rule_based_route("이 정책과 상충하는 주장은?") == "graph"
    assert _rule_based_route("그 결정과 충돌하는 주장 있어?") == "graph"
    assert _rule_based_route("앞 내용과 모순되는 주장 정리") == "graph"
    # No "…주장" anchor → these stay wiki (previously '과충돌'/'모순되는' over-captured them).
    assert _rule_based_route("기존 동작과 충돌하지 않는지 설명해줘") == "wiki"
    assert _rule_based_route("앞 문단과 모순되는 것 같은데 정리해줘") == "wiki"


def test_entity_rule_anchors_route_without_overcapture():
    """K9.3a — '…를 언급한/된 페이지·곳', '어디서/누가 언급', '엮인 페이지' entity 앵커는 graph로
    라우팅(CANDIDATE — named 개체가 kg_entities에 없으면 bind가 wiki로 안전 demote). 단독 '언급'/
    '엮인'은 일반 설명 wiki 질문을 과포획하지 않는다(다단어 앵커만 둔다)."""
    assert _rule_based_route("NOVA를 언급한 페이지 보여줘") == "graph"
    assert _rule_based_route("김대표가 언급된 곳은?") == "graph"
    assert _rule_based_route("이 프로젝트와 엮인 페이지") == "graph"
    # 단독 '언급'은 앵커 아님 — 설명 질문은 wiki(_WIKI_TERMS '설명') 유지.
    assert _rule_based_route("이 정책을 언급한 부분을 설명해줘") == "wiki"


def test_entity_mention_ending_anchors_route_without_overcapture():
    """그래프 사각지대 entity 어미 앵커(docs/routing-graph-follow-up.md §8) — "X가 어디에
    나오나/다뤄지나" 골든 3개 군집이 규칙만으로 graph에 도달한다(LLM/모델 무관). 단독
    "나오"/"다뤄"/연결어미 "-는데"와 제외한 후보("나오는페이지"/"페이지에서다뤄"/"맥락에서다뤄")가
    잡던 일반 wiki 질문은 과포획하지 않는다."""
    # 군집 A — "어디어디에 나오"
    assert _rule_based_route("CapCut은 어디어디에 나오나요") == "graph"
    # 군집 B — "다뤄지는 데(들)가 어디" (장소 의문사 "+어디" 결합형)
    assert _rule_based_route("Go가 다뤄지는 데가 어디인가요") == "graph"
    assert _rule_based_route("Sunsama가 다뤄지는 데들이 어디인가요") == "graph"
    # 군집 C — "페이지(들)·맥락에서 다뤄지"
    assert _rule_based_route("Nova는 어떤 페이지들에서 다뤄지나요") == "graph"
    assert _rule_based_route("I2V는 어떤 페이지나 맥락에서 다뤄지고 있나요?") == "graph"
    assert _rule_based_route("Script AI는 어느 페이지 또는 맥락에서 다뤄지고 있나요?") == "graph"
    # 연결어미 "-는데"는 앵커 아님(군집 A/B는 "+어디" 결합형만) — 설명/요약 wiki 질문 유지.
    assert _rule_based_route("이 정책이 다뤄지는데 자세히 설명해줘") == "wiki"
    assert _rule_based_route("회의에서 다뤄진 내용 요약해줘") == "wiki"
    # 제외한 후보("페이지에서다뤄"/"맥락에서다뤄"/"나오는페이지")가 과포획하던 케이스.
    assert _rule_based_route("온보딩 페이지에서 다뤄지는 내용 요약해줘") == "wiki"
    assert _rule_based_route("이 맥락에서 다뤄진 논점 설명해줘") == "wiki"
    assert _rule_based_route("결과가 잘 나오는 페이지 설정 방법 설명해줘") == "wiki"


def test_group_c_page_anchor_accepted_overcapture_is_pinned():
    """군집 C 앵커("페이지들에서다뤄")는 "+어디" 가드가 없어 요약형 wiki 질문을 graph로 끄는
    수용된 과포획이 있다(route.py 주석 문서화). 페이지 열거 의미라 준-정답 + bind 미스 시 wiki로
    안전 demote한다. 이 동작을 고정해, 앵커를 "+어디"로 좁히는 후속 변경이 조용히 회귀하지 않게
    한다(code-review finding #4)."""
    assert _rule_based_route("위키 페이지들에서 다뤄지는 주제 요약해줘") == "graph"


def test_relation_same_page_phrasings_bind_neighbors(graph_env, user_id):
    """Two phrasings that resolve to the SAME page (dedup, n_unresolved==0) must still bind
    neighbors — not demote. Distinguishes benign dedup from a failed-resolution."""
    _seed_two_linked_pages(user_id)
    run_rebuild()
    res = answer(
        user_id,
        "page-x와 페이지 page-x는 무슨 관계야?",  # 'page-x' (slug) + '페이지 page-x' (title) → page-x
        chat_model=_relation_chat(["page-x", "페이지 page-x"]),
    )
    assert res.mode == "graph"
    assert res.graph is not None
    assert res.graph.template == "neighbors"


def test_relation_unresolved_named_subject_with_context_demotes(graph_env, user_id):
    """A named subject that does NOT resolve must demote even when a context_wiki_slug fills
    the slot — the user named something we could not find, so neighbors-of-context would
    answer a different question (n_unresolved>0 → demote). Pins the #5 fix."""
    _seed_two_linked_pages(user_id)
    run_rebuild()
    res = answer(
        user_id,
        "이것과 NonexistentThing은 무슨 관계야?",
        chat_model=_relation_chat(["NonexistentThing"]),
        context_wiki_slug="page-x",
    )
    assert res.mode == "wiki"
    assert res.graph is None


def test_existing_routes_no_regression():
    """Adding the graph route must not flip any existing routing. The graph terms are
    disjoint from existing fixture questions, which keep their non-graph routes."""
    fixtures = [
        ("아크메 직원 목록", "structured"),
        ("협업업무표 상태별 건수", "structured"),
        ("티켓 상태별 개수", "structured"),
        ("온보딩 절차는 어떻게 진행되나요?", "wiki"),
        ("Atlas 정산 화면 금액 제대로 반영되도록 하기 작업은 뭐야?", "wiki"),
    ]
    for question, expected in fixtures:
        assert _rule_based_route(question) == expected
        compact = question.lower().replace(" ", "")
        assert not any(t.replace(" ", "") in compact for t in _GRAPH_TERMS)


def _write_claim_k8(user_id, slug, *, conflicting=()):
    return wiki_store.write_claim(
        WikiClaim(
            slug=slug,
            claim=f"{slug} 주장",
            supporting=[],
            conflicting=list(conflicting),
            confidence="high",
            last_reviewed=date(2026, 6, 1),
            evidence="근거",
        ),
        user_id=user_id,
    )


def test_route_graph_conflict_page_resolve(graph_env, user_id):
    """K8.3 (B3) — conflict intent가 claim으로 resolve되지 않으면 page로 fallback해
    page_conflicts를 바인딩한다(claim 우선 → page fallback). 토픽/페이지 표현이 그 페이지
    claim들의 모순을 노출한다. 기존 claim-rooted conflicts_of 경로는 무회귀."""
    _write_claim_k8(user_id, "claim-a", conflicting=["claim-b"])
    _write_claim_k8(user_id, "claim-b")
    _write_page(user_id, "nova-retro", backlinks=["claim-a"])
    run_rebuild()
    chat = MockChat(
        rules=[
            ("query router", '{"route":"graph"}'),
            ("graph-query intent", '{"intent":"conflict","subjects":["nova-retro"]}'),
        ],
        default="검토 결과 미해소 모순이 있습니다.",
    )
    res = answer(user_id, "nova-retro에 모순 있어?", chat_model=chat)
    assert res.mode == "graph"
    assert res.graph is not None
    assert res.graph.template == "page_conflicts"
    assert res.graph.intent == "conflict"


def test_route_graph_conflict_claim_resolve_unchanged(graph_env, user_id):
    """K8.3 무회귀 — conflict 주어가 claim으로 resolve되면 기존 conflicts_of를 그대로 바인딩
    (claim 우선; page fallback이 claim 경로를 가로채지 않는다)."""
    _write_claim_k8(user_id, "claim-a", conflicting=["claim-b"])
    _write_claim_k8(user_id, "claim-b")
    run_rebuild()
    binding = bind_graph_params(
        "claim-a와 충돌하는주장",
        caller_id=user_id,
        chat_model=MockChat(
            rules=[("graph-query intent", '{"intent":"conflict","subjects":["claim-a"]}')],
            default="x",
        ),
    )
    assert binding is not None
    assert binding.template == "conflicts_of"
    assert binding.params["slug"] == "claim-a"


def test_route_graph_conflict_page_fallback_owner_inclusive(graph_env, user_id):
    """코드리뷰 #3 — conflict page-fallback이 1차 resolve와 **대칭**으로 owner-inclusive다.
    owner-scope ON + caller면 호출자 본인 personal 페이지가 토픽 표현으로 page_conflicts에
    바인딩된다. (claim으로는 resolve 안 되는 personal page slug → page fallback 진입.)"""
    _write_page(user_id, "my-personal-retro", scope="personal", owner_id=user_id)
    chat = MockChat(
        rules=[
            ("graph-query intent", '{"intent":"conflict","subjects":["my-personal-retro"]}'),
        ],
        default="x",
    )
    binding = bind_graph_params(
        "my-personal-retro에 모순 있어?",
        caller_id=user_id,
        owner_scope=True,
        chat_model=chat,
    )
    assert binding is not None
    assert binding.template == "page_conflicts"
    assert binding.params["slug"] == "my-personal-retro"


def test_route_graph_conflict_page_fallback_company_only_when_flag_off(graph_env, user_id):
    """코드리뷰 #3 boundary — owner-scope OFF면 page-fallback은 company-only다. 호출자의
    personal 페이지는 resolve되지 않아 binding이 None(demote). flag-OFF 무회귀 보장."""
    _write_page(user_id, "my-personal-retro", scope="personal", owner_id=user_id)
    chat = MockChat(
        rules=[
            ("graph-query intent", '{"intent":"conflict","subjects":["my-personal-retro"]}'),
        ],
        default="x",
    )
    binding = bind_graph_params(
        "my-personal-retro에 모순 있어?",
        caller_id=user_id,
        owner_scope=False,
        chat_model=chat,
    )
    assert binding is None


def test_route_conflict_kg_down_warns_conflict_view_unavailable(graph_env, user_id, monkeypatch):
    """K8.3 (D6) — a conflict question during a Neo4j outage (the dominant degradation) must
    carry the honest `conflict_view_unavailable` token so the demoted plain-wiki answer isn't
    misread as "no conflicts". bind now runs BEFORE the availability check (graph.py), so the
    intent is known at demote time; the token then survives router.answer's warning rebuild
    (it is an FE-contract token, not a raw gate token). Before the D6 fix this path emitted
    only a generic warning and the FE banner was unreachable."""
    _write_claim_k8(user_id, "claim-a", conflicting=["claim-b"])
    _write_claim_k8(user_id, "claim-b")
    # KG flag on (graph_env) but availability probe reports down. bind (LLM+PG only) still
    # resolves claim-a, so binding.intent == 'conflict' at the demote.
    monkeypatch.setattr("orthus.router.graph.kg_available", lambda: False)
    res = answer(
        user_id,
        "claim-a와 충돌하는 주장 있어?",
        chat_model=_intent_chat("conflict", ["claim-a"]),
    )
    assert res.mode == "wiki"
    assert res.graph is None
    assert _GRAPH_DEGRADED_MESSAGE in res.warnings
    assert "conflict_view_unavailable" in res.warnings
    # The raw gate token stays suppressed from the asker-facing warnings (telemetry only).
    assert "kg_unavailable" not in res.warnings


def test_route_conflict_kg_down_unresolved_subject_still_warns(graph_env, user_id, monkeypatch):
    """K8.3 (D6, 코드리뷰) — conflict 질문이 어떤 claim/page로도 resolve되지 않아 binding=None이
    되어도, Neo4j 다운 시 conflict_view_unavailable을 띄운다. bind_graph_params가 intent_out으로
    detected intent를 흘려보내므로 binding=None 경로가 silence-as-all-clear로 빠지지 않는다(기존
    kg-down 테스트는 resolve되는 주어를 써서 이 경로가 미테스트였다)."""
    # 아무 claim/page도 seed하지 않음 → 주어가 claim·page 어느 것으로도 resolve 안 됨 → binding None.
    monkeypatch.setattr("orthus.router.graph.kg_available", lambda: False)
    res = answer(
        user_id,
        "어제 회의에서 안 맞는 부분 있어?",
        chat_model=_intent_chat("conflict", ["어제 회의"]),
    )
    assert res.mode == "wiki"
    assert res.graph is None
    assert "conflict_view_unavailable" in res.warnings  # binding=None이어도 honest 토큰
    assert "kg_unavailable" not in res.warnings


def test_route_conflict_gate_reject_warns_conflict_view_unavailable(
    graph_env, user_id, monkeypatch
):
    """K8.3 (D6) — a conflict query that BOUND but failed at the gate (resolve→exec race,
    timeout, …) also surfaces `conflict_view_unavailable`. Pins the post-bind gate-reject arm
    end-to-end: graph.py appends the token → router preserves it in RoutedAnswer.warnings while
    still suppressing the raw reject token."""
    _write_claim_k8(user_id, "claim-a", conflicting=["claim-b"])
    _write_claim_k8(user_id, "claim-b")
    run_rebuild()
    # kg_available() True, but the actual template run rejects — the gate_reject demote arm.
    monkeypatch.setattr(
        "orthus.router.graph.run_kg_template",
        lambda **k: KgTemplateResult(status=KgQueryStatus.TIMEOUT, reject_reason="timeout"),
    )
    res = answer(
        user_id,
        "claim-a와 충돌하는 주장 있어?",
        chat_model=_intent_chat("conflict", ["claim-a"]),
    )
    assert res.mode == "wiki"
    assert res.graph is None
    assert _GRAPH_DEGRADED_MESSAGE in res.warnings
    assert "conflict_view_unavailable" in res.warnings
    assert "timeout" not in res.warnings  # raw gate reject token suppressed


def test_route_graph_conflict_page_zero_returns_grounded(graph_env, user_id):
    """K8.3 (D6/FE5) — a conflict question on a page with NO unresolved conflicts returns a
    GROUNDED mode='graph' answer (page_conflicts OPTIONAL MATCH returns the page node, so
    _grounding_slugs is non-empty), NOT a demote. This is the contract the FE '미해소 모순이
    없습니다' copy depends on — a grounded "no conflicts" is distinct from a degraded
    'conflict_view_unavailable' demote, which must never read as all-clear."""
    _write_claim_k8(user_id, "claim-solo")  # no conflicting= → zero CONFLICTS_WITH edges
    _write_page(user_id, "calm-retro", backlinks=["claim-solo"])
    run_rebuild()
    chat = MockChat(
        rules=[
            ("query router", '{"route":"graph"}'),
            ("graph-query intent", '{"intent":"conflict","subjects":["calm-retro"]}'),
        ],
        default="확인한 페이지에 미해소 모순이 없습니다.",
    )
    res = answer(user_id, "calm-retro에 모순 있어?", chat_model=chat)
    assert res.mode == "graph"
    assert res.graph is not None
    assert res.graph.template == "page_conflicts"
    # Grounded on the page itself (not demoted) and carries no degraded/conflict warning.
    assert res.wiki is not None and res.wiki.sources
    assert "conflict_view_unavailable" not in res.warnings
