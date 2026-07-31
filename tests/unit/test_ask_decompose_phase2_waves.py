"""MA.6b 수용 게이트 단위 테스트 (docs/company-agent-orchestration.md §P2.3–§P2.6).

Wave loop + context_from injection + failure propagation, exercised by patching
_run_leaf (no live DB / LLM):
- read→act: upstream grounded body injected (redacted) into the dependent leaf
- PII redaction on the injected context (불변식 6/19)
- upstream failure → dependent leaf skipped as error="upstream_unavailable" (§P2.6)
- depth_exceeded → flat degrade (deps ignored, no context) (§P2.3)
- R15 scope mismatch → leaf_error isolation
- AgentWork intake stores redacted upstream under payload.context.upstream (§P2.4)
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

from orthus.router.decompose import _run_waves, build_sub_questions
from orthus.schemas.canonical import RoutedAnswer, SubAnswer, SubQuestion, WikiAnswer


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


def _run(sub_questions, fake_leaf, *, max_depth=2):
    with patch("orthus.router.decompose._run_leaf", side_effect=fake_leaf):
        return _run_waves(
            sub_questions,
            user_id=uuid4(),
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


# ── §P2.4 read→act context injection ─────────────────────────────────────────


class TestContextInjection:
    def test_upstream_body_injected_into_action_leaf(self):
        sqs = build_sub_questions(
            [("지난주 매출 얼마야", []), ("그 숫자로 메일 보내줘", [0])], scope="company"
        )
        captured: dict[str, str | None] = {}

        def fake_leaf(sq, *, upstream_context=None, **kw):
            captured[sq.text] = upstream_context
            if not sq.depends_on:
                return _grounded(sq, "지난주 매출은 1억 2천만원")
            return SubAnswer(
                sub_question_id=sq.id,
                grounded=False,
                agent_work_id=uuid4(),
                context_injected=bool(upstream_context),
            )

        answers, warnings = _run(sqs, fake_leaf)

        assert warnings == []
        # upstream leaf ran with no context
        assert captured["지난주 매출 얼마야"] is None
        # dependent action leaf received the upstream body
        assert captured["그 숫자로 메일 보내줘"] is not None
        assert "1억 2천만원" in captured["그 숫자로 메일 보내줘"]
        assert answers[0].grounded is True
        assert answers[1].agent_work_id is not None

    def test_pii_redacted_in_injected_context(self):
        sqs = build_sub_questions([("A", []), ("B", [0])], scope="company")
        captured: dict[str, str | None] = {}

        def fake_leaf(sq, *, upstream_context=None, **kw):
            captured[sq.text] = upstream_context
            if not sq.depends_on:
                return _grounded(sq, "담당자 이메일은 boss@nova.example 입니다")
            return SubAnswer(sub_question_id=sq.id, grounded=False, agent_work_id=uuid4())

        _run(sqs, fake_leaf)
        injected = captured["B"]
        assert injected is not None
        # the email must be redacted out of the injected context (불변식 6/19)
        assert "boss@nova.example" not in injected


# ── §P2.6 upstream failure propagation ───────────────────────────────────────


class TestFailurePropagation:
    def test_ungrounded_upstream_skips_dependent(self):
        sqs = build_sub_questions([("A", []), ("B", [0])], scope="company")
        ran: list[str] = []

        def fake_leaf(sq, *, upstream_context=None, **kw):
            ran.append(sq.text)
            if not sq.depends_on:
                # upstream NOT grounded
                return SubAnswer(sub_question_id=sq.id, grounded=False)
            return SubAnswer(sub_question_id=sq.id, grounded=True, agent_work_id=uuid4())

        answers, _ = _run(sqs, fake_leaf)

        assert "A" in ran
        # dependent leaf must NOT execute (read→act: no blind action)
        assert "B" not in ran
        assert answers[1].error == "upstream_unavailable"
        assert answers[1].agent_work_id is None

    def test_errored_upstream_skips_dependent(self):
        sqs = build_sub_questions([("A", []), ("B", [0])], scope="company")
        ran: list[str] = []

        def fake_leaf(sq, *, upstream_context=None, **kw):
            ran.append(sq.text)
            if not sq.depends_on:
                return SubAnswer(sub_question_id=sq.id, error="leaf_error")
            return SubAnswer(sub_question_id=sq.id, grounded=True)

        answers, _ = _run(sqs, fake_leaf)
        assert "B" not in ran
        assert answers[1].error == "upstream_unavailable"


# ── §P2.3 depth_exceeded degrade ─────────────────────────────────────────────


class TestDepthDegrade:
    def test_three_level_chain_degrades_flat(self):
        # A→B→C is 3 waves; max_depth=2 → flat degrade
        sqs = build_sub_questions([("A", []), ("B", [0]), ("C", [1])], scope="company")
        ran: list[str] = []

        def fake_leaf(sq, *, upstream_context=None, **kw):
            ran.append(sq.text)
            # flat degrade → no context should be injected
            assert upstream_context is None
            return SubAnswer(sub_question_id=sq.id, grounded=True)

        answers, warnings = _run(sqs, fake_leaf, max_depth=2)

        assert "depth_exceeded" in warnings
        assert sorted(ran) == ["A", "B", "C"]  # all executed flat, none skipped
        assert all(a.error is None for a in answers)


# ── R15 scope mismatch isolation ──────────────────────────────────────────────


class TestScopeBoundary:
    def test_scope_mismatch_isolated_as_leaf_error(self):
        # Manually construct mismatched scopes (build_sub_questions can't produce this)
        a = SubQuestion(id=uuid4(), text="A", scope="company")
        b = SubQuestion(id=uuid4(), text="B", scope="personal", depends_on=[a.id])
        ran: list[str] = []

        def fake_leaf(sq, *, upstream_context=None, **kw):
            ran.append(sq.text)
            if sq.text == "A":
                return _grounded(sq, "회사 사실")
            return SubAnswer(sub_question_id=sq.id, grounded=True)

        answers, _ = _run([a, b], fake_leaf)
        # B's resolve_context asserts scope equality → AssertionError → leaf_error
        assert "B" not in ran
        assert answers[1].error == "leaf_error"


# ── §P2.4 AgentWork intake stores redacted upstream ───────────────────────────


class TestAgentWorkUpstreamContext:
    def test_candidate_stores_redacted_upstream(self):
        from orthus.agentwork.service import assistant_command_candidate

        cand = assistant_command_candidate(
            "김부장에게 메일 보내줘",
            user_id=uuid4(),
            actor_role="owner",
            upstream_context="매출 보고: 담당 boss@nova.example 1억원",
        )
        assert cand is not None
        ctx = cand.payload.get("context", {})
        assert "upstream" in ctx
        assert "boss@nova.example" not in ctx["upstream"]  # redacted
        assert "1억원" in ctx["upstream"]
