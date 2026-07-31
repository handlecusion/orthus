"""MA.8b 수용 게이트 단위 테스트 (docs/company-agent-orchestration.md §P3B.3).

Full DAG (depth > 2):
- full-DAG flag off → effective max_depth caps at 2, depth > 2 flat-degrades
  (Phase 2 byte-identical, 불변식 32).
- full-DAG flag on → depth 3–4 read→act→act chains run as dependency waves with
  context injection (no depth_exceeded).
- MAX_TOTAL_LEAVES cost guard → split past the budget degrades to a single
  answer() (not a fan-out) (R18, 불변식 31).
- cumulative context_from chain length cap + per-hop PII redaction at depth ≥ 3
  (R19, 불변식 6/19).
- backward-only deps keep a depth-4 graph structurally acyclic (불변식 31).

Exercised by patching _run_leaf / get_settings (no live DB / LLM).
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch
from uuid import uuid4

from orthus.router.decompose import (
    _MAX_CONTEXT_CHARS,
    _run_waves,
    build_sub_questions,
    resolve_context,
    topo_levels,
)
from orthus.schemas.canonical import RoutedAnswer, SubAnswer, SubQuestion, WikiAnswer

_USER = uuid4()


def _grounded(sq: SubQuestion, answer: str) -> SubAnswer:
    return SubAnswer(
        sub_question_id=sq.id,
        grounded=True,
        routed=RoutedAnswer(
            question=sq.text,
            mode="wiki",
            wiki=WikiAnswer(question=sq.text, answer=answer, sources=[]),
        ),
    )


def _run(sub_questions, fake_leaf, *, max_depth):
    with patch("orthus.router.decompose._run_leaf", side_effect=fake_leaf):
        return _run_waves(
            sub_questions,
            user_id=_USER,
            scope="company",
            project=None,
            chat_model=None,
            context_wiki_slug=None,
            actor_role="owner",
            allow_agentic_in_leaf=False,
            stream_key=None,
            max_concurrency=4,
            leaf_timeout_sec=10,
            max_depth=max_depth,
        )


# ── §P3B.3.1 depth > 2 wave execution (full DAG) ─────────────────────────────


class TestFullDagWaves:
    def test_depth3_read_act_act_runs_as_waves(self):
        # A → B → C: 3 waves. max_depth=4 (full DAG) → executes as waves, context flows.
        sqs = build_sub_questions(
            [("매출 조회", []), ("보고서 초안", [0]), ("메일 초안", [1])], scope="company"
        )
        captured: dict[str, str | None] = {}

        def fake_leaf(sq, *, upstream_context=None, **kw):
            captured[sq.text] = upstream_context
            return _grounded(sq, f"{sq.text} 결과")

        answers, warnings = _run(sqs, fake_leaf, max_depth=4)

        assert "depth_exceeded" not in warnings
        # wave 0 ran with no context; each later hop saw its direct upstream body
        assert captured["매출 조회"] is None
        assert captured["보고서 초안"] is not None and "매출 조회 결과" in captured["보고서 초안"]
        assert captured["메일 초안"] is not None and "보고서 초안 결과" in captured["메일 초안"]
        assert all(a.error is None for a in answers)

    def test_depth4_chain_runs_as_four_waves(self):
        sqs = build_sub_questions([("A", []), ("B", [0]), ("C", [1]), ("D", [2])], scope="company")
        order: list[str] = []

        def fake_leaf(sq, *, upstream_context=None, **kw):
            order.append(sq.text)
            return _grounded(sq, f"{sq.text}-body")

        answers, warnings = _run(sqs, fake_leaf, max_depth=4)

        assert "depth_exceeded" not in warnings
        # backward-only deps force a strict A,B,C,D execution order across waves
        assert order == ["A", "B", "C", "D"]
        assert all(a.error is None for a in answers)

    def test_depth3_flat_degrades_when_cap_is_two(self):
        # Same 3-level chain but max_depth=2 (full DAG OFF effective) → flat degrade.
        sqs = build_sub_questions([("A", []), ("B", [0]), ("C", [1])], scope="company")

        def fake_leaf(sq, *, upstream_context=None, **kw):
            assert upstream_context is None  # flat degrade injects no context
            return _grounded(sq, "x")

        answers, warnings = _run(sqs, fake_leaf, max_depth=2)
        assert "depth_exceeded" in warnings
        assert all(a.error is None for a in answers)


# ── §P3B.3.2 backward-only / acyclic at depth 4 ──────────────────────────────


class TestAcyclicAtDepth:
    def test_depth4_chain_has_four_topo_levels(self):
        sqs = build_sub_questions([("A", []), ("B", [0]), ("C", [1]), ("D", [2])], scope="company")
        levels = topo_levels(sqs)
        assert [[sq.text for sq in lvl] for lvl in levels] == [["A"], ["B"], ["C"], ["D"]]

    def test_topo_levels_never_cycles_even_with_forward_dep(self):
        # backward-only is enforced upstream in split_question_with_deps (Phase 2);
        # topo_levels is the fail-safe: even a forward/dangling edge terminates and
        # places every node exactly once (no empty wave, no infinite loop).
        sqs = build_sub_questions([("A", [1]), ("B", []), ("C", [1])], scope="company")
        levels = topo_levels(sqs)
        placed = [sq.text for lvl in levels for sq in lvl]
        assert sorted(placed) == ["A", "B", "C"]
        assert all(lvl for lvl in levels)  # no empty wave


# ── §P3B.3.1 / R19 cumulative context cap + hop redaction ────────────────────


class TestContextChainBudget:
    def test_long_upstream_context_truncated_under_full_dag(self):
        a = SubQuestion(id=uuid4(), text="A", scope="company")
        b = SubQuestion(id=uuid4(), text="B", scope="company", depends_on=[a.id])
        long_body = "가" * (_MAX_CONTEXT_CHARS + 500)
        answers_by_id = {a.id: _grounded(a, long_body)}
        # full-DAG cap applied → truncated
        ctx = resolve_context(
            b, answers_by_id, {a.id: a, b.id: b}, max_context_chars=_MAX_CONTEXT_CHARS
        )
        assert ctx is not None
        assert len(ctx) <= _MAX_CONTEXT_CHARS

    def test_no_cap_when_off_keeps_full_context(self):
        # max_context_chars=None (full DAG off) → Phase 2 byte-identical, no truncation.
        a = SubQuestion(id=uuid4(), text="A", scope="company")
        b = SubQuestion(id=uuid4(), text="B", scope="company", depends_on=[a.id])
        long_body = "가" * (_MAX_CONTEXT_CHARS + 500)
        answers_by_id = {a.id: _grounded(a, long_body)}
        ctx = resolve_context(b, answers_by_id, {a.id: a, b.id: b})
        assert ctx is not None
        assert len(ctx) == _MAX_CONTEXT_CHARS + 500

    def test_depth3_context_redacted_each_hop(self):
        sqs = build_sub_questions([("A", []), ("B", [0]), ("C", [1])], scope="company")
        captured: dict[str, str | None] = {}

        def fake_leaf(sq, *, upstream_context=None, **kw):
            captured[sq.text] = upstream_context
            if sq.text == "A":
                return _grounded(sq, "담당자 메일 boss@nova.example 매출 1억")
            if sq.text == "B":
                return _grounded(sq, "두 번째 담당자 ceo@acme.example 보고")
            return _grounded(sq, "done")

        _run(sqs, fake_leaf, max_depth=4)
        # B sees A's body redacted; C sees B's body redacted (per-hop, 불변식 6/19)
        assert captured["B"] is not None and "boss@nova.example" not in captured["B"]
        assert captured["C"] is not None and "ceo@acme.example" not in captured["C"]


# ── §P3B.3.1 / R18 MAX_TOTAL_LEAVES + flag gating via answer_or_decompose ─────


@contextmanager
def _noop_audit(_name, **_kw):
    span = MagicMock()
    span.add_meta = MagicMock()
    yield span


def _settings(**over):
    s = MagicMock()
    s.ask_decompose_max_parts = over.get("max_parts", 8)
    s.ask_decompose_max_concurrency = 4
    s.ask_decompose_leaf_timeout_sec = 10
    s.ask_decompose_depends_enabled = True
    s.ask_decompose_max_depth = over.get("max_depth", 4)
    s.ask_decompose_full_dag_enabled = over.get("full_dag", True)
    s.ask_decompose_max_total_leaves = over.get("max_total_leaves", 12)
    s.ask_prompt_cache_enabled = False
    return s


def _aod(question, *, settings, pairs, fake_leaf):
    from orthus.router import decompose

    with (
        patch("orthus.router.decompose.get_settings", return_value=settings),
        patch("orthus.router.decompose.audit", side_effect=_noop_audit),
        patch("orthus.router.decompose.should_decompose", return_value=True),
        patch("orthus.router.decompose.split_question_with_deps", return_value=pairs),
        patch("orthus.router.decompose._run_leaf", side_effect=fake_leaf),
        patch("orthus.router.answer") as mock_answer,
    ):
        mock_answer.return_value = RoutedAnswer(question=question, mode="wiki")
        result = decompose.answer_or_decompose(
            _USER,
            question,
            scope="company",
            project=None,
            chat_model=None,
            context_wiki_slug=None,
            history=None,
            stream_id=None,
            actor_role="owner",
            allow_agentic_in_leaf=False,
            learn=False,
            record_gaps=False,
        )
    return result, mock_answer


def _leaf_ok(sq, *, upstream_context=None, **kw):
    return _grounded(sq, f"{sq.text}-body")


class TestFlagGatingAndLeafCap:
    def test_full_dag_off_caps_depth_at_two(self):
        # max_depth=4 but full DAG OFF → effective cap 2 → 3-level chain flat-degrades.
        settings = _settings(full_dag=False, max_depth=4)
        pairs = [("A", []), ("B", [0]), ("C", [1])]
        result, mock_answer = _aod("복합", settings=settings, pairs=pairs, fake_leaf=_leaf_ok)
        mock_answer.assert_not_called()
        assert result.mode == "decomposed"
        assert "depth_exceeded" in result.warnings

    def test_full_dag_on_allows_depth_three(self):
        settings = _settings(full_dag=True, max_depth=4)
        pairs = [("A", []), ("B", [0]), ("C", [1])]
        result, mock_answer = _aod("복합", settings=settings, pairs=pairs, fake_leaf=_leaf_ok)
        mock_answer.assert_not_called()
        assert result.mode == "decomposed"
        assert "depth_exceeded" not in result.warnings

    def test_total_leaves_exceeded_degrades_to_single_answer(self):
        # 6 parts but budget 4 → fall through to single answer() (no fan-out).
        settings = _settings(full_dag=True, max_depth=4, max_parts=8, max_total_leaves=4)
        pairs = [(c, []) for c in ("A", "B", "C", "D", "E", "F")]
        result, mock_answer = _aod("복합", settings=settings, pairs=pairs, fake_leaf=_leaf_ok)
        mock_answer.assert_called_once()
        assert result.mode != "decomposed"

    def test_total_leaves_within_budget_decomposes(self):
        settings = _settings(full_dag=True, max_depth=4, max_parts=8, max_total_leaves=12)
        pairs = [(c, []) for c in ("A", "B", "C")]
        result, mock_answer = _aod("복합", settings=settings, pairs=pairs, fake_leaf=_leaf_ok)
        mock_answer.assert_not_called()
        assert result.mode == "decomposed"

    def test_total_leaves_guard_inert_when_full_dag_off(self):
        # full DAG OFF + parts over budget → guard gated off → still decomposes
        # (Phase 1/2 byte-identical for any config, 불변식 32).
        settings = _settings(full_dag=False, max_depth=2, max_parts=8, max_total_leaves=4)
        pairs = [(c, []) for c in ("A", "B", "C", "D", "E", "F")]
        result, mock_answer = _aod("복합", settings=settings, pairs=pairs, fake_leaf=_leaf_ok)
        mock_answer.assert_not_called()
        assert result.mode == "decomposed"
