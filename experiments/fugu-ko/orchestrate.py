"""H2 판정: 규칙 오케스트레이션(static_map) vs 최강 단일 모델 vs 현행 baseline.

D2/D3 원자료를 재사용(추가 API 호출 없음). 태스크별 점수를 모아, 각 단일 모델과
오케스트레이션(태스크→최적모델 라우팅)을 §4.4 수용 기준으로 대조한다.

점수축(태스크별):
- T3: 실DB ground-truth 정답률(`t3_gold` 재사용, 18문항)
- T5: 라우팅 정확도(raw, 21문항)
- T2: baseline 대비 쌍대 승률(`t2_judge`, %)  ← 단일 모델은 자기 승률, 오케는 배정모델 승률

수용 기준(§4.4, baseline=gpt-4o-mini 기준):
- T3 ≥ baseline − 5%p
- T5 ≥ baseline (무손실)
- T2 승률 ≥ 40%
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "selectors"))

from static_map import TASK_MODEL  # noqa: E402
from t3_gold import SPECS, gold_numbers, load as load_t3, model_numbers  # noqa: E402

RAW = HERE / "analysis" / "raw"
WORKERS = ["solar", "ax", "exaone"]
ALL = WORKERS + ["baseline"]


def t3_correct() -> dict[str, float]:
    data = {m: load_t3(m) for m in ALL}
    gold = {i: gold_numbers(*s) for i, s in SPECS.items()}
    out = {}
    for m in ALL:
        ok = sum(1 for i, g in gold.items()
                 if data[m].get(i, {}).get("status") == "executed" and g and g <= model_numbers(data[m][i].get("rows")))
        out[m] = ok / len(gold) * 100
    return out


def t5_acc() -> dict[str, tuple[int, int]]:
    out = {}
    for m in ALL:
        rows = [json.loads(x) for x in (RAW / f"t5_{m}.jsonl").read_text("utf-8").splitlines() if x.strip()]
        out[m] = (sum(1 for r in rows if r.get("correct")), len(rows))
    return out


def t2_winrate() -> dict[str, float]:
    verdicts = [json.loads(x) for x in (RAW / "t2_judge.jsonl").read_text("utf-8").splitlines() if x.strip()]
    out = {}
    for m in WORKERS:
        vs = [v for v in verdicts if v["worker"] == m]
        win = sum(1 for v in vs if v["result"] == "win")
        loss = sum(1 for v in vs if v["result"] == "loss")
        out[m] = win / (win + loss) * 100 if (win + loss) else 0.0
    out["baseline"] = None  # 기준선은 자기 자신 비교 대상 없음
    return out


def main() -> None:
    t3 = t3_correct()
    t5 = t5_acc()
    t2 = t2_winrate()

    def t5pct(m):
        c, n = t5[m]
        return c / n * 100

    base_t3, base_t5 = t3["baseline"], t5pct("baseline")
    # 수용 기준
    def passes(t3v, t5v, t2v):
        return (t3v >= base_t3 - 5) and (t5v >= base_t5) and (t2v is not None and t2v >= 40)

    print("== H2: 오케스트레이션 vs 단일 vs 현행 baseline ==")
    print("  (수용: T3≥%.0f%%(base−5), T5≥%.0f%%(base 무손실), T2승률≥40%%)\n" % (base_t3 - 5, base_t5))
    print(f"  {'구성':16} {'T3정답':>7} {'T5라우팅':>9} {'T2승률':>7}  {'수용 전체':>8}")
    print("  " + "-" * 56)

    # 단일 모델들
    for m in ALL:
        t2v = t2[m]
        t2s = f"{t2v:.0f}%" if t2v is not None else "  — "
        ok = "" if m == "baseline" else ("PASS" if passes(t3[m], t5pct(m), t2v) else "FAIL")
        tag = " (현행)" if m == "baseline" else ""
        print(f"  {m+' 단일'+tag:16} {t3[m]:6.0f}% {t5pct(m):8.0f}% {t2s:>7}  {ok:>8}")

    # 오케스트레이션 (태스크→배정모델의 점수 채택)
    ot3 = t3[TASK_MODEL["structured"]]
    ot5 = t5pct(TASK_MODEL["routing"])
    ot2 = t2[TASK_MODEL["wiki_qa"]]
    ook = "PASS" if passes(ot3, ot5, ot2) else "FAIL"
    print("  " + "-" * 56)
    print(f"  {'오케스트레이션':16} {ot3:6.0f}% {ot5:8.0f}% {ot2:6.0f}%  {ook:>8}")
    print(f"    라우팅: structured→{TASK_MODEL['structured']}, routing→{TASK_MODEL['routing']}, wiki_qa→{TASK_MODEL['wiki_qa']}")

    # H2 결론
    single_pass = [m for m in WORKERS if passes(t3[m], t5pct(m), t2[m])]
    print("\n  [H2 판정]")
    print(f"    수용 기준을 모두 통과하는 단일 국내 모델: {single_pass or '없음'}")
    print(f"    오케스트레이션 전체 통과: {'예' if ook == 'PASS' else '아니오'}")
    if not single_pass and ook == "PASS":
        print("    → H2 지지: 어떤 단일 모델도 전 기준 미달, 오케스트레이션만 전 기준 통과.")


if __name__ == "__main__":
    main()
