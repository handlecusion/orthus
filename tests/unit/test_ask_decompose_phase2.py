"""MA.6a 수용 게이트 단위 테스트 (docs/company-agent-orchestration.md §P2).

Phase 2 building blocks, no live DB or real LLM:
- split_question_with_deps: backward-only guard, str/dict 하위호환, len filter
- topo_levels: dependency waves, cycle/dangling fail-safe collapse
- build_sub_questions: index → UUID depends_on mapping
- coordinate: flag off → no-op done=True (불변식 21); depends_enabled → next_wave 결정론
"""

from __future__ import annotations

import json
from uuid import uuid4

from orthus.router.decompose import (
    _enrich_parts,
    build_sub_questions,
    coordinate,
    split_question_with_deps,
    topo_levels,
)
from orthus.schemas.canonical import CoordinateResult, SubAnswer, SubQuestion


class _MockChat:
    def __init__(self, response: str):
        self._response = response

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        return self._response


# ── §P2.2 split_question_with_deps ───────────────────────────────────────────


class TestSplitQuestionWithDeps:
    def test_valid_with_dependency(self):
        payload = {
            "sub_questions": [
                {"text": "지난주 매출 얼마야", "depends_on": []},
                {"text": "그 숫자로 메일 보내줘", "depends_on": [0]},
            ]
        }
        chat = _MockChat(json.dumps(payload))
        result = split_question_with_deps("compound", k=5, chat_model=chat)
        assert result == [("지난주 매출 얼마야", []), ("그 숫자로 메일 보내줘", [0])]

    def test_backward_only_drops_forward_and_self(self):
        # idx0 depends on 1 (forward) and 0 (self) → both dropped.
        # idx1 depends on 0 (ok) and 5 (out-of-range) → keep only 0.
        payload = {
            "sub_questions": [
                {"text": "A", "depends_on": [1, 0]},
                {"text": "B", "depends_on": [0, 5]},
            ]
        }
        chat = _MockChat(json.dumps(payload))
        result = split_question_with_deps("q", k=5, chat_model=chat)
        assert result == [("A", []), ("B", [0])]

    def test_str_items_backward_compat(self):
        # Phase 1 format (list of str) → depends_on []
        chat = _MockChat(json.dumps({"sub_questions": ["A", "B", "C"]}))
        result = split_question_with_deps("q", k=5, chat_model=chat)
        assert result == [("A", []), ("B", []), ("C", [])]

    def test_dict_without_text_dropped(self):
        payload = {"sub_questions": [{"text": "A", "depends_on": []}, {"depends_on": [0]}]}
        chat = _MockChat(json.dumps(payload))
        # after dropping the textless item only 1 remains → []
        assert split_question_with_deps("q", k=5, chat_model=chat) == []

    def test_depends_non_list_coerced_empty(self):
        payload = {
            "sub_questions": [
                {"text": "A", "depends_on": "nope"},
                {"text": "B", "depends_on": [0]},
            ]
        }
        chat = _MockChat(json.dumps(payload))
        assert split_question_with_deps("q", k=5, chat_model=chat) == [("A", []), ("B", [0])]

    def test_single_item_returns_empty(self):
        chat = _MockChat(json.dumps({"sub_questions": [{"text": "only", "depends_on": []}]}))
        assert split_question_with_deps("q", k=5, chat_model=chat) == []

    def test_exceeds_k_returns_empty(self):
        payload = {"sub_questions": [{"text": c, "depends_on": []} for c in "ABCD"]}
        chat = _MockChat(json.dumps(payload))
        assert split_question_with_deps("q", k=3, chat_model=chat) == []

    def test_parse_failure_returns_empty(self):
        assert split_question_with_deps("q", k=5, chat_model=_MockChat("bad json")) == []


# ── §P2.3 topo_levels ────────────────────────────────────────────────────────


class TestTopoLevels:
    def test_all_independent_single_wave(self):
        sqs = [SubQuestion(id=uuid4(), text=t, scope="company") for t in ("A", "B", "C")]
        levels = topo_levels(sqs)
        assert len(levels) == 1
        assert [sq.text for sq in levels[0]] == ["A", "B", "C"]

    def test_linear_chain_two_waves(self):
        a = SubQuestion(id=uuid4(), text="A", scope="company")
        b = SubQuestion(id=uuid4(), text="B", scope="company", depends_on=[a.id])
        levels = topo_levels([a, b])
        assert [[sq.text for sq in w] for w in levels] == [["A"], ["B"]]

    def test_mixed_fan_in(self):
        a = SubQuestion(id=uuid4(), text="A", scope="company")
        b = SubQuestion(id=uuid4(), text="B", scope="company")
        c = SubQuestion(id=uuid4(), text="C", scope="company", depends_on=[a.id, b.id])
        levels = topo_levels([a, b, c])
        assert [sorted(sq.text for sq in w) for w in levels] == [["A", "B"], ["C"]]

    def test_three_level_chain(self):
        a = SubQuestion(id=uuid4(), text="A", scope="company")
        b = SubQuestion(id=uuid4(), text="B", scope="company", depends_on=[a.id])
        c = SubQuestion(id=uuid4(), text="C", scope="company", depends_on=[b.id])
        levels = topo_levels([a, b, c])
        assert len(levels) == 3

    def test_dangling_dep_collapses(self):
        # depends on an unknown id → treated as satisfiable (known filter) → single wave
        a = SubQuestion(id=uuid4(), text="A", scope="company", depends_on=[uuid4()])
        levels = topo_levels([a])
        assert len(levels) == 1


# ── §P2.2 build_sub_questions ─────────────────────────────────────────────────


class TestBuildSubQuestions:
    def test_index_to_uuid_mapping(self):
        pairs = [("A", []), ("B", [0]), ("C", [0, 1])]
        sqs = build_sub_questions(pairs, scope="company")
        assert [sq.text for sq in sqs] == ["A", "B", "C"]
        assert sqs[0].depends_on == []
        assert sqs[1].depends_on == [sqs[0].id]
        assert sqs[2].depends_on == [sqs[0].id, sqs[1].id]
        assert all(sq.scope == "company" for sq in sqs)


# ── §P2.5 coordinate ──────────────────────────────────────────────────────────


class TestEnrichParts:
    def test_enriches_text_and_positional_depends_on(self):
        sqs = build_sub_questions([("A", []), ("B", [0])], scope="company")
        # SubAnswers out of order to confirm mapping is by id, not position
        sas = [
            SubAnswer(sub_question_id=sqs[1].id, grounded=False, agent_work_id=uuid4()),
            SubAnswer(sub_question_id=sqs[0].id, grounded=True),
        ]
        enriched = {sa.sub_question_id: sa for sa in _enrich_parts(sas, sqs)}
        assert enriched[sqs[0].id].text == "A"
        assert enriched[sqs[0].id].depends_on == []
        assert enriched[sqs[1].id].text == "B"
        assert enriched[sqs[1].id].depends_on == [0]  # positional index of A

    def test_unknown_sub_answer_passthrough(self):
        sqs = build_sub_questions([("A", []), ("B", [])], scope="company")
        orphan = SubAnswer(sub_question_id=uuid4(), error="leaf_error")
        out = _enrich_parts([orphan], sqs)
        assert out[0].text is None and out[0].depends_on == []


class TestCoordinatePhase2:
    def test_flag_off_always_done(self):
        """flag off → no-op done=True regardless of pending (불변식 21)."""
        a = SubQuestion(id=uuid4(), text="A", scope="company")
        b = SubQuestion(id=uuid4(), text="B", scope="company", depends_on=[a.id])
        result = coordinate([], [a, b], depends_enabled=False)
        assert isinstance(result, CoordinateResult)
        assert result.done is True
        assert result.next_wave == []

    def test_legacy_call_signature_done(self):
        """Phase 1 callers using coordinate(list) keep done=True (불변식 12)."""
        assert coordinate([SubAnswer(sub_question_id=uuid4())]).done is True

    def test_next_wave_after_first_answered(self):
        a = SubQuestion(id=uuid4(), text="A", scope="company")
        b = SubQuestion(id=uuid4(), text="B", scope="company", depends_on=[a.id])
        # A answered, B pending with dep satisfied → next_wave == [B]
        result = coordinate(
            [SubAnswer(sub_question_id=a.id, grounded=True)], [a, b], depends_enabled=True
        )
        assert result.done is False
        assert result.next_wave == [b.id]

    def test_pending_with_unmet_dep_not_ready(self):
        a = SubQuestion(id=uuid4(), text="A", scope="company")
        b = SubQuestion(id=uuid4(), text="B", scope="company", depends_on=[a.id])
        # nothing answered → A ready, B not (dep unmet)
        result = coordinate([], [a, b], depends_enabled=True)
        assert result.done is False
        assert result.next_wave == [a.id]

    def test_all_answered_done(self):
        a = SubQuestion(id=uuid4(), text="A", scope="company")
        b = SubQuestion(id=uuid4(), text="B", scope="company", depends_on=[a.id])
        result = coordinate(
            [SubAnswer(sub_question_id=a.id), SubAnswer(sub_question_id=b.id)],
            [a, b],
            depends_enabled=True,
        )
        assert result.done is True
        assert result.next_wave == []
