"""D2 분석: analysis/raw/*.jsonl → 태스크×모델 매트릭스.

- T3: 게이트통과율 + 실행성공률 + **baseline 대비 결과 일치도**(품질 대리지표) + 지연.
      결과 일치 = 정렬된 rows 동일. baseline(gpt-4o-mini)은 정답이 아니라 현행 기준선이므로
      "일치"는 정확성이 아니라 현행과의 합치. 불일치는 양방향(국내가 맞고 baseline이 틀릴 수도).
- T5: 라우팅 정확도 + 오답 문항.
- 지연: p50/p95.
T2는 judge(D3) 전이라 회수율/지연만.
"""
from __future__ import annotations

import json
from pathlib import Path

RAW = Path(__file__).resolve().parent / "analysis" / "raw"
MODELS = ["solar", "ax", "exaone", "baseline"]


def load(task: str, model: str) -> dict[str, dict]:
    f = RAW / f"{task}_{model}.jsonl"
    if not f.exists():
        return {}
    return {r["id"]: r for r in (json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip())}


def _norm_rows(rows) -> str:
    if rows is None:
        return "∅"
    try:
        return json.dumps(sorted([[str(c) for c in row] for row in rows]), ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return json.dumps(rows, ensure_ascii=False, sort_keys=True)


def pctl(vals: list[int], q: float) -> int:
    if not vals:
        return 0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * q))]


def analyze_t3() -> None:
    data = {m: load("t3", m) for m in MODELS}
    base = data["baseline"]
    ids = sorted(base.keys())
    print("== T3 (NL→SQL) ==")
    print(f"{'model':9} {'gate':>7} {'exec':>7} {'agree_base':>11} {'p50':>6} {'p95':>6}")
    for m in MODELS:
        d = data[m]
        n = len(d)
        gate = sum(1 for r in d.values() if r.get("gate_passed"))
        ex = sum(1 for r in d.values() if r.get("status") == "executed")
        agree = sum(1 for i in ids if i in d and _norm_rows(d[i].get("rows")) == _norm_rows(base[i].get("rows")))
        lat = [r["ms"] for r in d.values()]
        tag = " (기준선)" if m == "baseline" else ""
        print(f"{m:9} {gate}/{n:<5} {ex}/{n:<5} {agree}/{len(ids):<9} {pctl(lat, 0.5):>6} {pctl(lat, 0.95):>6}{tag}")
    # 국내 모델이 baseline과 불일치한 문항(품질 차이 후보) 나열
    print("\n  [국내 vs 기준선 결과 불일치 문항]")
    for i in ids:
        diffs = [m for m in ("solar", "ax", "exaone") if i in data[m] and _norm_rows(data[m][i].get("rows")) != _norm_rows(base[i].get("rows"))]
        if diffs:
            print(f"    {i}: 불일치={diffs}  base_n={base[i].get('row_count')}")


def analyze_t5() -> None:
    data = {m: load("t5", m) for m in MODELS}
    ids = sorted(data["baseline"].keys())
    print("\n== T5 (라우팅) ==")
    for m in MODELS:
        d = data[m]
        ok = sum(1 for r in d.values() if r.get("correct"))
        wrong = [(i, d[i]["route"], d[i]["expected"]) for i in ids if i in d and not d[i].get("correct")]
        tag = " (기준선)" if m == "baseline" else ""
        print(f"  {m:9} {ok}/{len(d)}{tag}   오답: {[(w[0], w[1]+'≠'+str(w[2])) for w in wrong]}")


def analyze_t2() -> None:
    data = {m: load("t2", m) for m in MODELS}
    print("\n== T2 (/ask, judge=D3 전 회수율/지연) ==")
    NO_HIT = "관련 위키 내용을 찾지 못했습니다"
    for m in MODELS:
        d = data[m]
        ans = sum(1 for r in d.values() if r.get("answer"))
        nohit = sum(1 for r in d.values() if r.get("answer") and NO_HIT in r["answer"])
        avglen = round(sum(len(r.get("answer") or "") for r in d.values()) / max(1, len(d)))
        lat = [r["ms"] for r in d.values()]
        tag = " (기준선)" if m == "baseline" else ""
        print(f"  {m:9} 답변 {ans}/{len(d)}  근거없음 {nohit}  평균길이 {avglen}자  p50 {pctl(lat,0.5)}ms{tag}")


if __name__ == "__main__":
    analyze_t3()
    analyze_t5()
    analyze_t2()
