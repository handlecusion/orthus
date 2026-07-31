"""PromptKeyedSelectorChat — (b) 배정을 오케스트레이터 루프 전체에 물리는 셀렉터.

문제: `answer_or_decompose`는 `chat_model` **1개**를 should_decompose → split → fan_out(leaf:
route/wiki_qa/structured/graph) → synthesize 전 단계에 스레딩한다. 단계별 배정이 구조적으로 불가.

해법: ChatModel Protocol을 구현한 위임 셀렉터. `.complete(system, user, ...)`의 `system`
프리픽스를 orthus의 실제 스테이지 프롬프트 상수와 대조해 스테이지를 식별하고,
`static_map.TASK_MODEL`이 지정한 워커로 위임한다. **프로덕션 diff 0줄.**

§4.1 리스크(프롬프트 상수가 바뀌면 매칭이 조용히 default로 샌다)는 두 겹으로 막는다:
1. `verify_registry()` — 기동 시 orthus의 실제 상수를 import해 각각이 기대 스테이지 정확히
   1개로 매칭되는지 검사. 불일치면 즉시 raise(실행 진입 차단).
2. 런타임 미매칭 카운터 — 매 콜의 (스테이지, 위임워커)를 jsonl로 남기고 `unmatched`를 센다.
   미매칭 > 0이면 결과 무효(사전 고정, 계획 §5).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from static_map import select as select_worker

# ── 스테이지 레지스트리 ──────────────────────────────────────────────────────
# (stage, 프롬프트 프리픽스, static_map 태스크 라벨)
# 프리픽스는 **최장 우선**으로 매칭한다 — "You are a company wiki assistant"가 wiki_qa와
# synthesize에 공유되므로(전자는 ". Answer", 후자는 " synthesizing") 짧은 키로는 갈리지 않는다.
_REGISTRY: list[tuple[str, str, str]] = [
    # stage           prompt prefix                                        task label
    ("synthesize",    "You are a company wiki assistant synthesizing",     "synthesize"),
    ("wiki_qa",       "You are a company wiki assistant. Answer",          "wiki_qa"),
    ("route",         "You are a query router",                            "routing"),
    ("intent",        "You are an intent classifier",                      "intent"),
    ("structured",    "You are a careful SQL compiler",                    "structured"),
    ("graph_bind",    "You extract graph-query intent",                    "graph"),
    ("decompose_gate", "You are a compound-question detector",             "decompose"),
    ("split",         "You are a question-splitter",                       "decompose"),
    ("split_cmd",     "You are a request-splitter",                        "decompose"),
]
# 긴 프리픽스 먼저 — startswith 충돌 방지
_ORDERED = sorted(_REGISTRY, key=lambda r: -len(r[1]))


def match_stage(system: str) -> tuple[str, str] | None:
    """system 프롬프트 → (stage, task label). 미매칭이면 None."""
    for stage, prefix, task in _ORDERED:
        if system.startswith(prefix):
            return stage, task
    return None


def verify_registry() -> None:
    """orthus의 실제 프롬프트 상수가 전부 기대 스테이지로 매칭되는지 검사(기동 게이트).

    프롬프트가 리팩터되면 여기서 즉시 터진다 — 조용한 default 누출 대신.
    """
    from orthus.assistant.compile import _SYSTEM as COMPILE
    from orthus.router.decompose import (
        _DECOMPOSE_SYSTEM,
        _SPLIT_DEPS_SYSTEM_CMD_TMPL,
        _SPLIT_DEPS_SYSTEM_TMPL,
        _SPLIT_SYSTEM_CMD_TMPL,
        _SPLIT_SYSTEM_TMPL,
        _SYNTHESIZE_SYSTEM,
    )
    from orthus.router.graph import _BIND_SYSTEM
    from orthus.router.route import _INTENT_SYSTEM, _SYSTEM as ROUTE
    from orthus.wiki.qa import _SYSTEM as WIKI_QA

    expect = [
        ("decompose_gate", _DECOMPOSE_SYSTEM),
        ("split", _SPLIT_SYSTEM_TMPL.format(k=5)),
        ("split", _SPLIT_DEPS_SYSTEM_TMPL.format(k=5)),
        ("split_cmd", _SPLIT_SYSTEM_CMD_TMPL.format(k=5)),
        ("split_cmd", _SPLIT_DEPS_SYSTEM_CMD_TMPL.format(k=5)),
        ("synthesize", _SYNTHESIZE_SYSTEM),
        ("wiki_qa", WIKI_QA),
        ("route", ROUTE),
        ("intent", _INTENT_SYSTEM),
        ("structured", COMPILE.format(dialect="postgres")),
        ("graph_bind", _BIND_SYSTEM),
    ]
    bad = []
    for want, prompt in expect:
        got = match_stage(prompt)
        if got is None or got[0] != want:
            bad.append(f"{want}: matched={got[0] if got else None!r}")
    if bad:
        raise RuntimeError(
            "프롬프트-키 레지스트리가 orthus 상수와 어긋남(§4.1 무효 조건). "
            "orthus 프롬프트가 바뀌었다면 _REGISTRY를 갱신할 것:\n  " + "\n  ".join(bad)
        )


# ── 셀렉터 ChatModel ─────────────────────────────────────────────────────────


@dataclass
class CallLog:
    stage: str
    worker: str
    ms: int
    ok: bool


class PromptKeyedSelectorChat:
    """ChatModel Protocol 호환. 스테이지별로 다른 워커에 위임한다.

    pool: slug → ChatModel. 미매칭 콜은 default 워커로 보내되 `unmatched`를 센다
    (계획 §5 무효 조건: unmatched > 0).
    """

    def __init__(self, pool: dict, *, default: str = "solar", trace_path: Path | None = None):
        self._pool = pool
        self._default = default
        self.model_id = f"selector(b):{'+'.join(sorted(set(pool)))}"
        self.calls: list[CallLog] = []
        self.unmatched: list[str] = []          # 미매칭 system 프롬프트 프리픽스
        self._trace = trace_path
        self._lock = threading.Lock()           # fan_out이 스레드 병렬이라 필요

    def _dispatch(self, system: str) -> tuple[str, str]:
        m = match_stage(system)
        if m is None:
            with self._lock:
                self.unmatched.append(system[:80])
            return "UNMATCHED", self._default
        stage, task = m
        return stage, select_worker(task)

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        stage, worker_slug = self._dispatch(system)
        chat = self._pool.get(worker_slug) or self._pool[self._default]
        t0 = time.monotonic()
        ok = True
        try:
            return chat.complete(system, user, json_only=json_only)
        except Exception:
            ok = False
            raise
        finally:
            rec = CallLog(stage, worker_slug, int((time.monotonic() - t0) * 1000), ok)
            with self._lock:
                self.calls.append(rec)
                if self._trace:
                    with open(self._trace, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec.__dict__, ensure_ascii=False) + "\n")

    # 실행 회계 — 보고서 §5 무효 조건 판정용
    def stage_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.calls:
            out[c.stage] = out.get(c.stage, 0) + 1
        return out

    def worker_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.calls:
            out[c.worker] = out.get(c.worker, 0) + 1
        return out

    def reset(self) -> None:
        with self._lock:
            self.calls.clear()


class SingleChat:
    """단일 모델 arm — 셀렉터와 동일한 콜 회계를 얻기 위한 얇은 래퍼."""

    def __init__(self, chat, slug: str, trace_path: Path | None = None):
        self._chat = chat
        self.model_id = getattr(chat, "model_id", slug)
        self.slug = slug
        self.calls: list[CallLog] = []
        self.unmatched: list[str] = []
        self._trace = trace_path
        self._lock = threading.Lock()

    def complete(self, system: str, user: str, *, json_only: bool = False) -> str:
        m = match_stage(system)
        stage = m[0] if m else "UNMATCHED"
        if m is None:
            with self._lock:
                self.unmatched.append(system[:80])
        t0 = time.monotonic()
        ok = True
        try:
            return self._chat.complete(system, user, json_only=json_only)
        except Exception:
            ok = False
            raise
        finally:
            rec = CallLog(stage, self.slug, int((time.monotonic() - t0) * 1000), ok)
            with self._lock:
                self.calls.append(rec)
                if self._trace:
                    with open(self._trace, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec.__dict__, ensure_ascii=False) + "\n")

    stage_counts = PromptKeyedSelectorChat.stage_counts
    worker_counts = PromptKeyedSelectorChat.worker_counts
    reset = PromptKeyedSelectorChat.reset


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
    verify_registry()
    print("registry OK — orthus 11개 프롬프트 상수 전부 기대 스테이지로 매칭")
    for stage, prefix, task in _ORDERED:
        print(f"  {stage:15} → {task:11} | {prefix[:44]}…")
