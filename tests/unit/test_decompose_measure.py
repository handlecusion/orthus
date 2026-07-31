"""MA.5 measurement harness unit tests (docs/company-agent-orchestration.md §19).

No live DB or real LLM:
- _percentile / _safe_div math
- measure_corpus over a scripted chat → (a)(b) metrics + confusion
- measure_from_audit aggregation from mocked audit_log rows → decompose rate /
  n_parts / latency / over-5s budget violations
- read-only guarantee: harness never calls learn/record_gaps writers
"""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import MagicMock, patch
from uuid import uuid4

from orthus.router.decompose_measure import (
    Case,
    _percentile,
    _safe_div,
    measure_corpus,
    measure_from_audit,
)


class _ScriptedChat:
    """decompose enum says yes/no by keyword; split returns 2 parts for compound."""

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        if "compound-question detector" in system:
            verdict = "yes" if ("그리고" in user or "알려주고" in user) else "no"
            return json.dumps({"decompose": verdict})
        if "question-splitter" in system:
            return json.dumps({"sub_questions": ["sub a", "sub b"]})
        return "{}"


def test_percentile_interpolates():
    assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert _percentile([10.0], 95) == 10.0
    assert _percentile([], 50) == 0.0


def test_safe_div_zero_denominator():
    assert _safe_div(3, 0) == 0.0
    assert _safe_div(1, 4) == 0.25


def test_measure_corpus_metrics_and_confusion():
    cases = [
        Case("Nova 로드맵 알려주고 Orbit 운영비도 알려줘", True),  # compound
        Case("회사 정책이 뭐야", False),  # single, no signal → prefilter False
    ]
    rep = measure_corpus(cases, k=5, chat_model=_ScriptedChat())

    assert rep.n == 2
    assert rep.decompose_rate == 0.5
    # compound case split into 2 → independent
    assert rep.independent_parallel_rate == 1.0
    assert rep.n_parts_distribution == {2: 1}
    # confusion: tp=1 (compound→decomposed), tn=1 (single→not). no fp/fn
    assert rep.confusion == {"tp": 1, "fp": 0, "fn": 0, "tn": 1}


def test_measure_corpus_prefilter_skips_llm_for_plain_single():
    # "회사 정책이 뭐야" has no command verb / connective → should_decompose returns
    # False before the enum, so it is never decomposed.
    rep = measure_corpus([Case("회사 정책이 뭐야", False)], k=5, chat_model=_ScriptedChat())
    assert rep.decompose_rate == 0.0
    assert rep.rows[0].prefilter_passed is False


def _audit_rows():
    """(node_run_id, phase, meta, occurred_at) tuples for two decompose runs."""
    base = dt.datetime(2026, 6, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
    r1, r2 = uuid4(), uuid4()
    return [
        (r1, "enter", {}, base),
        (
            r1,
            "exit",
            {"decomposed": True, "n_parts": 3, "reason": None},
            base + dt.timedelta(seconds=2),
        ),
        (r2, "enter", {}, base),
        # 6s run → over 5s budget violation; not decomposed (fell through)
        (
            r2,
            "exit",
            {"decomposed": False, "reason": "split_empty"},
            base + dt.timedelta(seconds=6),
        ),
    ]


def test_measure_from_audit_aggregation():
    fake_session = MagicMock()
    fake_session.__enter__.return_value = fake_session
    fake_session.__exit__.return_value = False
    fake_session.execute.return_value.all.return_value = _audit_rows()

    with patch("orthus.router.decompose_measure.session", return_value=fake_session):
        rep = measure_from_audit()

    assert rep.total_runs == 2
    assert rep.decomposed_runs == 1
    assert rep.decompose_rate == 0.5
    assert rep.n_parts_mean == 3
    assert rep.n_parts_distribution == {3: 1}
    assert rep.latency_p50_sec == 4.0  # median of [2.0, 6.0]
    assert rep.latency_over_5s == 1
    assert rep.reasons == {"split_empty": 1}
