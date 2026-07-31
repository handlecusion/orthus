"""Paired 실행 하네스: golden/<task>.json × 워커풀 → analysis/raw/<task>_<model>.jsonl + 요약.

같은 문항을 전 모델에 실행(paired). 결정론 태스크(T3 게이트통과·T5 라우팅정확)는 여기서
즉시 채점, T2는 실행만(쌍대비교 judge는 D3 judge/). 응답 원문(T2 answer, T3 rows)은
analysis/raw/(gitignore)에만 기록.

실행 (company node env 선행 로드 필요 — get_settings가 orthus_company로 해석돼야 함):
  export ORTHUS_NODE=company ORTHUS_NODE_DB=orthus_company
  set -a; source ~/.orthus/nodes/company/node.env; set +a
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/harness.py t3
  ... t5 / t2 도 동일. --models solar,ax,exaone  --limit N
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                    # pool
sys.path.insert(0, str(HERE.parent.parent))      # orthus repo root

from pool import build_pool  # noqa: E402
from orthus.router.route import classify, classify_intent, _rule_based_route  # noqa: E402
from orthus.router.decompose import (  # noqa: E402
    should_decompose,
    split_question,
    _has_command_verb,
    _has_connective_or_enum,
)
from orthus.agentwork.service import detect_assistant_command_action  # noqa: E402
from orthus.structured.query import query_structured  # noqa: E402
from orthus.wiki.qa import ask  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")  # orthus_company의 회사 스코프 조회 유저
GOLDEN = HERE / "golden"
RAW = HERE / "analysis" / "raw"


def run_t3(chat, item: dict) -> dict:
    t0 = time.monotonic()
    try:
        r = query_structured(UID, item["q"], scope="company", chat_model=chat)
        return {
            "id": item["id"], "status": r.status,
            "gate_passed": bool(r.validation.passed),
            "reject": r.validation.rejected_reason,
            "sql": (r.compiled.sql if r.compiled else None),
            "rows": r.rows, "row_count": r.row_count,
            "ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as e:  # noqa: BLE001
        return {"id": item["id"], "status": "error", "error": f"{type(e).__name__}: {str(e)[:120]}",
                "ms": int((time.monotonic() - t0) * 1000)}


def run_t5(chat, item: dict) -> dict:
    t0 = time.monotonic()
    try:
        route = classify(item["q"], chat_model=chat)
        return {"id": item["id"], "route": route, "expected": item.get("expected"),
                "correct": route == item.get("expected"), "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"id": item["id"], "route": "error", "error": f"{type(e).__name__}: {str(e)[:120]}",
                "ms": int((time.monotonic() - t0) * 1000)}


def run_t2(chat, item: dict) -> dict:
    t0 = time.monotonic()
    try:
        a = ask(UID, item["q"], k=5, scope="company", chat_model=chat, learn=False, record_gaps=False)
        return {"id": item["id"], "answer": a.answer, "n_sources": len(a.sources),
                "sources": [s.page_slug for s in a.sources], "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"id": item["id"], "answer": None, "error": f"{type(e).__name__}: {str(e)[:120]}",
                "ms": int((time.monotonic() - t0) * 1000)}


def run_t6(chat, item: dict) -> dict:
    """오케스트레이터 intent 7-way (classify_intent). label = command_family or route.
    reached_llm=False면 결정론 fast-path(키워드 detector / 룰 route)가 답한 것 → 모델 무관."""
    t0 = time.monotonic()
    q = item["q"]
    reached = detect_assistant_command_action(q) is None and _rule_based_route(q) is None
    try:
        intent = classify_intent(q, chat_model=chat, allow_commands=True)
        label = intent.command_family or intent.route
        return {"id": item["id"], "label": label, "expected": item.get("expected"),
                "correct": label == item.get("expected"), "reached_llm": reached,
                "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"id": item["id"], "label": "error", "expected": item.get("expected"),
                "correct": False, "reached_llm": reached,
                "error": f"{type(e).__name__}: {str(e)[:120]}", "ms": int((time.monotonic() - t0) * 1000)}


def run_t7(chat, item: dict) -> dict:
    """오케스트레이터 decompose (should_decompose 게이트 + split_question).
    게이트 reached_llm=False면 prefilter(명령verb/접속·열거 신호 부재)가 LLM 0회 False."""
    t0 = time.monotonic()
    q = item["q"]
    compact = q.lower().replace(" ", "")
    reached = _has_command_verb(compact) or _has_connective_or_enum(q)
    exp_compound = bool(item.get("compound"))
    try:
        gate = should_decompose(q, chat_model=chat)
        out = {"id": item["id"], "gate": gate, "expected_compound": exp_compound,
               "gate_correct": gate == exp_compound, "reached_llm": reached}
        if exp_compound:
            parts = split_question(q, k=5, chat_model=chat)
            need = max(2, int(item.get("expected_parts", 2)))
            out["n_split"] = len(parts)
            out["split_ok"] = len(parts) >= need
            out["split"] = parts
        out["ms"] = int((time.monotonic() - t0) * 1000)
        return out
    except Exception as e:  # noqa: BLE001
        return {"id": item["id"], "gate": None, "expected_compound": exp_compound,
                "gate_correct": False, "reached_llm": reached,
                "error": f"{type(e).__name__}: {str(e)[:120]}", "ms": int((time.monotonic() - t0) * 1000)}


RUNNERS = {"t3": run_t3, "t5": run_t5, "t2": run_t2, "t6": run_t6, "t7": run_t7}


def run_with_usage(runner, chat, item: dict) -> dict:
    """E5 계측 래퍼: 항목 실행 전 usage reset → 실행 → 합산해 결과에 병기.

    러너 5종을 건드리지 않는 단일 지점이다. `n_llm_calls=0`은 결측이 아니라 **결정론 fast-path**
    (t5 `_rule_based_route` 매치, t6/t7 prefilter)로 LLM에 도달하지 않은 것 = 원가 0 실측.
    `n_usage_missing>0`이면 콜은 했는데 벤더가 usage를 안 준 것 = 진짜 결측(§5/§7).
    """
    if hasattr(chat, "reset_usage"):
        chat.reset_usage()
    r = runner(chat, item)
    if hasattr(chat, "usage_totals"):
        r.update(chat.usage_totals())
    return r


def _usage_suffix(results: list[dict]) -> str:
    """E5: 문항당 평균 in/out 토큰 + LLM 콜 수(0=fast-path) + usage 결측 경고."""
    n = len(results) or 1
    calls = sum(r.get("n_llm_calls", 0) for r in results)
    miss = sum(r.get("n_usage_missing", 0) for r in results)
    in_avg = sum(r.get("in_tok", 0) for r in results) / n
    out_avg = sum(r.get("out_tok", 0) for r in results) / n
    s = f"  tok/문항 {in_avg:.0f}→{out_avg:.0f}  calls {calls / n:.2f}"
    return s + (f"  ⚠usage결측 {miss}" if miss else "")


def summarize(task: str, slug: str, results: list[dict]) -> str:
    n = len(results)
    p = lambda ms: sorted(r["ms"] for r in results)  # noqa: E731
    lat = sorted(r["ms"] for r in results)
    p50 = lat[n // 2] if n else 0
    p95 = lat[min(n - 1, int(n * 0.95))] if n else 0
    tail = _usage_suffix(results)
    if task == "t3":
        gp = sum(1 for r in results if r.get("gate_passed"))
        ex = sum(1 for r in results if r.get("status") == "executed")
        err = sum(1 for r in results if r.get("status") == "error")
        return f"{slug:7} gate {gp}/{n}  executed {ex}/{n}  err {err}  p50 {p50}ms p95 {p95}ms{tail}"
    if task == "t5":
        ok = sum(1 for r in results if r.get("correct"))
        return f"{slug:7} route_acc {ok}/{n}  p50 {p50}ms p95 {p95}ms{tail}"
    if task == "t6":
        ok = sum(1 for r in results if r.get("correct"))
        rl = [r for r in results if r.get("reached_llm")]
        rok = sum(1 for r in rl if r.get("correct"))
        return f"{slug:7} intent {ok}/{n}  llm_reached {rok}/{len(rl)}  p50 {p50}ms p95 {p95}ms{tail}"
    if task == "t7":
        gok = sum(1 for r in results if r.get("gate_correct"))
        rl = [r for r in results if r.get("reached_llm")]
        grok = sum(1 for r in rl if r.get("gate_correct"))
        comp = [r for r in results if r.get("expected_compound")]
        sok = sum(1 for r in comp if r.get("split_ok"))
        return (f"{slug:7} gate {gok}/{n}  gate_llm {grok}/{len(rl)}  "
                f"split_ok {sok}/{len(comp)}  p50 {p50}ms p95 {p95}ms{tail}")
    ans = sum(1 for r in results if r.get("answer"))
    return f"{slug:7} answered {ans}/{n} (judge=D3)  p50 {p50}ms p95 {p95}ms{tail}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", choices=list(RUNNERS))
    ap.add_argument("--models", default="solar,ax,exaone")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    golden = json.load(open(GOLDEN / f"{args.task}.json", encoding="utf-8"))["items"]
    if args.limit:
        golden = golden[: args.limit]
    pool = build_pool(args.models.split(","))
    RAW.mkdir(parents=True, exist_ok=True)
    runner = RUNNERS[args.task]

    print(f"[{args.task}] {len(golden)}문항 × {list(pool)} (paired)\n")
    for slug, chat in pool.items():
        results = [run_with_usage(runner, chat, item) for item in golden]
        out = RAW / f"{args.task}_{slug}.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(summarize(args.task, slug, results))
    print(f"\n원자료: {RAW}/{args.task}_*.jsonl (gitignore)")


if __name__ == "__main__":
    main()
