"""SVC — structured 검증 캐스케이드 (experiments/fugu-ko/analysis/
structured-verification-cascade.md).

검증 게이트는 "유효하지 않은 SQL"은 막지만 "유효한데 틀린 SQL"은 통과시킨다. 실행 결과의
결정론 속성(게이트 실패 / 미실행 / 0행 / 결과 정수집합 == {0})만 보고 2차 모델로 한 번 더
묻는다. 모델 confidence는 보지 않는다.

고정하는 계약:
- `_retry_signal` 트리거 4종 + 미발동 (순수 함수, LLM 0회)
- 2차 채택은 gate_fail/not_executed/empty_rows에서만. `zero_answer`(1차의 확신한 0)는 채택
  비적격 → 2차를 아예 부르지 않고 1차의 0을 유지한다(R3, svc-cascade-verify-results.md
  §3.6-3.7 — 진짜-0 질문에서 2차 비-0 override로 인한 오발동 차단). shadow 스팬으로 관측만.
- 2차도 신호가 뜨면 1차 유지 ("진짜 0"인 답 보존)
- **기능 off(기본값)에서 기존 동작 완전 불변** — 신호 계산도, 2차 호출도, audit 스팬도 없다
- 명시 주입된 `chat_model`(실험/테스트 하네스)에는 개입하지 않는다
- 2차도 동일한 검증 게이트(`_query_structured`)를 그대로 통과한다 — 우회 경로 없음
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from decimal import Decimal

import pytest

from orthus.schemas.canonical import AssistantResult, CompiledQuery, ValidationResult
from orthus.structured import query as query_mod
from orthus.structured.query import _retry_signal, query_structured

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

_PASSED = ValidationResult(
    parsed=True,
    statement_kind="select",
    schema_ok=True,
    read_only_ok=True,
    explain_ok=True,
    injected_limit=1000,
)
_REJECTED = ValidationResult(rejected_reason="schema_ok_failed:no such column")


def _result(
    *,
    status: str = "executed",
    validation: ValidationResult = _PASSED,
    columns: list[str] | None = None,
    rows: list[list] | None = None,
    row_count: int | None = None,
) -> AssistantResult:
    rows = rows if rows is not None else [[7]]
    return AssistantResult(
        query_id=uuid.uuid4(),
        question="q",
        source_id=None,
        compiled=CompiledQuery(question="q", source_id=None, dialect="postgres", sql="SELECT 1"),
        validation=validation,
        status=status,
        columns=columns or ["count"],
        rows=rows,
        row_count=row_count if row_count is not None else len(rows),
    )


class _FakeSpan:
    def __init__(self) -> None:
        self.meta: dict = {}

    def add_meta(self, **kw) -> None:
        self.meta.update(kw)

    def set_output(self, output) -> None:  # pragma: no cover - unused
        pass


@pytest.fixture
def spans(monkeypatch):
    """No-DB audit: record every span name + meta instead of writing audit_log."""
    recorded: list[tuple[str, _FakeSpan]] = []

    @contextmanager
    def _fake_audit(node: str, *, correlation_id=None):
        span = _FakeSpan()
        recorded.append((node, span))
        yield span

    monkeypatch.setattr(query_mod, "audit", _fake_audit)
    return recorded


@pytest.fixture
def runs(monkeypatch):
    """Stub the (unchanged) single-shot path. Each call pops the next canned result,
    so the wrapper's decisions are observable without a DB or an LLM."""
    calls: list[dict] = []
    queue: list[AssistantResult] = []

    def _fake_query_structured(query_id, user_id, question, scope, project, chat_model):
        calls.append({"query_id": query_id, "chat_model": chat_model, "question": question})
        if not queue:
            raise AssertionError("_query_structured called more times than canned results")
        return queue.pop(0)

    monkeypatch.setattr(query_mod, "_query_structured", _fake_query_structured)
    return {"calls": calls, "queue": queue}


def _configure(monkeypatch, **kw):
    """Override settings for this test only (get_settings is cached)."""
    settings = query_mod.get_settings().model_copy(update=kw)
    monkeypatch.setattr(query_mod, "get_settings", lambda: settings)
    return settings


# ---------------------------------------------------------------------------
# _retry_signal — 순수 함수, 트리거 4종 + 미발동
# ---------------------------------------------------------------------------


def test_signal_gate_fail():
    assert _retry_signal(_result(status="rejected", validation=_REJECTED)) == "gate_fail"


def test_signal_not_executed():
    # 게이트는 통과했는데 실행이 실패한 경우 (status="failed").
    assert _retry_signal(_result(status="failed", rows=[], row_count=None)) == "not_executed"


def test_signal_empty_rows():
    assert _retry_signal(_result(rows=[], row_count=0)) == "empty_rows"


def test_signal_zero_answer():
    # 유효 SQL + 게이트 통과 + 실행 성공인데 결과가 0 — E1의 대표 실패 (없는 컬럼을 지어냄).
    assert _retry_signal(_result(rows=[[0]])) == "zero_answer"


def test_signal_zero_answer_decimal_and_label_columns():
    # PG count는 Decimal로 돌아올 수 있고, 라벨 컬럼이 섞여도 숫자집합이 {0}이면 발동한다.
    assert _retry_signal(_result(rows=[["열린", Decimal("0")]])) == "zero_answer"


def test_signal_none_when_healthy():
    assert _retry_signal(_result(rows=[[28]])) is None


def test_signal_none_when_some_number_is_nonzero():
    # 0이 섞여 있어도 다른 숫자가 있으면 정상 결과다 (그룹별 집계에 0이 있는 건 정상).
    assert _retry_signal(_result(rows=[["열린", 0], ["닫힘", 5]])) is None


def test_signal_none_for_text_only_rows():
    # 숫자가 아예 없는 목록형 결과는 zero_answer가 아니다.
    assert _retry_signal(_result(columns=["이름"], rows=[["박기획"]])) is None


def test_signal_ignores_bool_false():
    # bool은 숫자가 아니다 — False가 0으로 접혀 오발동하면 안 된다.
    assert _retry_signal(_result(columns=["활성"], rows=[[False]])) is None


# ---------------------------------------------------------------------------
# 기능 off (기본값) = 기존 동작 완전 불변  ★ 가장 중요한 회귀
# ---------------------------------------------------------------------------


def test_off_by_default_no_signal_no_retry_no_span(monkeypatch, spans, runs):
    """ORTHUS_LLM_FALLBACK_MODEL 미설정 = 기능 off. 1차만 실행하고, fallback 스팬도 없다."""
    _configure(monkeypatch, llm_fallback_model="")
    zero = _result(rows=[[0]])  # 트리거가 뜨는 결과인데도
    runs["queue"].append(zero)

    fallback_calls: list = []
    monkeypatch.setattr(
        query_mod, "get_fallback_chat_model", lambda: fallback_calls.append(1) or None
    )

    out = query_structured(uuid.uuid4(), "회사 미팅 기록 총 몇 건?")

    assert out is zero  # 1차 결과 그대로 (동일 객체)
    assert len(runs["calls"]) == 1  # 재질의 없음
    assert fallback_calls == []  # 2차 모델을 만들지도 않는다
    assert [name for name, _ in spans] == ["assistant.run"]  # 스팬도 1차 것만


def test_explicit_chat_model_is_never_intercepted(monkeypatch, spans, runs):
    """chat_model이 명시 주입된 호출(실험/테스트 하네스)에는 개입하지 않는다 —
    fallback이 설정돼 있어도 단일 모델 측정이 오염되면 안 된다."""
    _configure(monkeypatch, llm_fallback_model="solar-pro", structured_fallback_mode="on")
    zero = _result(rows=[[0]])
    runs["queue"].append(zero)
    monkeypatch.setattr(
        query_mod, "get_fallback_chat_model", lambda: pytest.fail("fallback must not be built")
    )

    injected = object()
    out = query_structured(uuid.uuid4(), "티켓 개수", chat_model=injected)

    assert out is zero
    assert len(runs["calls"]) == 1
    assert runs["calls"][0]["chat_model"] is injected
    assert [name for name, _ in spans] == ["assistant.run"]


def test_unknown_mode_fails_closed(monkeypatch, spans, runs):
    _configure(monkeypatch, llm_fallback_model="solar-pro", structured_fallback_mode="ON!!")
    zero = _result(rows=[[0]])
    runs["queue"].append(zero)

    out = query_structured(uuid.uuid4(), "티켓 개수")

    assert out is zero
    assert len(runs["calls"]) == 1


def test_mode_on_but_fallback_model_unbuildable_keeps_primary(monkeypatch, spans, runs):
    """provider 슬롯이 2차를 못 만들면(예: llm=cli) fail-closed로 1차를 유지한다."""
    _configure(monkeypatch, llm_fallback_model="solar-pro", structured_fallback_mode="on")
    zero = _result(rows=[[0]])
    runs["queue"].append(zero)
    monkeypatch.setattr(query_mod, "get_fallback_chat_model", lambda: None)

    out = query_structured(uuid.uuid4(), "티켓 개수")

    assert out is zero
    assert len(runs["calls"]) == 1
    assert "structured.fallback" not in [name for name, _ in spans]


# ---------------------------------------------------------------------------
# 캐스케이드 발동
# ---------------------------------------------------------------------------


@pytest.fixture
def fallback_on(monkeypatch):
    _configure(
        monkeypatch,
        llm="solar",
        llm_solar_api_key="k-test",
        llm_fallback_model="solar-mini",
        structured_fallback_mode="on",
    )
    sentinel = object()
    monkeypatch.setattr(query_mod, "get_fallback_chat_model", lambda: sentinel)

    # 1차 모델 해소는 #669의 작업별 배정을 탄다(`get_chat_model_for(TASK_STRUCTURED)`).
    # 이 스위트는 캐스케이드 로직만 보므로 배정 결과를 고정한다 — 배정×캐스케이드 조합은
    # 아래 `orchestration_on` 스위트가 실제 경로로 검증한다.
    class _Primary:
        model_id = "solar-pro"

    monkeypatch.setattr(query_mod, "get_chat_model_for", lambda task: _Primary())
    return sentinel


def test_no_signal_no_second_call(monkeypatch, spans, runs, fallback_on):
    """1차가 멀쩡하면 재질의하지 않는다 — 비용은 트리거 발동 시에만 발생한다."""
    healthy = _result(rows=[[28]])
    runs["queue"].append(healthy)

    out = query_structured(uuid.uuid4(), "회사 미팅 기록 총 몇 건?")

    assert out is healthy
    assert len(runs["calls"]) == 1
    assert [name for name, _ in spans] == ["assistant.run"]


@pytest.mark.parametrize(
    ("primary", "trigger"),
    [
        (_result(status="rejected", validation=_REJECTED), "gate_fail"),
        (_result(status="failed", rows=[], row_count=None), "not_executed"),
        (_result(rows=[], row_count=0), "empty_rows"),
    ],
)
def test_each_trigger_retries_and_adopts_healthy_second(
    monkeypatch, spans, runs, fallback_on, primary, trigger
):
    """채택 적격 트리거 3종(gate_fail/not_executed/empty_rows) 각각이 2차 재질의를 발동하고,
    2차가 멀쩡하면 2차를 채택한다. `zero_answer`는 채택 비적격이라 별도(아래)에서 검증한다."""
    healthy = _result(rows=[[28]])
    runs["queue"].extend([primary, healthy])

    out = query_structured(uuid.uuid4(), "회사 미팅 기록 총 몇 건?")

    assert out is healthy
    assert len(runs["calls"]) == 2
    # 2차는 fallback 모델로, 자체 query_id를 갖는 정규 run으로 돌아야 한다.
    assert runs["calls"][1]["chat_model"] is fallback_on
    assert runs["calls"][0]["query_id"] != runs["calls"][1]["query_id"]
    # 2차도 동일한 검증 게이트(_query_structured)를 그대로 탄다 — 우회 경로 없음.
    assert [name for name, _ in spans] == [
        "assistant.run",
        "structured.fallback",
        "assistant.run",
    ]
    meta = dict(spans[1][1].meta)
    assert meta["trigger"] == trigger
    assert meta["adopted"] is True
    assert meta["primary_model"] == "solar-pro"
    assert meta["fallback_model"] == "solar-mini"
    assert meta["fallback_trigger"] is None


def test_second_also_empty_keeps_primary(monkeypatch, spans, runs, fallback_on):
    """★ 오발동 방어: 2차도 신호가 뜨면 1차를 유지한다 — 두 모델이 독립적으로 "없다"고
    하면 진짜 0인 답으로 본다(설계 §6.1). 채택 적격 트리거(empty_rows)로 검증한다."""
    primary = _result(rows=[], row_count=0)
    second = _result(rows=[], row_count=0)
    runs["queue"].extend([primary, second])

    out = query_structured(uuid.uuid4(), "다음 주 마감 티켓 몇 개?")

    assert out is primary  # 2차를 채택하지 않는다
    assert len(runs["calls"]) == 2
    meta = dict(spans[1][1].meta)
    assert meta["trigger"] == "empty_rows"
    assert meta["adopted"] is False
    assert meta["fallback_trigger"] == "empty_rows"


def test_zero_answer_is_not_adoption_eligible_keeps_primary(monkeypatch, spans, runs, fallback_on):
    """★ R3(svc-cascade-verify-results.md §3.6-3.7): `zero_answer`(1차의 확신한 0)는 채택
    비적격이라 2차를 아예 부르지 않고 1차의 0을 유지한다. 진짜-0 질문에서 2차의 비-0 override로
    인한 오발동을 원천 차단한다. shadow 스팬으로 관측만 남긴다(추가 LLM 호출 0)."""
    primary = _result(rows=[[0]])  # zero_answer
    runs["queue"].append(primary)
    monkeypatch.setattr(
        query_mod,
        "get_fallback_chat_model",
        lambda: pytest.fail("zero_answer는 2차를 만들지도 부르지도 않아야 한다"),
    )

    out = query_structured(uuid.uuid4(), "박기획인 파트너 몇 명?")

    assert out is primary  # 1차의 확신한 0 유지
    assert len(runs["calls"]) == 1  # 재질의 없음
    assert [name for name, _ in spans] == ["assistant.run", "structured.fallback_shadow"]
    meta = dict(spans[1][1].meta)
    assert meta["trigger"] == "zero_answer"
    assert meta["adopted"] is False
    assert meta["retry_skipped"] == "non_adopt_trigger"


def test_second_rejected_by_gate_keeps_primary(monkeypatch, spans, runs, fallback_on):
    """2차가 게이트에서 reject되면 그 SQL은 실행되지 않았고, 채택되지도 않는다."""
    primary = _result(rows=[], row_count=0)  # empty_rows (채택 적격)
    second = _result(status="rejected", validation=_REJECTED)
    runs["queue"].extend([primary, second])

    out = query_structured(uuid.uuid4(), "티켓 개수")

    assert out is primary
    assert dict(spans[1][1].meta)["adopted"] is False


def test_fallback_exception_keeps_primary(monkeypatch, spans, runs, fallback_on):
    """2차 실행이 통째로 터져도 1차 답은 살아남는다(fail-open to current behavior)."""
    primary = _result(rows=[], row_count=0)  # empty_rows (채택 적격)
    runs["queue"].append(primary)

    calls = {"n": 0}
    original = query_mod._query_structured

    def _boom(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(*a, **kw)
        raise RuntimeError("upstream 502")

    monkeypatch.setattr(query_mod, "_query_structured", _boom)

    out = query_structured(uuid.uuid4(), "티켓 개수")

    assert out is primary
    meta = dict(spans[1][1].meta)
    assert meta["adopted"] is False
    assert meta["fallback_error"] == "RuntimeError"


# ---------------------------------------------------------------------------
# shadow 모드 (SVC.0 관측 전용)
# ---------------------------------------------------------------------------


def test_shadow_mode_records_trigger_without_retrying(monkeypatch, spans, runs):
    _configure(monkeypatch, structured_fallback_mode="shadow", llm_fallback_model="")
    monkeypatch.setattr(
        query_mod, "get_fallback_chat_model", lambda: pytest.fail("shadow must not build a model")
    )
    primary = _result(rows=[[0]])
    runs["queue"].append(primary)

    out = query_structured(uuid.uuid4(), "티켓 개수")

    assert out is primary
    assert len(runs["calls"]) == 1  # 추가 LLM 호출 0
    assert [name for name, _ in spans] == ["assistant.run", "structured.fallback_shadow"]
    assert dict(spans[1][1].meta)["trigger"] == "zero_answer"


def test_shadow_mode_silent_when_healthy(monkeypatch, spans, runs):
    _configure(monkeypatch, structured_fallback_mode="shadow", llm_fallback_model="")
    runs["queue"].append(_result(rows=[[28]]))

    query_structured(uuid.uuid4(), "티켓 개수")

    assert [name for name, _ in spans] == ["assistant.run"]


# ---------------------------------------------------------------------------
# read→act 게이트: zero-answer 상위 결과 위에서 **명령**을 실행하지 않는다
# (`structured_zero_blocks_dependents` — kill switch, default ON)
#
# 명령/읽기 판정은 결정론 키워드 감지기(`detect_assistant_command_action`, LLM 0회).
# 읽기 의존 리프는 막지 않는다 → 정보 손실 없음.
# ---------------------------------------------------------------------------

# 감지기가 실제로 명령으로 판정하는 문장 / 읽기로 판정하는 문장(결정론이라 고정 가능).
_COMMAND_LEAF = "owner01@nova.example 로 그 결과 메일 보내줘"
_READ_LEAF = "그럼 그 티켓 목록 알려줘"


def test_command_detector_is_deterministic_and_llm_free():
    """게이트가 기대는 전제를 고정한다: 명령/읽기 판정이 LLM 없이 갈린다."""
    from orthus.agentwork import detect_assistant_command_action

    assert detect_assistant_command_action(_COMMAND_LEAF) is not None
    assert detect_assistant_command_action(_READ_LEAF) is None


def _sub_answer_with(result: AssistantResult):
    from orthus.schemas.canonical import RoutedAnswer, SubAnswer

    return SubAnswer(
        sub_question_id=uuid.uuid4(),
        routed=RoutedAnswer(question="q", mode="structured", structured=result),
        grounded=result.status != "rejected",  # 현행 규칙 그대로
    )


def _dependent_on(up, text: str):
    from orthus.schemas.canonical import SubQuestion

    return SubQuestion(id=uuid.uuid4(), text=text, scope="all", depends_on=[up.sub_question_id])


def _gate(monkeypatch, *, enabled: bool):
    from orthus.router import decompose as dc

    settings = dc.get_settings().model_copy(update={"structured_zero_blocks_dependents": enabled})
    monkeypatch.setattr(dc, "get_settings", lambda: settings)
    return dc


def test_default_on_blocks_command_dependent_on_zero_answer(monkeypatch):
    """★ 기본값(kill switch ON)에서 발동: 확신에 찬 0 위에서 명령을 실행하지 않는다.
    settings override 없이 실제 default를 쓴다."""
    from orthus.router import decompose as dc
    from orthus.settings import Settings

    assert Settings().structured_zero_blocks_dependents is True  # default ON

    zero = _sub_answer_with(_result(rows=[[0]]))
    empty = _sub_answer_with(_result(rows=[], row_count=0))
    gate_fail = _sub_answer_with(_result(status="rejected", validation=_REJECTED))

    assert dc._deps_unavailable(_dependent_on(zero, _COMMAND_LEAF), {zero.sub_question_id: zero})
    assert dc._deps_unavailable(_dependent_on(empty, _COMMAND_LEAF), {empty.sub_question_id: empty})
    # gate_fail 상위는 애초에 grounded=False라 기존 규칙만으로도 막힌다(회귀 확인).
    assert dc._deps_unavailable(
        _dependent_on(gate_fail, _COMMAND_LEAF), {gate_fail.sub_question_id: gate_fail}
    )
    # 상위 sub-answer는 여전히 grounded — synthesize 입력에서 빠지지 않는다(조용한 누락 방지).
    assert zero.grounded is True


def test_read_dependent_on_zero_answer_is_not_blocked(monkeypatch):
    """★ 이번 변경의 핵심 회귀: **읽기** 의존 리프는 zero 상위여도 정상 실행된다.
    (전면 차단이었다면 여기서 정보가 사라졌다.)"""
    dc = _gate(monkeypatch, enabled=True)

    zero = _sub_answer_with(_result(rows=[[0]]))
    empty = _sub_answer_with(_result(rows=[], row_count=0))

    assert (
        dc._deps_unavailable(_dependent_on(zero, _READ_LEAF), {zero.sub_question_id: zero}) is False
    )
    assert (
        dc._deps_unavailable(_dependent_on(empty, _READ_LEAF), {empty.sub_question_id: empty})
        is False
    )


def test_healthy_upstream_still_licenses_command(monkeypatch):
    """과잉 차단 없음 — 멀쩡한 structured 상위는 명령 리프를 계속 허가한다."""
    dc = _gate(monkeypatch, enabled=True)

    healthy = _sub_answer_with(_result(rows=[[28]]))
    assert (
        dc._deps_unavailable(
            _dependent_on(healthy, _COMMAND_LEAF), {healthy.sub_question_id: healthy}
        )
        is False
    )


def test_kill_switch_off_restores_previous_behavior(monkeypatch):
    """kill switch를 끄면 zero 상위여도 명령 리프가 실행된다(구 동작)."""
    dc = _gate(monkeypatch, enabled=False)

    zero = _sub_answer_with(_result(rows=[[0]]))
    assert (
        dc._deps_unavailable(_dependent_on(zero, _COMMAND_LEAF), {zero.sub_question_id: zero})
        is False
    )


def test_gate_uses_no_llm(monkeypatch):
    """게이트 경로에서 LLM 슬롯을 건드리지 않는다(결정론 보장)."""
    from orthus.router import decompose as dc

    monkeypatch.setattr(
        dc, "get_chat_model_for", lambda task: pytest.fail("게이트는 LLM을 부르지 않는다")
    )

    zero = _sub_answer_with(_result(rows=[[0]]))
    assert dc._deps_unavailable(_dependent_on(zero, _COMMAND_LEAF), {zero.sub_question_id: zero})


# ---------------------------------------------------------------------------
# 작업별 모델 배정(#669) × 검증 캐스케이드(SVC) — 두 층이 함께 돈다
#
# 겹치는 구조가 의도된 것이다(실험 리포트 "(b) 배정 위에 (d) 캐스케이드"):
#   1차   = get_chat_model_for(TASK_STRUCTURED)   ← 배정. off면 기존 get_chat_model()
#   재질의 = get_fallback_chat_model()             ← 캐스케이드
#
# 이 테스트가 없으면 다음 사람이 둘 중 하나를 무심코 죽인다. 특히 배정을 켜면
# zero-answer가 **늘어난다**(E2 실측: solar 18% vs baseline 11%) — 캐스케이드는
# 배정과 함께 살아 있어야 하는 안전장치다.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_pipeline(monkeypatch):
    """`_query_structured` 본문은 그대로 두고 그 **바깥 경계**(catalog/DB/실행)만 스텁한다.
    → 1차 모델 해소(get_chat_model_for)가 실제로 일어나고, compile에 넘어간 모델을 관찰할 수 있다."""
    compiled = CompiledQuery(
        question="q", source_id=None, dialect="postgres", sql="SELECT count(*) FROM notion_rows"
    )
    seen: list[str] = []
    rows_queue: list[list[list]] = []

    def _spy_compile(question, source_id, dialect, catalog, examples, chat_model):
        seen.append(str(getattr(chat_model, "model_id", chat_model)))
        return compiled

    monkeypatch.setattr(query_mod, "build_notion_catalog", lambda *a, **kw: {})
    monkeypatch.setattr(query_mod, "insert_run", lambda *a, **kw: None)
    monkeypatch.setattr(query_mod, "update_run", lambda *a, **kw: None)
    monkeypatch.setattr(query_mod, "grounding_for", lambda *a, **kw: [])
    monkeypatch.setattr(query_mod, "validate", lambda *a, **kw: (_PASSED, compiled.sql))
    monkeypatch.setattr(query_mod, "_inject_scope_filter", lambda sql, *a, **kw: sql)
    monkeypatch.setattr(query_mod, "compile_query", _spy_compile)
    monkeypatch.setattr(
        query_mod,
        "execute_readonly",
        lambda *a, **kw: (["count"], rows_queue.pop(0), 1),
    )
    return {"models": seen, "rows": rows_queue}


@pytest.fixture
def orchestration_on(monkeypatch):
    """배정 ON + solar 워커 설정 + 캐스케이드 ON(2차 = 교차 모델).

    settings는 캐시된 단일 객체라 필드를 직접 세팅한다 — orchestration 모듈도 같은
    객체를 읽어야 하기 때문(query_mod.get_settings만 갈아끼우면 배정이 안 켜진다).
    """
    from orthus.settings import get_settings as real_get_settings

    s = real_get_settings()
    for field, value in {
        "llm": "solar",  # mock이면 배정 자체가 무효(테스트가 실 API를 안 부르는 보증)
        "model_orchestration_enabled": True,
        "llm_solar_api_key": "up-test",
        "llm_solar_model": "solar-pro",
        "llm_fallback_model": "solar-mini",
        "llm_fallback_provider": "solar",
        "structured_fallback_mode": "on",
    }.items():
        monkeypatch.setattr(s, field, value)
    return s


def test_orchestration_on_primary_is_assigned_and_retry_uses_fallback(
    monkeypatch, spans, real_pipeline, orchestration_on
):
    """★ 배정 ON에서 1차 = solar(배정), 재질의 = 폴백 슬롯. 둘 다 살아 있다.
    채택 적격 트리거(empty_rows)로 검증한다 — zero_answer는 재질의를 안 하므로(R3)."""
    real_pipeline["rows"].extend([[], [[19]]])  # 1차 = 빈 결과(empty_rows) → 2차 = 19

    out = query_structured(uuid.uuid4(), "NOVA 영입 후보 리스트에 총 몇 명이야?")

    # 1차는 배정된 워커, 2차는 폴백 슬롯 — 한쪽이 죽으면 여기서 잡힌다.
    assert real_pipeline["models"] == ["solar-pro", "solar-mini"]
    assert out.rows == [[19]]  # 캐스케이드가 빈 결과를 회수했다
    fallback_span = dict(spans[1][1].meta)
    assert fallback_span["trigger"] == "empty_rows"
    assert fallback_span["adopted"] is True
    # audit이 **실제** 1차 모델을 기록한다(기본 모델명이 아니라 배정된 워커).
    assert fallback_span["primary_model"] == "solar-pro"
    assert fallback_span["fallback_model"] == "solar-mini"


def test_orchestration_on_healthy_primary_never_calls_fallback(
    monkeypatch, spans, real_pipeline, orchestration_on
):
    """배정만으로 답이 멀쩡하면 2차는 호출되지 않는다 — 비용은 트리거 발동 시에만."""
    real_pipeline["rows"].append([[19]])

    out = query_structured(uuid.uuid4(), "NOVA 영입 후보 리스트에 총 몇 명이야?")

    assert real_pipeline["models"] == ["solar-pro"]  # 1차뿐
    assert out.rows == [[19]]
    assert [name for name, _ in spans] == ["assistant.run"]


def test_orchestration_off_primary_is_default_model_cascade_still_works(
    monkeypatch, spans, real_pipeline, orchestration_on
):
    """배정 OFF에서도 캐스케이드는 그대로 돈다 — 1차는 기본 슬롯(solar-pro).
    채택 적격 트리거(empty_rows)로 검증한다(zero_answer는 R3에서 재질의 안 함)."""
    monkeypatch.setattr(orchestration_on, "model_orchestration_enabled", False)
    real_pipeline["rows"].extend([[], [[19]]])

    out = query_structured(uuid.uuid4(), "NOVA 영입 후보 리스트에 총 몇 명이야?")

    assert real_pipeline["models"] == ["solar-pro", "solar-mini"]
    assert out.rows == [[19]]
    assert dict(spans[1][1].meta)["primary_model"] == "solar-pro"


def test_cascade_off_leaves_assignment_untouched(
    monkeypatch, spans, real_pipeline, orchestration_on
):
    """캐스케이드만 끄면(폴백 모델 미설정) 배정은 그대로 살아 있고, 재질의만 사라진다."""
    monkeypatch.setattr(orchestration_on, "llm_fallback_model", "")
    real_pipeline["rows"].append([[0]])  # 가짜 0이 그대로 나간다(캐스케이드 off)

    out = query_structured(uuid.uuid4(), "NOVA 영입 후보 리스트에 총 몇 명이야?")

    assert real_pipeline["models"] == ["solar-pro"]  # 배정은 살아 있다
    assert out.rows == [[0]]
    assert [name for name, _ in spans] == ["assistant.run"]  # 재질의 없음
