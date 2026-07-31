"""T7 decompose 홀드아웃 — 프리필터 T3에서 LLM 게이트가 실제로 판단하는 문항만 채점.

기본 티어(0)에서는 후보 24개 중 **5개만** 게이트에 도달했다. 진짜 복합질문 8개가 "와/과/각각/
비교"를 신호로 치지 않아 차단됐다 — `docs/decompose-prefilter-ext.md` 가 실측한 누락형 recall
0/40 과 같은 현상이다. 모델의 판단력을 재려면 질문이 모델에게 **도달은 해야** 하므로
`ORTHUS_DECOMPOSE_PREFILTER_EXT_TIER=3` 에서 측정한다(문서 권고 티어).

채점 두 축:
- **gate**: should_decompose 가 복합/단일을 맞혔나 (재현율 + 정밀도)
- **split**: 복합으로 판정한 경우 조각 수가 기대치 이상인가

정밀도 함정(접속 신호는 있으나 단일 질문) 7문항을 넣었다 — 접속사만 보고 무조건 분해하는
모델은 여기서 걸린다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"
MODELS = ["solar", "ax", "exaone", "baseline"]
TIER = "3"


def items() -> list[dict]:
    return json.loads((HERE / "golden" / "t7_holdout.json").read_text(encoding="utf-8"))["items"]


def run(models: list[str]) -> None:
    os.environ["ORTHUS_DECOMPOSE_PREFILTER_EXT_TIER"] = TIER
    sys.path.insert(0, str(HERE))
    from harness import run_t7  # noqa: E402
    from pool import build_pool  # noqa: E402

    from orthus.router.decompose import prefilter_ext_tier  # noqa: E402

    assert prefilter_ext_tier() == int(TIER), f"티어 미적용: {prefilter_ext_tier()}"
    pool = build_pool(models)
    RAW.mkdir(parents=True, exist_ok=True)
    data = items()
    for slug in models:
        with (RAW / f"t7h_{slug}.jsonl").open("w", encoding="utf-8") as fh:
            for n, it in enumerate(data, 1):
                fh.write(json.dumps(run_t7(pool[slug], it), ensure_ascii=False) + "\n")
                fh.flush()
                print(f"  {slug:9} {n:2}/{len(data)} {it['id']}", flush=True)


def mcnemar(a: dict, b: dict) -> tuple[int, int, float]:
    ids = set(a) & set(b)
    w = sum(1 for i in ids if a[i] and not b[i])
    lo = sum(1 for i in ids if not a[i] and b[i])
    n = w + lo
    if n == 0:
        return w, lo, 1.0
    k = min(w, lo)
    return w, lo, min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--score-only", action="store_true")
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    if not a.score_only:
        run(models)

    data = items()
    n_comp = sum(1 for i in data if i["compound"])
    print(f"\n  ══ decompose 홀드아웃 (T3, {len(data)}문항 = 복합 {n_comp} + 단일 {len(data) - n_comp}) ══")
    gate: dict[str, dict[str, bool]] = {}
    for m in models:
        p = RAW / f"t7h_{m}.jsonl"
        if not p.exists():
            continue
        g, splits = {}, []
        tp = fp = fn = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            g[r["id"]] = bool(r.get("gate_correct"))
            exp, got = r["expected_compound"], r.get("gate")
            if exp and got:
                tp += 1
                splits.append(bool(r.get("split_ok")))
            elif not exp and got:
                fp += 1  # 단일 질문을 분해 (오분해)
            elif exp and not got:
                fn += 1  # 복합을 놓침
        gate[m] = g
        k = sum(g.values())
        sp = f"{sum(splits)}/{len(splits)}" if splits else "-"
        print(
            f"  {m:9} 게이트 {k:2}/{len(g)} ({k / len(g) * 100:5.1f}%)  "
            f"| 놓침(FN) {fn}  오분해(FP) {fp}  | 조각수 OK {sp}"
        )

    dom = [m for m in ("solar", "ax", "exaone") if m in gate]
    if len(dom) > 1:
        best = max(dom, key=lambda m: sum(gate[m].values()))
        print("\n  ── 국내 모델 쌍대 비교 (게이트 정확도, McNemar) ──")
        for m in dom:
            if m == best:
                continue
            w, lo, p = mcnemar(gate[best], gate[m])
            print(
                f"    {best} vs {m:7}: {best}만맞음 {w:2}, {m}만맞음 {lo:2} → p={p:.4f}"
                f"  {'★ 유의미' if p < 0.05 else '차이 불충분'}"
            )


if __name__ == "__main__":
    main()
