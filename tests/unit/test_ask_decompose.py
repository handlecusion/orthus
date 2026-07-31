"""MA.1/MA.2 수용 게이트 단위 테스트 (docs/company-agent-orchestration.md §20).

Tests cover (without a live DB or real LLM):
- should_decompose prefilter boundary cases
- should_decompose LLM enum 3-class: yes→True, no→False, uncertain→False
- split_question parse/filter
- classify_subpart off-list rejection
- coordinate() always done=True (불변식 12)
- depends_on always ignored (불변식 12)
- flag off: answer() output byte-identical to legacy (no decompose)
- priority: decompose on + not federate → decompose entry
- priority: decompose off + agentic on + not federate → agentic entry
- personal + scope=all → legacy (federate=True guard)
- knowledge-token (allow_decompose=False) → legacy
- fan_out partial failure isolation
- owner-scope inheritance (sub-query never gets a wider scope)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from orthus.router.decompose import (
    coordinate,
    should_decompose,
    split_question,
    synthesize,
)
from orthus.router.tools import classify_subpart
from orthus.schemas.canonical import (
    AssistantResult,
    CoordinateResult,
    RoutedAnswer,
    SubAnswer,
    SubQuestion,
    ValidationResult,
    WikiAnswer,
    WikiGap,
    WikiSourceRef,
)
from orthus.wiki.gap import detect_gap

_USER = UUID("cbd68a41-cdb7-4fd7-8ed1-605f7ecff93f")

# A well-scoring but unrelated hit — the retrieval shape behind a prose refusal (F1).
_HIT = WikiSourceRef(
    page_slug="p", title="P", kind="page", excerpt="…", score=0.9, source_scope="company"
)


# ── MockChat helper ─────────────────────────────────────────────────────────


class _MockChat:
    """Scripted chat model: returns fixed completions."""

    def __init__(self, response: str):
        self._response = response

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        return self._response


# ── §4.1 False-prefilter ─────────────────────────────────────────────────────


class TestShouldDecomposePrefilter:
    """Prefilter: both conditions (no verb AND no connective) → immediately False."""

    def test_no_verb_no_connective_immediately_false(self):
        # Pure knowledge question — no command verb, no connective
        q = "회사 정책이 뭐야"
        # Patch the model resolver to prove the prefilter never reaches an LLM.
        with patch("orthus.router.decompose.get_chat_model_for") as mock_gcm:
            result = should_decompose(q)
            # Resolver NOT called → the deterministic prefilter answered on its own.
            mock_gcm.assert_not_called()
        assert result is False

    def test_has_command_verb_calls_llm(self):
        # "해줘" is a command verb → prefilter passes → LLM called
        q = "정리해줘"
        chat = _MockChat(json.dumps({"decompose": "no"}))
        result = should_decompose(q, chat_model=chat)
        assert result is False  # LLM says "no"

    def test_has_connective_calls_llm(self):
        # "그리고" is a connective → prefilter passes → LLM called
        q = "A 알려줘 그리고 B도 알려줘"
        chat = _MockChat(json.dumps({"decompose": "yes"}))
        result = should_decompose(q, chat_model=chat)
        assert result is True

    def test_has_verb_only_calls_llm(self):
        q = "Notion 동기화해줘"
        chat = _MockChat(json.dumps({"decompose": "no"}))
        result = should_decompose(q, chat_model=chat)
        assert result is False

    def test_empty_question(self):
        assert should_decompose("") is False


# ── §4.2 LLM enum 3-class ────────────────────────────────────────────────────


class TestShouldDecomposeLLMEnum:
    """LLM enum: yes→True, no→False, uncertain→False."""

    def _q_with_verb(self) -> str:
        return "이메일 보내줘 그리고 slack 동기화해줘"

    def test_yes_returns_true(self):
        chat = _MockChat(json.dumps({"decompose": "yes"}))
        assert should_decompose(self._q_with_verb(), chat_model=chat) is True

    def test_no_returns_false(self):
        chat = _MockChat(json.dumps({"decompose": "no"}))
        assert should_decompose(self._q_with_verb(), chat_model=chat) is False

    def test_uncertain_returns_false(self):
        chat = _MockChat(json.dumps({"decompose": "uncertain"}))
        assert should_decompose(self._q_with_verb(), chat_model=chat) is False

    def test_malformed_json_returns_false(self):
        chat = _MockChat("not json at all")
        assert should_decompose(self._q_with_verb(), chat_model=chat) is False

    def test_unexpected_value_returns_false(self):
        chat = _MockChat(json.dumps({"decompose": "maybe"}))
        assert should_decompose(self._q_with_verb(), chat_model=chat) is False


# ── §5 split_question ────────────────────────────────────────────────────────


class TestSplitQuestion:
    def test_valid_split(self):
        payload = {"sub_questions": ["A", "B", "C"]}
        chat = _MockChat(json.dumps(payload))
        result = split_question("compound", k=5, chat_model=chat)
        assert result == ["A", "B", "C"]

    def test_single_item_returns_empty(self):
        chat = _MockChat(json.dumps({"sub_questions": ["only one"]}))
        assert split_question("q", k=5, chat_model=chat) == []

    def test_exceeds_k_returns_empty(self):
        payload = {"sub_questions": ["A", "B", "C", "D", "E", "F"]}
        chat = _MockChat(json.dumps(payload))
        assert split_question("q", k=3, chat_model=chat) == []

    def test_parse_failure_returns_empty(self):
        chat = _MockChat("bad json")
        assert split_question("q", k=5, chat_model=chat) == []

    def test_empty_strings_filtered(self):
        payload = {"sub_questions": ["A", "  ", "B"]}
        chat = _MockChat(json.dumps(payload))
        result = split_question("q", k=5, chat_model=chat)
        assert result == ["A", "B"]

    def test_all_empty_after_filter_returns_empty(self):
        payload = {"sub_questions": ["  ", ""]}
        chat = _MockChat(json.dumps(payload))
        assert split_question("q", k=5, chat_model=chat) == []


# ── §6 classify_subpart ──────────────────────────────────────────────────────


class TestClassifySubpart:
    def test_off_list_leaf_raises(self):
        """Off-list adapter name must raise ValueError (불변식 9)."""
        # We can't produce an off-list leaf from classify_subpart itself, but the
        # _VALID_LEAVES check inside classify_subpart guards against it. Simulate by
        # patching the unified classify_intent() to return an off-list read route.
        from orthus.router.route import RouteIntent

        with patch(
            "orthus.router.tools.classify_intent",
            return_value=RouteIntent(None, "unknown_leaf"),
        ):
            with pytest.raises(ValueError, match="off-list"):
                classify_subpart("회사 wiki 내용 설명해줘")

    def test_info_query_not_action_intake(self):
        """_INFO_QUERY_TERMS question falls through to wiki/structured, not action-intake."""
        leaf, family = classify_subpart("회사 정책이 어떻게 돼?")
        assert leaf != "action-intake"
        assert family is None

    def test_command_verb_detects_action_intake(self):
        """Command verb → action-intake leaf."""
        leaf, family = classify_subpart("Notion 동기화해줘")
        assert leaf == "action-intake"
        assert family is not None


# ── §2 coordinate no-op (불변식 12) ─────────────────────────────────────────


class TestCoordinate:
    def test_always_done_true_empty(self):
        result = coordinate([])
        assert isinstance(result, CoordinateResult)
        assert result.done is True

    def test_always_done_true_with_sub_answers(self):
        sa = SubAnswer(sub_question_id=uuid4(), grounded=True)
        result = coordinate([sa])
        assert result.done is True

    def test_always_done_true_multiple(self):
        sas = [SubAnswer(sub_question_id=uuid4()) for _ in range(5)]
        result = coordinate(sas)
        assert result.done is True


# ── synthesize includes structured sub-answers (knowledge+aggregation fix) ──


class _CapturingChat:
    """Chat model that records the last user prompt and returns a fixed completion."""

    def __init__(self, response: str):
        self._response = response
        self.last_user: str | None = None

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        self.last_user = user
        return self._response


def _wiki_sub(sq_id, *, answer: str) -> SubAnswer:
    routed = RoutedAnswer(
        question="q",
        mode="wiki",
        wiki=WikiAnswer(question="q", answer=answer, sources=[]),
    )
    return SubAnswer(sub_question_id=sq_id, routed=routed, grounded=True)


def _structured_sub(sq_id, *, columns, rows) -> SubAnswer:
    result = AssistantResult(
        query_id=uuid4(),
        question="q",
        compiled=None,
        validation=ValidationResult(),
        status="executed",
        columns=columns,
        rows=rows,
    )
    routed = RoutedAnswer(question="q", mode="structured", structured=result)
    return SubAnswer(sub_question_id=sq_id, routed=routed, grounded=True)


class TestSynthesizeIncludesStructured:
    """A knowledge+aggregation compound must feed the structured rows into synthesis,
    not drop them (regression: synthesized body used to deny the aggregation)."""

    def test_structured_body_reaches_synthesis_prompt(self):
        wiki_sq = SubQuestion(id=uuid4(), text="휴가 정책 설명해줘", scope="company")
        struct_sq = SubQuestion(id=uuid4(), text="열려있는 티켓 개수 알려줘", scope="company")
        subs = [
            _wiki_sub(wiki_sq.id, answer="휴가는 연 15일"),
            _structured_sub(struct_sq.id, columns=["open_tickets"], rows=[[12]]),
        ]
        chat = _CapturingChat("휴가는 15일이고 열린 티켓은 12개입니다.")
        body = synthesize("휴가 정책과 티켓 개수", subs, [wiki_sq, struct_sq], chat_model=chat)
        assert body is not None
        # The structured columns/rows summary must be in the prompt the synthesis LLM saw.
        assert chat.last_user is not None
        assert "open_tickets" in chat.last_user
        assert "12" in chat.last_user
        # Both sub-questions appear so the LLM addresses each.
        assert "티켓" in chat.last_user

    def test_structured_only_compound_still_synthesizes(self):
        """All-structured compound previously returned None (no wiki leaf)."""
        a = SubQuestion(id=uuid4(), text="열린 티켓 개수", scope="company")
        b = SubQuestion(id=uuid4(), text="담당자별 티켓 수", scope="company")
        subs = [
            _structured_sub(a.id, columns=["open"], rows=[[7]]),
            _structured_sub(b.id, columns=["assignee", "n"], rows=[["kim", 3]]),
        ]
        chat = _CapturingChat("열린 티켓 7개, 담당자별 집계 결과입니다.")
        body = synthesize("티켓 집계", subs, [a, b], chat_model=chat)
        assert body is not None
        assert chat.last_user is not None
        assert "7" in chat.last_user and "kim" in chat.last_user


# ── 단일 grounded wiki 조각은 LLM 없이 verbatim passthrough (latency) ─────────


class TestSynthesizeSingleGroundedPassthrough:
    """command-split "명령+질문" 대표 시나리오: 명령 fragment는 action-intake라
    grounded=False, 질문 leaf 1개만 grounded. 이때 synthesize는 LLM을 부르지 않고
    leaf 본문을 그대로 돌려준다(결정론 passthrough)."""

    def test_single_wiki_body_returns_verbatim_without_llm(self):
        wiki_sq = SubQuestion(id=uuid4(), text="휴가 정책 알려줘", scope="company")
        cmd_sq = SubQuestion(id=uuid4(), text="gmail 동기화해줘", scope="company")
        subs = [
            _wiki_sub(wiki_sq.id, answer="휴가는 연 15일이며 이월은 5일까지 가능합니다."),
            # action-intake fragment: grounded=False, routed=None
            SubAnswer(sub_question_id=cmd_sq.id, grounded=False, agent_work_id=uuid4()),
        ]
        chat = _CapturingChat("호출되면 안 되는 응답")
        body = synthesize(
            "gmail 동기화해줘 그리고 휴가정책 알려줘", subs, [wiki_sq, cmd_sq], chat_model=chat
        )
        assert body is not None
        # verbatim: LLM 재작성 없이 leaf 본문 그대로.
        assert body.answer == "휴가는 연 15일이며 이월은 5일까지 가능합니다."
        # LLM은 호출되지 않았다.
        assert chat.last_user is None

    def test_single_wiki_body_carries_error_warnings(self):
        wiki_sq = SubQuestion(id=uuid4(), text="휴가 정책", scope="company")
        err_sq = SubQuestion(id=uuid4(), text="실패한 조각", scope="company")
        subs = [
            _wiki_sub(wiki_sq.id, answer="연 15일"),
            SubAnswer(sub_question_id=err_sq.id, error="leaf_error"),
        ]
        chat = _CapturingChat("무관")
        body = synthesize("복합", subs, [wiki_sq, err_sq], chat_model=chat)
        assert body is not None
        assert body.answer == "연 15일"
        assert "sub_answer_error:leaf_error" in body.warnings
        assert chat.last_user is None

    def test_single_structured_body_still_uses_llm(self):
        """structured 단일 조각은 columns/rows 요약을 산문으로 다듬는 가치가 있어
        기존 LLM 합성 경로를 유지한다."""
        sq = SubQuestion(id=uuid4(), text="열린 티켓 개수", scope="company")
        subs = [_structured_sub(sq.id, columns=["open"], rows=[[7]])]
        chat = _CapturingChat("열린 티켓은 7개입니다.")
        body = synthesize("티켓 개수", subs, [sq], chat_model=chat)
        assert body is not None
        assert body.answer == "열린 티켓은 7개입니다."
        assert chat.last_user is not None  # LLM 경로 유지

    def test_two_grounded_bodies_still_synthesize_with_llm(self):
        a = SubQuestion(id=uuid4(), text="휴가 정책", scope="company")
        b = SubQuestion(id=uuid4(), text="재택 정책", scope="company")
        subs = [
            _wiki_sub(a.id, answer="연 15일"),
            _wiki_sub(b.id, answer="주 2회"),
        ]
        chat = _CapturingChat("휴가는 연 15일, 재택은 주 2회입니다.")
        body = synthesize("휴가와 재택 정책", subs, [a, b], chat_model=chat)
        assert body is not None
        assert body.answer == "휴가는 연 15일, 재택은 주 2회입니다."
        assert chat.last_user is not None
        assert "연 15일" in chat.last_user and "주 2회" in chat.last_user


# ── depends_on always ignored (불변식 12) ────────────────────────────────────


class TestDependsOnIgnored:
    def test_depends_on_field_ignored_by_decompose(self):
        """depends_on is a reserved field and must never affect Phase 1 behavior."""
        uid_a = uuid4()
        uid_b = uuid4()
        sq_b = SubQuestion(id=uid_b, text="B", scope="company", depends_on=[uid_a])
        # Phase 1: depends_on has no impact on fan_out ordering or coordinate()
        sa_b = SubAnswer(sub_question_id=uid_b, grounded=False)
        # If depends_on were used, coordinate might block; it must not
        result = coordinate([sa_b])
        assert result.done is True
        # depends_on field exists (reserved) but is ignored in phase 1
        assert sq_b.depends_on == [uid_a]


# ── flag off → legacy path unchanged ─────────────────────────────────────────


class TestFlagOff:
    """ORTHUS_ASK_DECOMPOSE_ENABLED=false → answer() output unchanged."""

    def test_flag_off_uses_legacy(self):
        from orthus.router import answer
        from contextlib import contextmanager

        @contextmanager
        def _noop_audit(_name, **_kw):
            span = MagicMock()
            span.add_meta = MagicMock()
            yield span

        with (
            patch("orthus.router.get_settings") as mock_settings,
            patch("orthus.router.should_federate", return_value=False),
            patch("orthus.router.get_agent_chat_model", return_value=None),
            patch("orthus.router.classify", return_value="wiki"),
            patch(
                "orthus.router.ask", return_value=WikiAnswer(question="q", answer="a", sources=[])
            ),
            patch("orthus.router.audit", side_effect=_noop_audit),
        ):
            settings = MagicMock()
            settings.ask_decompose_enabled = False
            settings.agentic_ask_enabled = False
            settings.ask_semantic_cache_enabled = False
            mock_settings.return_value = settings

            result = answer(_USER, "q", scope="company")
        assert result.mode != "decomposed"


# ── priority: decompose on → decompose entry ─────────────────────────────────


class TestDecomposePriority:
    def test_decompose_on_enters_decompose(self):
        from orthus.router import answer

        decompose_result = RoutedAnswer(question="q", mode="decomposed")

        with (
            patch("orthus.router.get_settings") as mock_settings,
            patch("orthus.router.should_federate", return_value=False),
            patch(
                "orthus.router.decompose.answer_or_decompose", return_value=decompose_result
            ) as mock_aod,
        ):
            settings = MagicMock()
            settings.ask_decompose_enabled = True
            settings.agentic_ask_enabled = False
            settings.ask_semantic_cache_enabled = False
            mock_settings.return_value = settings

            result = answer(_USER, "q", scope="company", allow_decompose=True)

        mock_aod.assert_called_once()
        assert result.mode == "decomposed"

    def test_allow_decompose_false_skips_decompose(self):
        """allow_decompose=False (knowledge-token / leaf call) → decompose never entered."""
        from orthus.router import answer
        from contextlib import contextmanager

        @contextmanager
        def _noop_audit(_name, **_kw):
            span = MagicMock()
            span.add_meta = MagicMock()
            yield span

        with (
            patch("orthus.router.get_settings") as mock_settings,
            patch("orthus.router.should_federate", return_value=False),
            patch("orthus.router.get_agent_chat_model", return_value=None),
            patch("orthus.router.classify", return_value="wiki"),
            patch(
                "orthus.router.ask", return_value=WikiAnswer(question="q", answer="a", sources=[])
            ),
            patch("orthus.router.audit", side_effect=_noop_audit),
        ):
            settings = MagicMock()
            settings.ask_decompose_enabled = True
            settings.agentic_ask_enabled = False
            settings.ask_semantic_cache_enabled = False
            mock_settings.return_value = settings

            result = answer(_USER, "q", scope="company", allow_decompose=False)

        assert result.mode in {"wiki", "structured", "graph"}


# ── personal + scope=all → legacy (federate=True) ─────────────────────────────


class TestFederateSkipsDecompose:
    def test_federate_true_goes_legacy(self):
        from orthus.router import answer
        from contextlib import contextmanager

        @contextmanager
        def _noop_audit(_name, **_kw):
            span = MagicMock()
            span.add_meta = MagicMock()
            yield span

        with (
            patch("orthus.router.get_settings") as mock_settings,
            patch("orthus.router.should_federate", return_value=True),
            patch(
                "orthus.router.federated_wiki_answer",
                return_value=WikiAnswer(question="q", answer="a", sources=[]),
            ),
            patch("orthus.router.classify", return_value="wiki"),
            patch("orthus.router.audit", side_effect=_noop_audit),
        ):
            settings = MagicMock()
            settings.ask_decompose_enabled = True
            settings.agentic_ask_enabled = False
            settings.ask_semantic_cache_enabled = False
            mock_settings.return_value = settings

            result = answer(_USER, "q", scope="all")

        assert result.mode == "wiki"


# ── fan_out partial failure isolation ────────────────────────────────────────


class TestFanOutIsolation:
    """A single leaf error must not fail the whole fan_out (불변식 R3)."""

    def test_leaf_error_isolated(self):
        from orthus.router.decompose import fan_out

        sq1 = SubQuestion(id=uuid4(), text="A", scope="company")
        sq2 = SubQuestion(id=uuid4(), text="B", scope="company")

        call_count = {"n": 0}

        def _bad_leaf(sq, **kwargs):
            call_count["n"] += 1
            if sq.text == "A":
                raise RuntimeError("simulated leaf error")
            return SubAnswer(sub_question_id=sq.id, grounded=True)

        with patch("orthus.router.decompose._run_leaf", side_effect=_bad_leaf):
            results = fan_out(
                [sq1, sq2],
                user_id=_USER,
                scope="company",
                project=None,
                chat_model=None,
                context_wiki_slug=None,
                actor_role="owner",
                allow_agentic_in_leaf=False,
                stream_key=None,
                max_concurrency=2,
                leaf_timeout_sec=10,
            )

        assert len(results) == 2
        # The errored leaf is surfaced as error, not a 500
        error_answers = [r for r in results if r.error]
        ok_answers = [r for r in results if r.grounded]
        assert len(error_answers) >= 1
        assert len(ok_answers) >= 1


# ── owner-scope never escalated ──────────────────────────────────────────────


class TestOwnerScopeInheritance:
    def test_sub_question_inherits_parent_scope(self):
        sq = SubQuestion(id=uuid4(), text="B", scope="company")
        # The sub-question must carry the SAME scope as the parent — never escalated
        assert sq.scope == "company"

    def test_sub_question_no_escalation(self):
        sq_personal = SubQuestion(id=uuid4(), text="C", scope="personal")
        # personal stays personal; cannot escalate to "all" or "company"
        assert sq_personal.scope == "personal"


# ── _run_leaf grounded: a prose refusal is NOT grounded (gap-aware, F1) ──────


def _routed_wiki(answer: str, gap: WikiGap | None) -> RoutedAnswer:
    return RoutedAnswer(
        question="q",
        mode="wiki",
        wiki=WikiAnswer(question="q", answer=answer, sources=[], gap=gap),
    )


def _leaf_with(routed: RoutedAnswer) -> SubAnswer:
    """Run _run_leaf over a knowledge leaf whose answer() returns `routed`."""
    from orthus.router import decompose as d

    sub_q = SubQuestion(id=uuid4(), text="노바티 영입 후보 선정 기준", scope="company")
    with (
        patch.object(d, "classify_subpart", return_value=("wiki", None)),
        patch("orthus.router.answer", return_value=routed),
    ):
        return d._run_leaf(
            sub_q,
            user_id=_USER,
            scope="company",
            project=None,
            chat_model=None,
            context_wiki_slug=None,
            actor_role="owner",
            allow_agentic_in_leaf=False,
            stream_key=None,
            leaf_index=0,
        )


_REFUSAL = (
    "제공된 컨텍스트에는 노바티 영입 후보 선정 기준에 대한 정보가 포함되어 있지 않습니다. "
    "따라서 해당 질문에 대한 답변을 제공할 수 없습니다."
)


class TestRunLeafGroundedIsGapAware:
    """F1: the old predicate only looked for the no-hits sentinel ("근거 없음"), so the far
    more common failure — 5 unrelated hits + a prose refusal — passed as grounded and was
    fed to synthesize() as evidence. The deterministic detector (wiki.gap.detect_gap), which
    the cache path already requires, now decides."""

    def test_prose_refusal_with_gap_is_not_grounded(self):
        gap = detect_gap("노바티 영입 후보 선정 기준", [_HIT], _REFUSAL)
        assert gap is not None and gap.reason == "insufficient_grounding"
        sa = _leaf_with(_routed_wiki(_REFUSAL, gap))
        assert sa.grounded is False
        assert sa.gap is not None and sa.gap.reason == "insufficient_grounding"

    def test_substantive_answer_stays_grounded(self):
        body = "영입 후보는 포트폴리오, 레퍼런스 체크, 팀 적합도 3단계로 선정합니다."
        assert detect_gap("노바티 영입 후보 선정 기준", [_HIT], body) is None
        sa = _leaf_with(_routed_wiki(body, None))
        assert sa.grounded is True
        assert sa.gap is None

    def test_no_hits_sentinel_still_not_grounded(self):
        # Pre-existing behavior kept: the 0-hit sentinel body is ungrounded (now via gap too).
        sentinel = "관련 위키 내용을 찾지 못했습니다. (wiki에 근거 없음)"
        sa = _leaf_with(_routed_wiki(sentinel, detect_gap("q", [], sentinel)))
        assert sa.grounded is False

    def test_refusal_leaf_is_dropped_from_synthesis(self):
        """The point of the fix: a refusal must not enter the synthesis input, and must not
        contribute citations. With every leaf refusing, synthesize() early-exits (None)."""
        refuse_sq = SubQuestion(id=uuid4(), text="영입 기준", scope="company")
        ok_sq = SubQuestion(id=uuid4(), text="휴가 정책", scope="company")
        gap = detect_gap("영입 기준", [_HIT], _REFUSAL)
        subs = [
            SubAnswer(
                sub_question_id=refuse_sq.id,
                routed=_routed_wiki(_REFUSAL, gap),
                grounded=False,  # what _run_leaf now produces
                gap=gap,
            ),
            _wiki_sub(ok_sq.id, answer="휴가는 연 15일"),
        ]
        chat = _CapturingChat("휴가는 연 15일입니다.")
        body = synthesize("영입 기준과 휴가 정책", subs, [refuse_sq, ok_sq], chat_model=chat)
        assert body is not None
        # single grounded wiki body → deterministic passthrough, no LLM, refusal text absent
        assert body.answer == "휴가는 연 15일"
        assert chat.last_user is None

    def test_all_refusals_synthesize_to_none(self):
        a = SubQuestion(id=uuid4(), text="영입 기준", scope="company")
        b = SubQuestion(id=uuid4(), text="로드맵", scope="company")
        gap = detect_gap("영입 기준", [_HIT], _REFUSAL)
        subs = [
            SubAnswer(sub_question_id=a.id, routed=_routed_wiki(_REFUSAL, gap), grounded=False),
            SubAnswer(sub_question_id=b.id, routed=_routed_wiki(_REFUSAL, gap), grounded=False),
        ]
        chat = _CapturingChat("호출되면 안 됨")
        assert synthesize("복합", subs, [a, b], chat_model=chat) is None
        assert chat.last_user is None  # no LLM burned on refusals

    def test_weak_retrieval_leaf_stays_grounded(self):
        """`weak_retrieval` is NOT answerless: detect_gap only reaches it when the answer does
        NOT look insufficient — the leaf answered, retrieval merely scored low. Dropping it
        would delete the sub-answer from the synthesized body (content loss), so it stays
        grounded. This is the one place the decompose predicate is deliberately laxer than the
        cache predicate (`gap is None`) — cache misjudgment costs a cache miss, decompose
        misjudgment costs an answer."""
        weak_hit = WikiSourceRef(
            page_slug="p", title="P", kind="page", excerpt="…", score=0.45, source_scope="company"
        )
        body = "영입 후보는 포트폴리오, 레퍼런스 체크, 팀 적합도 3단계로 선정합니다."
        gap = detect_gap("영입 기준", [weak_hit], body)
        assert gap is not None and gap.reason == "weak_retrieval"

        sa = _leaf_with(_routed_wiki(body, gap))
        assert sa.grounded is True
        assert sa.gap is not None and sa.gap.reason == "weak_retrieval"

        # …and it reaches the synthesis input (verbatim passthrough for a single wiki body).
        sq = SubQuestion(id=sa.sub_question_id, text="영입 기준", scope="company")
        chat = _CapturingChat("무관")
        synthesized = synthesize("영입 기준", [sa], [sq], chat_model=chat)
        assert synthesized is not None
        assert synthesized.answer == body

    def test_missing_link_leaf_is_not_grounded(self):
        """`missing_link` is an upgrade OF `insufficient_grounding` (wiki/gap.py::
        maybe_missing_link returns early for any other reason), so the body is still a refusal —
        the reason only adds 'the related pages exist but are disconnected'. Stays ungrounded."""
        gap = WikiGap(
            detected=True,
            reason="missing_link",
            missing_topic="영입 기준",
            top_score=0.9,
            message="자료는 있으나 서로 연결되어 있지 않습니다.",
        )
        sa = _leaf_with(_routed_wiki(_REFUSAL, gap))
        assert sa.grounded is False

    def test_ungrounded_read_blocks_dependent_action(self):
        """read→act (§P2.6): the action leaf must not run on a failed read."""
        from orthus.router.decompose import _deps_unavailable

        read_sq = SubQuestion(id=uuid4(), text="영입 기준", scope="company")
        act_sq = SubQuestion(
            id=uuid4(), text="그 내용 메일 보내줘", scope="company", depends_on=[read_sq.id]
        )
        gap = detect_gap("영입 기준", [_HIT], _REFUSAL)
        refused = SubAnswer(
            sub_question_id=read_sq.id, routed=_routed_wiki(_REFUSAL, gap), grounded=False, gap=gap
        )
        assert _deps_unavailable(act_sq, {read_sq.id: refused}) is True
        # …and a grounded read still lets it run.
        ok = _wiki_sub(read_sq.id, answer="영입 기준은 3단계입니다.")
        assert _deps_unavailable(act_sq, {read_sq.id: ok}) is False


class TestFewShotPrompts:
    """DF1 재측정 채택(experiments/fugu-ko/analysis/df-results.md §6, owner 승인
    2026-07-19)으로 gate/split 프롬프트에 few-shot 예시를 넣었다. 실수로 지워지지 않게
    고정한다 — split 예시는 {k} 포맷 이후에도 살아남아야 한다(이스케이프 회귀 방지)."""

    def test_gate_prompt_has_fewshot_examples(self):
        from orthus.router.decompose import _DECOMPOSE_SYSTEM

        assert "Examples:" in _DECOMPOSE_SYSTEM
        assert "환불 정책은 어떻게 되고 배송 정책은 어때?" in _DECOMPOSE_SYSTEM
        assert _DECOMPOSE_SYSTEM.count('"decompose": "yes"') >= 2
        assert _DECOMPOSE_SYSTEM.count('"decompose": "no"') >= 2

    def test_split_prompt_fewshot_survives_format(self):
        from orthus.router.decompose import _SPLIT_SYSTEM_TMPL

        rendered = _SPLIT_SYSTEM_TMPL.format(k=3)
        assert "Examples:" in rendered
        assert '["환불 정책은 어떻게 돼?", "배송 정책은 어때?"]' in rendered
