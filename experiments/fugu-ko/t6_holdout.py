"""T6 intent 홀드아웃 — 결정론 fast-path를 통과해 LLM이 실제로 판단하는 문항만 채점.

`classify_intent()`는 (1) 키워드 command family, (2) 라우트 fast-path 중 하나라도 걸리면
LLM을 부르지 않는다. 기존 t6 20문항 중 **LLM이 결정한 건 6개뿐**이었다 — `intent → solar`
배정의 실제 근거가 n=6이라는 뜻이다. 이 홀드아웃(20문항)은 전부 LLM이 결정한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"
MODELS = ["solar", "ax", "exaone", "baseline"]


def items() -> list[dict]:
    return json.loads((HERE / "golden" / "t6_holdout.json").read_text(encoding="utf-8"))["items"]


def run(models: list[str]) -> None:
    sys.path.insert(0, str(HERE))
    from harness import run_t6  # noqa: E402
    from pool import build_pool  # noqa: E402

    pool = build_pool(models)
    RAW.mkdir(parents=True, exist_ok=True)
    data = items()
    for slug in models:
        with (RAW / f"t6h_{slug}.jsonl").open("w", encoding="utf-8") as fh:
            for n, it in enumerate(data, 1):
                fh.write(json.dumps(run_t6(pool[slug], it), ensure_ascii=False) + "\n")
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

    gold = {i["id"]: i["expected"] for i in items()}
    print(f"\n  ══ intent 홀드아웃 ({len(gold)}문항, 전부 LLM이 결정) ══")
    got: dict[str, dict[str, bool]] = {}
    for m in models:
        p = RAW / f"t6h_{m}.jsonl"
        if not p.exists():
            continue
        d, conf = {}, Counter()
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            i = r["id"]
            if i not in gold:
                continue
            # harness.run_t6 은 라벨을 'label' 에 기록한다(명령이면 command_family, 아니면 route).
            pred = r.get("label")
            d[i] = pred == gold[i]
            if pred != gold[i]:
                conf[f"{gold[i]}→{pred}"] += 1
        got[m] = d
        k = sum(d.values())
        print(f"  {m:9} {k:2}/{len(d)}  {k / len(d) * 100:5.1f}%")
        for kk, v in conf.most_common(4):
            print(f"      오분류 ×{v}: {kk}")

    dom = [m for m in ("solar", "ax", "exaone") if m in got]
    if len(dom) > 1:
        best = max(dom, key=lambda m: sum(got[m].values()))
        print("\n  ── 국내 모델 쌍대 비교 (McNemar) ──")
        for m in dom:
            if m == best:
                continue
            w, lo, p = mcnemar(got[best], got[m])
            print(
                f"    {best} vs {m:7}: {best}만맞음 {w:2}, {m}만맞음 {lo:2} → p={p:.4f}"
                f"  {'★ 유의미' if p < 0.05 else '차이 불충분'}"
            )


if __name__ == "__main__":
    main()
