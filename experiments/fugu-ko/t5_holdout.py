"""T5 라우팅 홀드아웃 — 룰을 통과해 LLM이 실제로 결정하는 문항만 채점.

왜 필요한가: `classify()`는 `_rule_based_route()`가 먼저 걸리면 **LLM을 아예 부르지 않는다**.
기존 t5 21문항 중 **11개가 룰로 결정**됐으므로, 보고서의 "라우팅 95.2%"는 대부분 룰 테이블의
성적이고 모델을 변별한 문항은 **10개뿐**이었다. `routing → ax` 배정이 사실상 n=10에 얹혀 있다.

이 홀드아웃은 `_rule_based_route(q) is None` 인 문항만 담는다(골든 생성 시 기계적으로 필터링).
따라서 여기 점수는 **오롯이 모델의 성적**이다.

    python t5_holdout.py                # 모델별 실행 + 채점
    python t5_holdout.py --score-only
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
MODELS = ["solar", "ax", "exaone", "baseline"]  # baseline=gpt-4o-mini는 참고용(사용 불가 제약)


def items() -> list[dict]:
    return json.loads((HERE / "golden" / "t5_holdout.json").read_text(encoding="utf-8"))["items"]


def raw_path(m: str) -> Path:
    return RAW / f"t5h_{m}.jsonl"


def run(models: list[str]) -> None:
    sys.path.insert(0, str(HERE))
    from harness import run_t5  # noqa: E402
    from pool import build_pool  # noqa: E402

    pool = build_pool(models)
    RAW.mkdir(parents=True, exist_ok=True)
    data = items()
    for slug in models:
        chat = pool[slug]
        with raw_path(slug).open("w", encoding="utf-8") as fh:
            for n, it in enumerate(data, 1):
                rec = run_t5(chat, it)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                print(f"  {slug:9} {n:2}/{len(data)} {it['id']}", flush=True)


def score(models: list[str]) -> dict[str, dict[str, bool]]:
    gold = {i["id"]: i["expected"] for i in items()}
    out: dict[str, dict[str, bool]] = {}
    for m in models:
        p = raw_path(m)
        if not p.exists():
            continue
        got: dict[str, bool] = {}
        conf: Counter = Counter()
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            i = r["id"]
            if i not in gold:
                continue
            pred = r.get("route")
            got[i] = pred == gold[i]
            if pred != gold[i]:
                conf[f"{gold[i]}→{pred}"] += 1
        out[m] = got
        k = sum(got.values())
        print(f"  {m:9} {k:2}/{len(got)}  {k / len(got) * 100:5.1f}%   오분류: {dict(conf) or '없음'}")
    return out


def mcnemar(a: dict, b: dict) -> tuple[int, int, float]:
    ids = set(a) & set(b)
    b10 = sum(1 for i in ids if a[i] and not b[i])
    b01 = sum(1 for i in ids if not a[i] and b[i])
    n = b10 + b01
    if n == 0:
        return b10, b01, 1.0
    k = min(b10, b01)
    return b10, b01, min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--score-only", action="store_true")
    a = ap.parse_args()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    if not a.score_only:
        run(models)
    print(f"\n  ══ 라우팅 홀드아웃 ({len(items())}문항, 전부 LLM이 결정) ══")
    got = score(models)
    dom = [m for m in ("solar", "ax", "exaone") if m in got]
    if len(dom) > 1:
        print("\n  ── 국내 모델 쌍대 비교 (McNemar) ──")
        best = max(dom, key=lambda m: sum(got[m].values()))
        for m in dom:
            if m == best:
                continue
            w, lo, p = mcnemar(got[best], got[m])
            sig = "★ 유의미" if p < 0.05 else "차이 불충분"
            print(f"    {best} vs {m:7}: {best}만맞음 {w:2}, {m}만맞음 {lo:2} → p={p:.4f}  {sig}")


if __name__ == "__main__":
    main()
