"""eval_qa 순수 로직(케이스 파싱·결정론 채점) 단위 테스트 — DB/LLM/audit 미경유."""

import json

from orthus.wiki.eval_qa import (
    DEFAULT_COVERAGE_THRESHOLD,
    QAEvalCase,
    _load_cases,
    _pass_mode,
    is_absence_answer,
    match_slug,
    parse_judge_output,
    score_case,
    score_slugs,
    summarize,
    summarize_judge,
)


def _case(**kwargs) -> QAEvalCase:
    defaults = {"case_id": "c1", "question": "q"}
    defaults.update(kwargs)
    return QAEvalCase(**defaults)


def test_match_slug_substring_rank():
    assert match_slug("nova", ["orbit", "nova-v1-status"]) == 2
    assert match_slug("kpi", ["kpi-management"]) == 1
    assert match_slug("absent", ["a", "b"]) is None


def test_score_slugs_any_hit_and_recall():
    got = ["nova-v1-status", "kpi-management", "orbit"]
    score = score_slugs(("nova-v1", "kpi", "missing"), got)
    assert score["any_hit"] is True
    assert score["recall"] == 2 / 3
    assert score["matched"] == {"nova-v1": 1, "kpi": 2}
    assert score["first_rank"] == 1


def test_score_slugs_empty_expected():
    score = score_slugs((), ["anything"])
    assert score["any_hit"] is False
    assert score["recall"] == 0.0


def test_is_absence_answer_markers():
    assert is_absence_answer("관련 위키 내용을 찾지 못했습니다.")
    assert is_absence_answer("해당 정보는 근거 없음 상태입니다.")
    assert is_absence_answer("연차 규정에 대한 정보가 없습니다.")
    # 일상 답변의 부정 표현만으로는 부재 판정하지 않는다.
    assert not is_absence_answer("추가 비용은 발생하지 않으며 무료입니다.")


def test_score_case_positive_pass_and_miss():
    case = _case(expected_slugs=("nova",), tags=("specific",))
    hit = score_case(case, answer_text="답", got_slugs=["nova-app"], gap_detected=False)
    assert hit["passed"] is True and hit["any_hit"] is True
    miss = score_case(case, answer_text="답", got_slugs=["orbit"], gap_detected=False)
    assert miss["passed"] is False


def test_score_case_negative_gap_or_marker():
    case = _case(expect_gap=True, tags=("negative",))
    by_gap = score_case(case, answer_text="모름", got_slugs=[], gap_detected=True)
    assert by_gap["passed"] is True and by_gap["gap_ok"] is True
    by_marker = score_case(
        case, answer_text="위키에서 찾지 못했습니다.", got_slugs=[], gap_detected=False
    )
    assert by_marker["passed"] is True
    neither = score_case(case, answer_text="연차는 15일입니다.", got_slugs=[], gap_detected=False)
    assert neither["passed"] is False


def test_pass_mode_by_tag():
    assert _pass_mode(_case(tags=("specific",)), ("broad",)) == "any_hit"
    assert _pass_mode(_case(tags=("broad",)), ("broad",)) == "coverage"
    assert _pass_mode(_case(tags=("broad",), expect_gap=True), ("broad",)) == "gap"
    assert _pass_mode(_case(tags=("followup",)), ("broad",)) == "any_hit"


def test_score_case_broad_coverage_threshold():
    # broad는 any-hit로 통과하지 않는다 — covered/total >= 0.5 여야 통과.
    case = _case(expected_slugs=("a", "b", "c", "d"), tags=("broad",))
    # 1/4 = 0.25 < 0.5 → any-hit이지만 coverage 미달로 MISS.
    low = score_case(case, answer_text="x", got_slugs=["a-page"], gap_detected=False)
    assert low["any_hit"] is True and low["coverage"] == 0.25
    assert low["pass_mode"] == "coverage" and low["passed"] is False
    # 2/4 = 0.5 >= 0.5 → PASS.
    ok = score_case(case, answer_text="x", got_slugs=["a-page", "b-page"], gap_detected=False)
    assert ok["coverage"] == 0.5 and ok["coverage_pass"] is True and ok["passed"] is True
    assert ok["coverage_threshold"] == DEFAULT_COVERAGE_THRESHOLD


def test_score_case_broad_threshold_override():
    case = _case(expected_slugs=("a", "b", "c", "d"), tags=("broad",))
    row = score_case(
        case, answer_text="x", got_slugs=["a-page"], gap_detected=False, coverage_threshold=0.25
    )
    assert row["coverage"] == 0.25 and row["passed"] is True


def test_score_case_specific_unaffected_by_coverage():
    # specific은 여전히 any-hit — coverage 필드는 출력하되 pass 판정에 안 쓴다.
    case = _case(expected_slugs=("a", "b", "c", "d"), tags=("specific",))
    row = score_case(case, answer_text="x", got_slugs=["a-page"], gap_detected=False)
    assert row["pass_mode"] == "any_hit" and row["passed"] is True and row["coverage"] == 0.25


def test_summarize_coverage_aggregates():
    rows = [
        score_case(
            _case(case_id="b1", expected_slugs=("a", "b", "c", "d"), tags=("broad",)),
            answer_text="x",
            got_slugs=["a-page", "b-page"],  # 0.5 → PASS
            gap_detected=False,
        ),
        score_case(
            _case(case_id="b2", expected_slugs=("a", "b", "c", "d"), tags=("broad",)),
            answer_text="x",
            got_slugs=["a-page"],  # 0.25 → MISS
            gap_detected=False,
        ),
        score_case(
            _case(case_id="s1", expected_slugs=("a",), tags=("specific",)),
            answer_text="x",
            got_slugs=["a-page"],
            gap_detected=False,
        ),
    ]
    summary = summarize(rows)
    broad = summary["by_tag"]["broad"]
    assert broad["coverage_cases"] == 2
    assert broad["coverage_pass"] == 1
    assert broad["coverage_pass_rate"] == 0.5
    assert broad["mean_coverage"] == 0.375  # (0.5 + 0.25) / 2
    # specific 서브셋은 coverage-mode 케이스가 없다.
    assert summary["by_tag"]["specific"]["coverage_cases"] == 0
    assert summary["by_tag"]["specific"]["coverage_pass_rate"] == 0.0


def test_summarize_by_tag_and_negative_exclusion():
    rows = [
        score_case(
            _case(case_id="s1", expected_slugs=("a",), tags=("specific",)),
            answer_text="x",
            got_slugs=["a-page"],
            gap_detected=False,
        ),
        score_case(
            _case(case_id="s2", expected_slugs=("b",), tags=("specific",)),
            answer_text="x",
            got_slugs=["c"],
            gap_detected=False,
        ),
        score_case(
            _case(case_id="n1", expect_gap=True, tags=("negative",)),
            answer_text="찾지 못했습니다",
            got_slugs=[],
            gap_detected=False,
        ),
    ]
    summary = summarize(rows)
    assert summary["cases"] == 3
    assert summary["passed"] == 2
    assert summary["by_tag"]["specific"]["pass_rate"] == 0.5
    assert summary["by_tag"]["negative"]["pass_rate"] == 1.0
    # negative는 slug 지표(any_hit/recall) 모수에서 제외된다.
    assert summary["by_tag"]["negative"]["any_hit_rate"] == 0.0
    assert summary["any_hit_rate"] == 0.5


def test_load_cases_jsonl_and_json(tmp_path):
    items = [
        {"case_id": "a", "question": "q1", "expected_slugs": ["s"], "tags": ["specific"]},
        {
            "case_id": "b",
            "question": "q2",
            "tags": ["followup"],
            "history": [{"role": "user", "text": "이전 질문"}],
        },
        {"case_id": "c", "question": "q3", "tags": ["negative"], "expect_gap": True},
    ]
    jsonl = tmp_path / "cases.jsonl"
    jsonl.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items), encoding="utf-8")
    cases = _load_cases(jsonl)
    assert [c.case_id for c in cases] == ["a", "b", "c"]
    assert cases[0].expected_slugs == ("s",)
    assert cases[1].history[0]["role"] == "user"
    assert cases[2].expect_gap is True

    as_json = tmp_path / "cases.json"
    as_json.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    assert [c.case_id for c in _load_cases(as_json)] == ["a", "b", "c"]


def test_parse_judge_output():
    ok = parse_judge_output(
        '{"accuracy": 5, "groundedness": 4, "no_hallucination": 5, "no_catalog": 3}'
    )
    assert ok == {"accuracy": 5, "groundedness": 4, "no_hallucination": 5, "no_catalog": 3}
    wrapped = parse_judge_output(
        '채점 결과: {"accuracy":1,"groundedness":1,"no_hallucination":1,"no_catalog":1} 끝'
    )
    assert wrapped is not None and wrapped["accuracy"] == 1
    assert parse_judge_output("not json") is None
    assert (
        parse_judge_output(
            '{"accuracy": 9, "groundedness": 1, "no_hallucination": 1, "no_catalog": 1}'
        )
        is None
    )
    assert parse_judge_output('{"accuracy": 5}') is None


def test_parse_judge_output_zero_is_valid_lowest_score():
    # 회귀: no_hallucination=0("환각을 실제로 발견")이 과거엔 범위(1-5) 밖으로 취급돼
    # 케이스 전체가 폐기됐다. 0은 유효한 최저점이어야 한다 — 4항목 전부.
    zeroed = parse_judge_output(
        '{"accuracy": 0, "groundedness": 0, "no_hallucination": 0, "no_catalog": 0}'
    )
    assert zeroed == {"accuracy": 0, "groundedness": 0, "no_hallucination": 0, "no_catalog": 0}
    only_hallucination_zero = parse_judge_output(
        '{"accuracy": 4, "groundedness": 4, "no_hallucination": 0, "no_catalog": 4}'
    )
    assert only_hallucination_zero is not None
    assert only_hallucination_zero["no_hallucination"] == 0


def test_parse_judge_output_invalid_inputs_still_rejected():
    # 0을 허용하게 넓혔다고 다른 무효 입력까지 통과시키면 안 된다: 범위 밖(6+/음수),
    # 비정수, bool, 키 누락은 여전히 명확히 None으로 처리(폐기 사유는 run_judge가 남김).
    assert (
        parse_judge_output(
            '{"accuracy": 6, "groundedness": 1, "no_hallucination": 1, "no_catalog": 1}'
        )
        is None
    )
    assert (
        parse_judge_output(
            '{"accuracy": -1, "groundedness": 1, "no_hallucination": 1, "no_catalog": 1}'
        )
        is None
    )
    assert (
        parse_judge_output(
            '{"accuracy": "high", "groundedness": 1, "no_hallucination": 1, "no_catalog": 1}'
        )
        is None
    )
    assert (
        parse_judge_output(
            '{"accuracy": true, "groundedness": 1, "no_hallucination": 1, "no_catalog": 1}'
        )
        is None
    )
    assert parse_judge_output('{"accuracy": 1, "groundedness": 1, "no_hallucination": 1}') is None
    assert parse_judge_output("완전히 텍스트뿐") is None


def test_summarize_judge_counts_attempted_scored_dropped_with_reasons():
    rows = [
        {"judge": {"accuracy": 5, "groundedness": 5, "no_hallucination": 0, "no_catalog": 5}},
        {"judge": {"accuracy": 4, "groundedness": 4, "no_hallucination": 4, "no_catalog": 4}},
        {"judge": {"error": "parse", "raw": "not json"}},
        {"judge": {"error": "parse", "raw": "still not json"}},
        {"judge": {"error": "call:TimeoutError"}},
        {},  # judge 미실행(judge=False 또는 answer_text 빈 케이스) — attempted에서 제외.
    ]
    agg = summarize_judge(rows)
    assert agg["judge_attempted"] == 5
    assert agg["judge_scored"] == 2
    assert agg["judge_dropped"] == 3
    assert agg["judge_dropped_reasons"] == {"parse": 2, "call:TimeoutError": 1}


def test_summarize_judge_no_attempts_is_all_zero():
    rows = [{"judge": None}, {}]
    agg = summarize_judge(rows)
    assert agg == {
        "judge_attempted": 0,
        "judge_scored": 0,
        "judge_dropped": 0,
        "judge_dropped_reasons": {},
    }


def test_summarize_exposes_judge_dropped_count():
    # summarize()의 최종 summary가 judge dropped를 침묵 없이 노출한다(top-level 통합).
    base = _case(case_id="s1", expected_slugs=("a",), tags=("specific",))
    rows = [
        {
            **score_case(base, answer_text="x", got_slugs=["a-page"], gap_detected=False),
            "judge": {"accuracy": 0, "groundedness": 0, "no_hallucination": 0, "no_catalog": 0},
        },
        {
            **score_case(base, answer_text="x", got_slugs=["a-page"], gap_detected=False),
            "judge": {"error": "parse", "raw": "garbage"},
        },
    ]
    summary = summarize(rows)
    assert summary["judge_attempted"] == 2
    assert summary["judge_scored"] == 1
    assert summary["judge_dropped"] == 1
    assert summary["judge_dropped_reasons"] == {"parse": 1}
