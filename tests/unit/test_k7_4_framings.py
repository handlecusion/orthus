"""K7.4 — owner-scope two-framing(`run_path_framings`) + missing_link owner extension.

**neo4j 불필요(§6 non-neo4j-gated 핀):** `run_kg_template`/`caller_has_personal_pages`/
`kg_*_enabled`을 monkeypatch해 오케스트레이션(short-circuit/dual-path/fail-open/row 예산),
company-anchor anti-flood 가드, cross-scope 중립 카피(L7), user_id threading, span allowlist를
PG/메모리 평면만으로 고정한다. 이 가드들은 CI의 neo4j 서비스 컨테이너가 없어도 그대로 돌아
build를 떨어뜨린다(fingerprint/leak 방어가 neo4j-부재 lane에서 no-op로 증발하지 않게).
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from orthus.kg import gate
from orthus.kg.gate import (
    _FRAMING_A_LABEL,
    _FRAMING_B_LABEL,
    KgQueryStatus,
    KgTemplateResult,
    _framing_from_result,
    _framings_demote_cause,
    run_path_framings,
)
from orthus.schemas.canonical import (
    KgFramingsSpanMeta,
    KgGraphEdge,
    KgGraphNode,
    WikiGap,
    WikiSourceRef,
)
from orthus.wiki.gap import _CROSS_SCOPE_MISSING_LINK_MESSAGE, _build_message, maybe_missing_link


# --- fixtures / helpers -------------------------------------------------------------


def _co_node(slug: str) -> KgGraphNode:
    return KgGraphNode(id=slug, label="WikiPage", slug=slug, scope="company")


def _pers_node(slug: str) -> KgGraphNode:
    return KgGraphNode(id=slug, label="WikiPage", slug=slug, scope="personal", is_own_personal=True)


def _ok(nodes, edges=None, truncated=False) -> KgTemplateResult:
    return KgTemplateResult(
        status=KgQueryStatus.OK, nodes=nodes, edges=edges or [], truncated=truncated
    )


def _empty() -> KgTemplateResult:
    return KgTemplateResult(status=KgQueryStatus.OK, nodes=[])


def _err() -> KgTemplateResult:
    return KgTemplateResult(status=KgQueryStatus.ERROR, reject_reason="driver_error:Boom")


class _Recorder:
    """run_kg_template stub — template별 canned 결과를 돌려주고 호출 순서를 기록한다."""

    def __init__(self, by_template: dict):
        self.by_template = by_template
        self.calls: list[str] = []
        self.user_ids: list = []

    def __call__(self, *, user_id, template_name, params, correlation_id=None, limit=None):
        self.calls.append(template_name)
        self.user_ids.append(user_id)
        res = self.by_template[template_name]
        return res() if callable(res) else res


@pytest.fixture
def caller():
    return uuid.uuid4()


def _patch(monkeypatch, recorder, *, has_personal: bool):
    monkeypatch.setattr(gate, "run_kg_template", recorder)
    monkeypatch.setattr(gate, "caller_has_personal_pages", lambda cid, **kw: has_personal)


# --- _framing_from_result (서버 파생 personal_dependent) -----------------------------


def test_framing_personal_dependent_from_node_scope():
    f = _framing_from_result(_FRAMING_B_LABEL, _ok([_co_node("a"), _pers_node("b")]))
    assert f.personal_dependent is True
    assert f.same_as_direct is False
    assert f.path_slugs == ["a", "b"]


def test_framing_personal_dependent_from_edge_scope():
    # 모든 노드가 company여도 personal **엣지**가 있으면 personal_dependent True(edge-inclusive).
    edge = KgGraphEdge(src="a", dst="b", rel="BACKLINK", scope="personal")
    f = _framing_from_result(_FRAMING_B_LABEL, _ok([_co_node("a"), _co_node("b")], edges=[edge]))
    assert f.personal_dependent is True


def test_framing_all_company_is_same_as_direct():
    f = _framing_from_result(_FRAMING_A_LABEL, _ok([_co_node("a"), _co_node("b")]))
    assert f.personal_dependent is False
    assert f.same_as_direct is True


# --- run_path_framings — short-circuit (caller has no personal pages) ----------------


def test_short_circuit_runs_framing_a_only_synthesizes_b(monkeypatch, caller):
    rec = _Recorder({"path_between_company": _ok([_co_node("a"), _co_node("b")])})
    _patch(monkeypatch, rec, has_personal=False)
    out = run_path_framings(user_id=caller, slug_a="a", slug_b="b", max_hops=4)
    assert out is not None
    # 1 row 예산 — framing A만 실행(B 합성)
    assert rec.calls == ["path_between_company"]
    labels = [f.label for f in out.framings.framings]
    assert labels == [_FRAMING_A_LABEL, _FRAMING_B_LABEL]
    framing_b = out.framings.framings[1]
    assert framing_b.same_as_direct is True
    assert framing_b.personal_dependent is False
    # spine = framing A 내용(all-company)
    assert out.spine.path_slugs == ["a", "b"]


def test_short_circuit_empty_path_demotes(monkeypatch, caller):
    rec = _Recorder({"path_between_company": _empty()})
    _patch(monkeypatch, rec, has_personal=False)
    assert run_path_framings(user_id=caller, slug_a="a", slug_b="b", max_hops=4) is None
    assert rec.calls == ["path_between_company"]


# --- run_path_framings — dual-path (caller has personal pages) -----------------------


def test_dual_path_personal_bridge(monkeypatch, caller):
    # framing A all-company, framing B owner-variant가 personal hop을 경유 → personal_dependent.
    rec = _Recorder(
        {
            "path_between_company": _ok([_co_node("a"), _co_node("b")]),
            "path_between": _ok([_co_node("a"), _pers_node("mine"), _co_node("b")]),
        }
    )
    _patch(monkeypatch, rec, has_personal=True)
    out = run_path_framings(user_id=caller, slug_a="a", slug_b="b", max_hops=4)
    assert out is not None
    # 2 row 예산 — A + B
    assert rec.calls == ["path_between_company", "path_between"]
    framing_b = out.framings.framings[1]
    assert framing_b.label == _FRAMING_B_LABEL
    assert framing_b.personal_dependent is True
    assert framing_b.same_as_direct is False
    # spine = framing B(owner-visible) — personal bridge 노드 포함
    assert "mine" in out.spine.path_slugs


def test_dual_path_b_coincides_with_a(monkeypatch, caller):
    # caller가 personal 페이지를 가져도 owner-variant가 all-company 경로를 반환하면 A==B →
    # personal_dependent False(끝점 scope가 아니라 **반환 scope**에서 파생, 오라벨 금지).
    rec = _Recorder(
        {
            "path_between_company": _ok([_co_node("a"), _co_node("b")]),
            "path_between": _ok([_co_node("a"), _co_node("b")]),
        }
    )
    _patch(monkeypatch, rec, has_personal=True)
    out = run_path_framings(user_id=caller, slug_a="a", slug_b="b", max_hops=4)
    framing_b = out.framings.framings[1]
    assert framing_b.personal_dependent is False
    assert framing_b.same_as_direct is True


def test_dual_path_b_empty_demotes(monkeypatch, caller):
    # B가 spine — B empty면 A 성공 여부 무관 demote(§2 step 5).
    rec = _Recorder(
        {"path_between_company": _ok([_co_node("a"), _co_node("b")]), "path_between": _empty()}
    )
    _patch(monkeypatch, rec, has_personal=True)
    assert run_path_framings(user_id=caller, slug_a="a", slug_b="b", max_hops=4) is None
    assert rec.calls == ["path_between_company", "path_between"]


def test_framings_demote_cause_distinguishes_error_from_empty():
    # §1 observability — demote cause는 transient gate 오류와 genuine empty path를 구분하되
    # coarse 카테고리만(slug/detail 미포함, span allowlist 안전).
    assert _framings_demote_cause(_empty()) == "empty_path"
    assert _framings_demote_cause(_err()) == "driver_error"  # "driver_error:Boom"의 `:` 앞


def test_dual_path_a_error_b_alone(monkeypatch, caller):
    # A error + B ok → framings=[B] only(A 단독 제시), B spine. row 예산 = 2(per-attempt).
    rec = _Recorder(
        {"path_between_company": _err(), "path_between": _ok([_co_node("a"), _co_node("b")])}
    )
    _patch(monkeypatch, rec, has_personal=True)
    out = run_path_framings(user_id=caller, slug_a="a", slug_b="b", max_hops=4)
    assert out is not None
    assert [f.label for f in out.framings.framings] == [_FRAMING_B_LABEL]
    assert len(rec.calls) == 2


def test_run_path_framings_threads_session_user_id(monkeypatch, caller):
    rec = _Recorder(
        {
            "path_between_company": _ok([_co_node("a"), _co_node("b")]),
            "path_between": _ok([_co_node("a"), _co_node("b")]),
        }
    )
    _patch(monkeypatch, rec, has_personal=True)
    run_path_framings(user_id=caller, slug_a="a", slug_b="b", max_hops=4)
    assert all(uid == caller for uid in rec.user_ids)


def test_run_path_framings_fail_open_on_exception(monkeypatch, caller):
    def _boom(*a, **k):
        raise RuntimeError("neo4j exploded")

    monkeypatch.setattr(gate, "run_kg_template", _boom)
    monkeypatch.setattr(gate, "caller_has_personal_pages", lambda cid, **kw: True)
    # 절대 raise 안 함 — None으로 수렴(graph.py wiki demote).
    assert run_path_framings(user_id=caller, slug_a="a", slug_b="b", max_hops=4) is None


# --- span allowlist (구조적 가드 — slug/title/owner_id 차단) --------------------------


def test_framings_span_meta_forbids_unknown_keys():
    # typed allowlist — slug/title/owner_id를 emit 시점에 거부(enumerated absence보다 강함).
    with pytest.raises(ValidationError):
        KgFramingsSpanMeta(framing_count=2, slug="leaked-company-page")
    with pytest.raises(ValidationError):
        KgFramingsSpanMeta(framing_count=2, owner_id="some-uuid")
    # 허용 키만으로는 정상
    meta = KgFramingsSpanMeta(
        framing_count=2, personal_dependent=True, same_as_direct=False, short_circuit=False
    )
    assert set(meta.model_dump()) == {
        "framing_count",
        "personal_dependent",
        "same_as_direct",
        "short_circuit",
        "demote_cause",
    }


# --- maybe_missing_link — owner extension (monkeypatched gate, no neo4j) -------------


def _hit(score: float, slug: str = "p", scope: str = "company") -> WikiSourceRef:
    return WikiSourceRef(
        page_slug=slug, title="P", kind="page", excerpt="…", score=score, source_scope=scope
    )


def _insufficient_gap() -> WikiGap:
    return WikiGap(
        detected=True,
        reason="insufficient_grounding",
        missing_topic="A와 B의 관계",
        top_score=1.0,
        message=_build_message("A와 B의 관계", "insufficient_grounding"),
    )


@pytest.fixture
def _ml_env(monkeypatch):
    """maybe_missing_link을 central + KG-on + owner-scope-on으로 고정하고 gate를 stub한다."""
    monkeypatch.setattr("orthus.wiki.gap._is_personal", lambda: False)
    monkeypatch.setattr("orthus.kg.client.kg_enabled", lambda: True)
    monkeypatch.setattr("orthus.kg.client.kg_owner_scope_enabled", lambda: True)
    rec = _Recorder({"path_between": _empty()})  # 기본: 경로 없음 → missing_link 발화
    monkeypatch.setattr("orthus.kg.gate.run_kg_template", rec)
    return rec


def test_personal_personal_short_circuits_without_gate_call(_ml_env, monkeypatch):
    # company-anchor anti-flood 가드 — top-2가 둘 다 personal이면 **gate 호출 없이** 차단.
    hits = [_hit(1.0, "p1", "personal"), _hit(0.9, "p2", "personal")]
    out = maybe_missing_link(_insufficient_gap(), hits, user_id=uuid.uuid4())
    assert out.reason == "insufficient_grounding"  # 미발화
    assert _ml_env.calls == []  # Neo4j 왕복 0


def test_cross_scope_fires_with_neutral_slug_free_copy(_ml_env):
    caller = uuid.uuid4()
    hits = [_hit(1.0, "co-secret-strategy", "company"), _hit(0.9, "my-note", "personal")]
    out = maybe_missing_link(_insufficient_gap(), hits, user_id=caller)
    assert out.reason == "missing_link"
    assert out.message == _CROSS_SCOPE_MISSING_LINK_MESSAGE
    # L7 — 회사 페이지 slug/title이 메시지에 들어가지 않는다(title-leak 표면 0).
    assert "co-secret-strategy" not in out.message
    assert "my-note" not in out.message
    # gate에 세션 user_id가 thread됨(hit에서 재유도 아님).
    assert _ml_env.user_ids == [caller]


def test_company_company_uses_generic_copy(_ml_env):
    hits = [_hit(1.0, "co-a", "company"), _hit(0.9, "co-b", "company")]
    out = maybe_missing_link(_insufficient_gap(), hits, user_id=uuid.uuid4())
    assert out.reason == "missing_link"
    assert out.message == _build_message("A와 B의 관계", "missing_link")
    assert out.message != _CROSS_SCOPE_MISSING_LINK_MESSAGE


def test_cross_scope_suppressed_when_owner_path_exists(_ml_env, monkeypatch):
    # owner-variant path_between이 경로를 반환하면(owner hop으로 이어짐) missing_link 미발화.
    monkeypatch.setattr(
        "orthus.kg.gate.run_kg_template",
        _Recorder({"path_between": _ok([_co_node("co-a"), _pers_node("my-note")])}),
    )
    hits = [_hit(1.0, "co-a", "company"), _hit(0.9, "my-note", "personal")]
    out = maybe_missing_link(_insufficient_gap(), hits, user_id=uuid.uuid4())
    assert out.reason == "insufficient_grounding"  # 경로 존재 → suppress


def test_node_kind_personal_early_returns_unchanged(monkeypatch):
    # _is_personal() True(personal node, Neo4j 없음) → 변경 없이 즉시 반환(가드 불변).
    monkeypatch.setattr("orthus.wiki.gap._is_personal", lambda: True)
    called = []
    monkeypatch.setattr("orthus.kg.gate.run_kg_template", lambda **k: called.append(1) or _empty())
    out = maybe_missing_link(
        _insufficient_gap(), [_hit(1.0, "a"), _hit(0.9, "b")], user_id=uuid.uuid4()
    )
    assert out.reason == "insufficient_grounding"
    assert called == []


# --- candidate / read predicates (pure SQL fragments, no DB) -------------------------


def _compiled(expr):
    return str(expr.compile(compile_kwargs={"literal_binds": True}))


def test_owner_inclusive_read_where_caller_branch():
    from orthus.kg.visibility import owner_inclusive_read_where
    from orthus.tables import wiki_pages

    cid = uuid.uuid4()
    sql = _compiled(owner_inclusive_read_where(wiki_pages.c.scope, wiki_pages.c.owner_id, cid))
    assert "scope = 'company'" in sql
    assert f"owner_id = '{cid.hex}'" in sql or f"owner_id = '{cid}'" in sql
    assert " OR " in sql


def test_owner_inclusive_read_where_null_caller_is_company_only():
    from orthus.kg.visibility import owner_inclusive_read_where
    from orthus.tables import wiki_pages

    sql = _compiled(owner_inclusive_read_where(wiki_pages.c.scope, wiki_pages.c.owner_id, None))
    assert sql == "wiki_pages.scope = 'company'"  # owner 비교 자체를 안 만든다(B-null-caller)


def test_relation_candidate_filter_owner_inclusive_only_for_page_and_flag_on():
    from orthus.router.graph import _relation_candidate_filter

    cid = uuid.uuid4()
    # page + owner-scope ON → owner-inclusive
    assert " OR " in _compiled(_relation_candidate_filter("page", cid, True)[0])
    # claim intent → company-only(decision a)
    assert (
        _compiled(_relation_candidate_filter("claim", cid, True)[0])
        == "wiki_pages.scope = 'company'"
    )
    # page + flag OFF → company-only
    assert (
        _compiled(_relation_candidate_filter("page", cid, False)[0])
        == "wiki_pages.scope = 'company'"
    )
    # page + owner ON but caller None → company-only(B-null-caller)
    assert (
        _compiled(_relation_candidate_filter("page", None, True)[0])
        == "wiki_pages.scope = 'company'"
    )


# --- G2 (§6 merge-blocking): confused-deputy — $caller는 세션 user, hit에서 재유도 불가 ---------
#
# **neo4j 불필요(§6 G3 핀):** 아래 G2 가드는 monkeypatched gate(`_ml_env`)와 순수 스키마
# introspection만 쓰므로 neo4j 서비스 컨테이너가 없는 lane에서도 그대로 돈다 — leak 방어가
# skip으로 증발하지 않는다(이 파일 모듈 docstring 참조).


def test_wiki_source_ref_carries_no_owner_id():
    # 구조적 보장(enumerated absence보다 강함): WikiSourceRef는 owner_id/owner를 **필드로 갖지
    # 않는다** → maybe_missing_link/_top_company_page_slugs가 hit에서 $caller로 재유도할 owner_id
    # 자체가 없다(confused-deputy 표면 0). hit는 page_slug/source_scope만 노출한다.
    assert "owner_id" not in WikiSourceRef.model_fields
    assert "owner" not in WikiSourceRef.model_fields


def test_caller_is_session_user_never_redrived_from_hit(_ml_env):
    # 적대적: top-2 = [회사 anchor, 제3자처럼 보이는 personal 노트], 세션 caller는 또 다른 유저.
    # owner-variant gate에 thread되는 user_id는 **세션 caller**여야 하고, hit 슬러그/내용에서
    # 유도된 어떤 값도 아니어야 한다(retrieve가 foreign-personal을 섞더라도 caller는 불변).
    session_caller = uuid.uuid4()
    hits = [_hit(1.0, "co-policy", "company"), _hit(0.9, "looks-like-someone-else", "personal")]
    out = maybe_missing_link(_insufficient_gap(), hits, user_id=session_caller)
    assert out.reason == "missing_link"
    # 단일 owner — gate는 정확히 세션 caller로만 호출된다(다른 어떤 id도 아님).
    assert _ml_env.user_ids == [session_caller]


def test_null_caller_company_company_threads_none(_ml_env):
    # B-null-caller: user_id=None이면 owner-inclusive personal anchor가 들어올 수 없어 cross-scope
    # 분기 자체가 불가 → company-company만 발화하고 gate에 None을 thread한다(owner-variant 미발화).
    hits = [_hit(1.0, "co-a", "company"), _hit(0.9, "co-b", "company")]
    out = maybe_missing_link(_insufficient_gap(), hits, user_id=None)
    assert out.reason == "missing_link"
    assert out.message == _build_message("A와 B의 관계", "missing_link")  # cross-scope 카피 아님
    assert _ml_env.user_ids == [None]


def test_top_company_page_slugs_returns_scope_tags_not_ids():
    # _top_company_page_slugs는 (slug, scope) tuple만 반환 — owner_id/caller id를 절대 싣지 않는다.
    from orthus.wiki.gap import _top_company_page_slugs

    hits = [_hit(1.0, "co-a", "company"), _hit(0.9, "mine", "personal")]
    anchors = _top_company_page_slugs(hits, n=2, owner_scope=True)
    assert anchors == [("co-a", "company"), ("mine", "personal")]
    for slug, scope in anchors:
        assert scope in {"company", "personal"}  # id가 아니라 scope-tag
