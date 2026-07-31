"""E5 집계: 실측 usage(raw jsonl) × 공시 단가(e5-prices.json) → 원가 표.

계획서 `analysis/e5-usage-cost-plan.md` §4.2 구현.

- 작업별(structured=t3 / routing=t5 / wiki_qa=t2) 문항당 평균 in/out 토큰 → 단가 곱 → 문항당 원가.
  fast-path 문항(`n_llm_calls=0`)은 **원가 0으로 평균에 포함**한다(결측이 아니라 실측 0원).
- 질문당 원가 합성식(§4.2): cost/question = c_routing + p·c_structured + (1-p)·c_wiki.
  기준선(baseline)도 **동일 합성식**으로 계산해야 "현행 대비 배율"이 성립한다.
- (b) 배정 = selectors/static_map.TASK_MODEL의 작업별 모델 원가를 같은 합성식에 대입.
- EXAONE(Friendli dedicated)은 토큰 과금이 아니라 GPU-시간 과금 → 실측 지연 기반 별도 산식
  (이용률 100% 가정의 하한 원가). 표에 각주로 명기한다.
- 드리프트(§4.2): t3 gate_passed + t5 correct만 직전 실행(raw_prev)과 비교.

실행:
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e5_cost.py
  옵션: --p 0.483 (질문 믹스; 기본은 골든 구성비) --prev analysis/raw_prev
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "selectors"))  # stdlib `selectors`와 이름 충돌 — 기존 스크립트 관례

from static_map import TASK_MODEL  # noqa: E402  — (b) 배정표

RAW = HERE / "analysis" / "raw"
PRICES = HERE / "analysis" / "e5-prices.json"

MODELS = ["solar", "ax", "exaone", "baseline"]
# 작업 라벨 ↔ 골든 태스크. static_map의 라벨과 같은 이름을 쓴다.
TASKS = {"structured": "t3", "routing": "t5", "wiki_qa": "t2"}


def load_raw(task: str, model: str, root: Path) -> list[dict]:
    f = root / f"{task}_{model}.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def require_instrumented(task: str, model: str, rows: list[dict]) -> None:
    """계측 이전(E5 이전) raw를 원가 0원으로 조용히 집계하는 사고를 막는다.

    fast-path 행은 `n_llm_calls=0`을 **키로 갖고** 있고, 계측 이전 행은 키 자체가 없다.
    `.get(key, 0)`은 둘을 구분하지 못해 구 raw를 넣으면 '0.00원 + 결측 0 PASS'라는
    그럴듯한 오답이 나온다(실제로 그렇게 나왔다 — E5 E2E 검증에서 발견).
    """
    stale = [r for r in rows if "n_llm_calls" not in r]
    if stale:
        raise SystemExit(
            f"계측 이전 raw: {task}_{model}.jsonl — {len(stale)}/{len(rows)}행에 usage 필드 없음.\n"
            f"  E5 계측이 들어간 harness로 재실행해야 한다(구 raw는 원가 0원으로 오집계된다):\n"
            f"  python experiments/fugu-ko/harness.py {task} --models solar,ax,exaone,baseline"
        )


def task_usage(rows: list[dict]) -> dict:
    """문항당 평균 토큰/콜. fast-path(콜 0)는 0원 문항으로 평균에 포함."""
    n = len(rows) or 1
    return {
        "n": len(rows),
        "in_avg": sum(r.get("in_tok", 0) for r in rows) / n,
        "out_avg": sum(r.get("out_tok", 0) for r in rows) / n,
        "calls_avg": sum(r.get("n_llm_calls", 0) for r in rows) / n,
        "fastpath": sum(1 for r in rows if r.get("n_llm_calls", 0) == 0),
        "missing": sum(r.get("n_usage_missing", 0) for r in rows),
        "ms_avg": sum(r.get("ms", 0) for r in rows) / n,
    }


def item_cost_krw(model: str, u: dict, prices: dict) -> float | None:
    """문항당 원가(KRW). 단가 미공시면 None(추정 단가 임의 대입 금지 — §7 정직성 원칙)."""
    p = prices["models"][model]
    fx = prices["fx_usd_krw"]
    if p["billing"] == "token":
        if p.get("in_per_1m") is None or p.get("out_per_1m") is None:
            return None
        mult = fx if p["currency"] == "USD" else 1.0
        return (u["in_avg"] * p["in_per_1m"] + u["out_avg"] * p["out_per_1m"]) / 1e6 * mult
    if p["billing"] == "gpu_hour":
        # 이용률 100% 가정의 **하한** 원가: 콜이 점유한 벽시계 시간만 과금한다고 볼 때.
        # 실트래픽에서는 idle 시간도 과금되므로 실효 원가는 이보다 크다(보고서 각주).
        if p.get("hourly") is None:
            return None
        mult = fx if p["currency"] == "USD" else 1.0
        return p["hourly"] * mult * (u["ms_avg"] / 1000) / 3600
    raise ValueError(f"unknown billing: {p['billing']}")


def fmt(v: float | None, unit: str = "원") -> str:
    return "미공시" if v is None else f"{v:,.2f}{unit}"


def compose(costs: dict[str, float | None], mix: dict[str, str], p: float) -> float | None:
    """질문당 원가 = c_routing + p·c_structured + (1-p)·c_wiki. mix: 작업→모델."""
    parts = [
        costs[(mix["routing"], "routing")],
        costs[(mix["structured"], "structured")],
        costs[(mix["wiki_qa"], "wiki_qa")],
    ]
    if any(c is None for c in parts):
        return None
    c_route, c_struct, c_wiki = parts  # type: ignore[misc]
    return c_route + p * c_struct + (1 - p) * c_wiki


def drift(prev_root: Path, root: Path) -> list[str]:
    """모델 드리프트 신호: t3 gate_passed + t5 correct만(§4.2). t2/rows는 미포함."""
    out = []
    for model in MODELS:
        for task, key in (("t3", "gate_passed"), ("t5", "correct")):
            now = {r["id"]: bool(r.get(key)) for r in load_raw(task, model, root)}
            was = {r["id"]: bool(r.get(key)) for r in load_raw(task, model, prev_root)}
            common = set(now) & set(was)
            diff = [i for i in common if now[i] != was[i]]
            if not common:
                out.append(f"{task}/{model}: 비교 불가(직전 raw 없음)")
            elif diff:
                out.append(
                    f"{task}/{model}: {len(diff)}/{len(common)} 문항 변동 {sorted(diff)[:5]}"
                )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--p", type=float, default=None, help="질문 믹스(structured 비중). 기본=골든 구성비"
    )
    ap.add_argument("--prev", default="analysis/raw_prev", help="드리프트 비교용 직전 raw 디렉터리")
    ap.add_argument("--monthly", type=int, default=1000, help="시나리오 질문 수/월")
    args = ap.parse_args()

    prices = json.loads(PRICES.read_text(encoding="utf-8"))

    usage: dict[tuple[str, str], dict] = {}
    costs: dict[tuple[str, str], float | None] = {}
    for label, task in TASKS.items():
        for model in MODELS:
            rows = load_raw(task, model, RAW)
            if not rows:
                raise SystemExit(f"raw 없음: {task}_{model}.jsonl — 본실행(harness) 선행 필요")
            require_instrumented(task, model, rows)
            u = task_usage(rows)
            usage[(model, label)] = u
            costs[(model, label)] = item_cost_krw(model, u, prices)

    n_struct = usage[("solar", "structured")]["n"]
    n_wiki = usage[("solar", "wiki_qa")]["n"]
    p = args.p if args.p is not None else n_struct / (n_struct + n_wiki)

    print(
        f"# E5 원가 표  (환율 {prices['fx_usd_krw']:,.1f} KRW/USD, 단가 고정일 {prices['as_of']})\n"
    )

    print("## 1. 작업별 실측 usage (문항당 평균)\n")
    print("| 작업 | 모델 | 문항 | in tok | out tok | LLM콜 | fast-path | usage결측 | 지연 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for label in TASKS:
        for model in MODELS:
            u = usage[(model, label)]
            warn = f"**{u['missing']}**" if u["missing"] else "0"
            print(
                f"| {label} | {model} | {u['n']} | {u['in_avg']:.0f} | {u['out_avg']:.0f} | "
                f"{u['calls_avg']:.2f} | {u['fastpath']} | {warn} | {u['ms_avg']:.0f}ms |"
            )

    print("\n## 2. 작업별 문항당 원가 (KRW)\n")
    print("| 작업 | " + " | ".join(MODELS) + " |")
    print("|---" * (len(MODELS) + 1) + "|")
    for label in TASKS:
        print(f"| {label} | " + " | ".join(fmt(costs[(m, label)]) for m in MODELS) + " |")

    print("\n## 3. 질문당 원가 — 합성식 c_routing + p·c_structured + (1-p)·c_wiki\n")
    print(
        f"질문 믹스 p = {p:.3f} (골든 구성비 {n_struct}/{n_struct + n_wiki}); 감도용 50/50 병기.\n"
    )
    configs: dict[str, dict[str, str]] = {f"단일 {m}": dict.fromkeys(TASKS, m) for m in MODELS}
    configs["(b) 배정"] = {k: TASK_MODEL[k] for k in TASKS}

    base = compose(costs, configs["단일 baseline"], p)
    print(
        f"| 구성 | 배정(routing/structured/wiki) | 질문당 | 현행 대비 | {args.monthly:,}질문/월 |"
    )
    print("|---|---|---|---|---|")
    for name, mix in configs.items():
        c = compose(costs, mix, p)
        c50 = compose(costs, mix, 0.5)
        ratio = "—" if c is None or not base else f"{c / base:.2f}×"
        month = "미공시" if c is None else f"{c * args.monthly:,.0f}원"
        cell = "미공시" if c is None else f"{c:.2f}원 (p=.5: {c50:.2f}원)"
        assign = f"{mix['routing']}/{mix['structured']}/{mix['wiki_qa']}"
        print(f"| {name} | {assign} | {cell} | {ratio} | {month} |")

    print(
        "\n> EXAONE은 Friendli **dedicated = GPU-시간 과금**이라 토큰 단가가 아니다. 위 값은 콜이 점유한"
    )
    print(
        "> 벽시계 시간만 과금한다고 본 **이용률 100% 가정의 하한 원가**다 — 실트래픽에서는 idle 시간도"
    )
    print(
        "> 과금되므로 실효 원가는 이보다 크다. 단가 미공시 벤더는 추정치를 넣지 않고 '미공시'로 남긴다."
    )

    print("\n## 4. 재실행 드리프트 (t3 gate + t5 route 한정)\n")
    d = drift(HERE / args.prev, RAW)
    print("\n".join(f"- {x}" for x in d) if d else "- 변동 없음 (직전 실행과 정오 동일)")

    miss = sum(u["missing"] for u in usage.values())
    print(
        f"\n## 5. 완료 정의: usage 결측(LLM 도달 콜 기준) = **{miss}** "
        + ("PASS" if miss == 0 else "FAIL — §7 처리")
    )


if __name__ == "__main__":
    main()
