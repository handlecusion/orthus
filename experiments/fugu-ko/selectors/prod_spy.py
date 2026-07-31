"""arm `assign_final` — **프로덕션 배정 계층 그 자체**를 태우고 관측만 한다.

기존 `assign` arm은 실험용 `PromptKeyedSelectorChat`이 system 프롬프트 프리픽스를 보고 워커를
고른다(프로덕션 diff 0줄이 목적이었다). 그러나 그 배정표(`selectors/static_map.py`)는 **1차안**이고,
홀드아웃 재검증에서 기각됐다(라우팅→A.X). 최종 배정은 이미 프로덕션 코드에 있다
(`orthus/models/orchestration.py::ASSIGNMENTS`, PR #696).

그래서 재측정은 실험 셀렉터를 고쳐 쓰지 않는다. **`chat_model=None`으로 호출해 프로덕션이
자기 배정표로 모델을 고르게 두고**, 이 스파이는 그 선택을 기록만 한다. 이렇게 하면 측정 대상이
"실험이 흉내 낸 배정"이 아니라 **켤 때 실제로 도는 코드**가 된다 — 배정표뿐 아니라 폴백 사다리·
재시도 예산(`_WORKER_RETRIES=1`)·RPS 간격까지 포함해서.

무효 조건(§4.1의 프로덕션 등가물): 루프가 부른 LLM 작업 중 **배정이 물리지 않은 콜이 1건이라도
있으면 그 측정은 무효**다. `get_chat_model_for(task)`가 `FallbackChat`이 아닌 것을 돌려주면
(= 플래그 off / 키 미설정 / 미배정 작업) 기본 슬롯이 답한 것이므로 `unmatched`에 쌓는다.

관측 방식: 각 호출부는 `from ... import get_chat_model_for`로 **직접 참조**를 들고 있으므로
원본 모듈만 패치하면 안 잡힌다. 호출부 모듈들의 심볼을 각각 감싼다(동작 불변 — 원본을 그대로
호출하고 반환값만 기록용으로 감싼다).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from orthus.models import orchestration as orch

# `get_chat_model_for`를 import해 들고 있는 호출부 모듈 (루프가 실제로 타는 것들 + 나머지 전부).
# 하나라도 빠뜨리면 그 콜이 관측에서 조용히 사라진다 — 무효 판정이 무력해지므로 전수로 적는다.
_CALLERS = [
    "orthus.router.route",
    "orthus.router.decompose",
    "orthus.router.graph",
    "orthus.structured.query",
    "orthus.wiki.qa",
    "orthus.wiki.gap",
    "orthus.agentwork.delegation",
    "orthus.agentwork.service",
    "orthus.mail.compose",
    "orthus.wiki.backfill_claim_headline",
]


@dataclass
class CallLog:
    stage: str  # 프로덕션 task 라벨 (structured/routing/wiki_qa/...)
    worker: str  # 실제로 답한 모델 id
    ms: int
    ok: bool
    assigned: bool  # 배정이 물렸는가 (False = 기본 슬롯이 답함 → 무효 신호)
    fell_back: bool  # 1차 워커가 죽어 폴백 사다리를 탔는가


class _RecordingChat:
    """`get_chat_model_for`가 돌려준 모델을 감싸 complete()를 계측한다. 동작 불변."""

    def __init__(self, inner, task: str, spy: "ProdSpy", assigned: bool):
        self._inner = inner
        self._task = task
        self._spy = spy
        self._assigned = assigned
        self.model_id = getattr(inner, "model_id", "?")

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        t0 = time.monotonic()
        ok = True
        try:
            return self._inner.complete(system, user, json_only=json_only)
        except Exception:
            ok = False
            raise
        finally:
            # FallbackChat은 폴백해도 model_id가 1차 워커 id 그대로다. 폴백 여부는 audit
            # span으로만 남으므로, 여기서는 감사 로그를 세는 대신 예외 없이 완주했는지만 본다.
            # (폴백률은 e2_eval이 audit_spans의 `model.fallback`을 집계한다 — 단일 SoR.)
            self._spy.calls.append(
                CallLog(
                    stage=self._task,
                    worker=self.model_id,
                    ms=int((time.monotonic() - t0) * 1000),
                    ok=ok,
                    assigned=self._assigned,
                    fell_back=False,
                )
            )
            if not self._assigned:
                self._spy.unmatched.append(self._task)


class ProdSpy:
    """`chat_model=None`으로 프로덕션을 태울 때 붙이는 관측자.

    하네스의 다른 arm(SingleChat/PromptKeyedSelectorChat)과 같은 인터페이스를 노출한다
    (`reset()` / `calls` / `unmatched` / `stage_counts()`)지만, **chat_model로 넘겨선 안 된다** —
    넘기면 호출부의 `chat_model or get_chat_model_for(...)`가 배정 계층을 건너뛴다.
    """

    is_production = True  # 하네스가 "이 arm은 chat_model=None으로 부른다"를 아는 표식

    def __init__(self, trace_path: Path | None = None):
        self.calls: list[CallLog] = []
        self.unmatched: list[str] = []
        self._trace = trace_path
        self._orig: dict[str, object] = {}
        self._installed = False

    # -- 하네스 호환 --------------------------------------------------------
    def reset(self) -> None:
        if self._trace and self.calls:
            with open(self._trace, "a", encoding="utf-8") as f:
                for c in self.calls:
                    f.write(json.dumps(c.__dict__, ensure_ascii=False) + "\n")
        self.calls = []
        self.unmatched = []

    def stage_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.calls:
            out[c.stage] = out.get(c.stage, 0) + 1
        return out

    # -- 설치/해제 ----------------------------------------------------------
    def install(self) -> None:
        if self._installed:
            return
        import importlib

        spy = self

        def wrapper(task: str):
            model = orch.get_chat_model_for(task)
            assigned = isinstance(model, orch.FallbackChat)
            return _RecordingChat(model, task, spy, assigned)

        for mod_name in _CALLERS:
            mod = importlib.import_module(mod_name)
            if hasattr(mod, "get_chat_model_for"):
                self._orig[mod_name] = mod.get_chat_model_for
                mod.get_chat_model_for = wrapper  # type: ignore[attr-defined]
        self._installed = True

    def uninstall(self) -> None:
        import importlib

        for mod_name, fn in self._orig.items():
            setattr(importlib.import_module(mod_name), "get_chat_model_for", fn)
        self._orig = {}
        self._installed = False


def preflight() -> list[str]:
    """측정 시작 전 게이트 — 배정이 실제로 물릴 수 있는 상태인가.

    반환값이 비어 있지 않으면 **측정을 시작해선 안 된다**. 여기서 안 막으면 "배정을 켰다고
    믿었는데 전부 gpt-4o-mini가 답한" 측정이 그럴듯한 숫자를 내고 끝난다.
    """
    from orthus.settings import get_settings

    s = get_settings()
    problems: list[str] = []
    if s.llm == "mock":
        problems.append("ORTHUS_LLM=mock — 배정 계층이 기본 슬롯으로 폴백한다")
    if not s.model_orchestration_enabled:
        problems.append("ORTHUS_MODEL_ORCHESTRATION_ENABLED=false — 배정이 물리지 않는다")

    # 루프가 타는 작업의 배정 워커가 전부 자격증명을 갖췄는가
    loop_tasks = [
        orch.TASK_ROUTING, orch.TASK_INTENT, orch.TASK_DECOMPOSE,
        orch.TASK_SYNTHESIZE, orch.TASK_STRUCTURED, orch.TASK_WIKI_QA, orch.TASK_GRAPH_BIND,
    ]
    for task in loop_tasks:
        model = orch.get_chat_model_for(task)
        if not isinstance(model, orch.FallbackChat):
            slug = orch.ASSIGNMENTS.get(task, "(미배정)")
            problems.append(f"{task} → {slug}: 배정이 안 물림 (키 미설정?)")
    return problems
