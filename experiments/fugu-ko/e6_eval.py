"""E6 판정 — 라이브 캐스케이드 원자료(e6_cascade_*.jsonl)에서 지표 산출 (신규 콜 0).

지표(핸드오프 §4):
  회수율     = 트리거 발동 후 최종 정답 / 트리거 발동 (t3셋, gold>0)
  오발동율   = 2차 채택으로 진짜-0을 뒤집어 비-0 오답 / TN 트리거 발동 (TN셋, gold=0)
  비상관     = adopt된 2차 답과 1차 트리거의 관계 대신, arm B 대비 회수 격차로 "비상관 효과" 제시
  비용 배율  = (n + 발동수) / n

실행: PYTHONPATH=$PWD python experiments/fugu-ko/e6_eval.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"


def load(tag: str) -> list[dict]:
    f = RAW / f"e6_cascade_{tag}.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    z = 1.96
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def eval_t3(tag: str, label: str) -> dict:
    rows = load(tag)
    if not rows:
        print(f"  [{label}] 원자료 없음")
        return {}
    n = len(rows)
    fired = [r for r in rows if r["cascade_fired"]]
    # 무결성: 트리거났으나 adopt=False인데 최종 정답이면 = span 오염 (동시 실행). 경고.
    contaminated = [
        r["id"] for r in fired
        if r["adopted"] is False and r["final_correct"] is True
    ]
    final_ok = sum(1 for r in rows if r["final_correct"])
    # 회수: 발동(=1차가 조용히/명시적으로 실패) 문항 중 최종 정답
    recovered = [r["id"] for r in fired if r["final_correct"]]
    rec_lo, rec_hi = wilson(len(recovered), len(fired))
    fberr = [r["id"] for r in fired if r["fallback_error"]]
    calls = (n + len(fired)) / n
    adopt_true = [r for r in fired if r["adopted"]]
    adopt_recovered = [r["id"] for r in adopt_true if r["final_correct"]]
    print(f"\n  [{label}]  n={n}")
    print(f"    최종 정답    {final_ok}/{n} = {final_ok / n * 100:.1f}%")
    print(f"    트리거 발동  {len(fired)}  {[r['id'] for r in fired]}")
    if contaminated:
        print(f"    ⚠️ span 오염 의심(fired+adopt=False+correct): {contaminated} — 재실행 필요")
    print(f"    2차 채택(adopt=True) {len(adopt_true)}  회수 {len(adopt_recovered)}  {adopt_recovered}")
    print(f"    ★ 회수  {len(recovered)}/{len(fired)} = {len(recovered) / len(fired) * 100 if fired else 0:.1f}%  "
          f"[95%CI {rec_lo * 100:.0f}–{rec_hi * 100:.0f}]  {recovered}")
    print(f"    fallback_error {len(fberr)}  {fberr}")
    print(f"    비용 배율  {calls:.3f}×  (트리거율 {len(fired) / n * 100:.1f}%)")
    return {
        "tag": tag, "n": n, "final_ok": final_ok, "fired": [r["id"] for r in fired],
        "recovered": recovered, "adopt_recovered": adopt_recovered,
        "fberr": fberr, "calls": calls, "contaminated": contaminated,
    }


def eval_tn(tag: str, label: str) -> dict:
    rows = load(tag)
    if not rows:
        print(f"  [{label}] 원자료 없음")
        return {}
    n = len(rows)
    fired = [r for r in rows if r["cascade_fired"]]
    adopt_true = [r for r in fired if r["adopted"]]
    # 오발동 = 2차 채택으로 최종이 진짜-0을 뒤집어 비-0(false non-zero)
    false_adopt = [r["id"] for r in adopt_true if r["final_zero"] is False]
    # 안전(정상): 최종이 여전히 0 (진짜-0 보존)
    safe_zero = [r["id"] for r in rows if r["final_zero"]]
    fa_lo, fa_hi = wilson(len(false_adopt), len(fired))
    fberr = [r["id"] for r in fired if r["fallback_error"]]
    print(f"\n  [{label} TN]  n={n}")
    print(f"    트리거 발동   {len(fired)}/{n} (진짜-0이라 대부분 발동 예상)")
    print(f"    2차 채택      {len(adopt_true)}  {[r['id'] for r in adopt_true]}")
    print(f"    최종 진짜-0 보존 {len(safe_zero)}/{n}")
    print(f"    ★ 오발동(진짜-0→비-0 오답)  {len(false_adopt)}/{len(fired)}  {false_adopt}")
    print(f"       (0이 안전. rule-of-three 상한 {fa_hi * 100:.1f}% @95%)")
    print(f"    fallback_error {len(fberr)}  {fberr}")
    return {"tag": tag, "n": n, "fired": len(fired), "adopt": len(adopt_true),
            "false_adopt": false_adopt, "safe_zero": len(safe_zero), "fberr": fberr}


def main() -> None:
    print("=" * 78)
    print("E6 라이브 캐스케이드 판정")
    print("=" * 78)
    print("\n[t3셋 — 회수/비용]")
    t3 = {
        "A": eval_t3("A", "arm A: gpt-4o-mini → A.X (핵심)"),
        "B": eval_t3("B", "arm B: gpt-4o-mini → gpt-4o-mini (대조)"),
        "C": eval_t3("C", "arm C: solar → A.X (prod 배정)"),
        "D": eval_t3("D", "arm D: solar → EXAONE (E1 조합)"),
    }
    print("\n\n[TN셋 — 오발동/안전]")
    tn = {
        "A": eval_tn("A_tn", "arm A"),
        "B": eval_tn("B_tn", "arm B"),
        "C": eval_tn("C_tn", "arm C"),
    }

    print("\n" + "=" * 78)
    print("[요약]")
    print("=" * 78)
    print(f"  {'arm':40} {'최종%':>7} {'발동':>5} {'회수':>7} {'비용':>7}")
    for k, r in t3.items():
        if not r:
            continue
        rec = f"{len(r['recovered'])}/{len(r['fired'])}"
        print(f"  {k+' '+r['tag']:40} {r['final_ok']/r['n']*100:6.1f} {len(r['fired']):>5} {rec:>7} {r['calls']:>6.2f}×")
    print(f"\n  {'arm TN':40} {'발동':>5} {'채택':>5} {'오발동':>7} {'안전0':>6}")
    for k, r in tn.items():
        if not r:
            continue
        print(f"  {k:40} {r['fired']:>5} {r['adopt']:>5} {len(r['false_adopt']):>7} {r['safe_zero']:>6}")

    out = RAW / "e6_eval_summary.json"
    out.write_text(json.dumps({"t3": t3, "tn": tn}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n요약 저장: {out}")


if __name__ == "__main__":
    main()
