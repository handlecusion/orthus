"""mirror_results.json의 차이가 통계적으로 유의한가 (McNemar).

왜: `docs/model-orchestration.md` §11의 교훈 — 눈에 띄던 차이가 재측정에서 전부
유의하지 않았다(McNemar p>0.05). 같은 기준을 임베딩에도 적용한다. 83문항에서 hit@5
몇 %p 차이는 대개 1~4문항이고, 그건 노이즈와 구분되지 않는다.

McNemar는 **쌍체(paired)** 검정이다 — 같은 질문에 대해 두 모델의 정오답을 짝지어,
"한쪽만 맞힌" 불일치 쌍만 본다. 양쪽 다 맞히거나 다 틀린 문항은 정보가 없다.
"""
from __future__ import annotations

import json
import os
from math import comb

HERE = os.path.dirname(os.path.abspath(__file__))
K = 5


def exact_mcnemar(b: int, c: int) -> float:
    """양측 정확 McNemar (이항검정). b, c = 불일치 쌍 수."""
    n = b + c
    if n == 0:
        return 1.0
    lo = min(b, c)
    tail = sum(comb(n, i) for i in range(lo + 1)) / (2**n)
    return min(1.0, 2 * tail)


def hits(rows: list[dict]) -> dict[str, bool]:
    return {r["case_id"]: (r["rank"] is not None and r["rank"] <= K) for r in rows}


def main() -> None:
    data = json.load(open(f"{HERE}/mirror_results.json", encoding="utf-8"))
    detail = data["detail"]
    gens = sorted({k.split("|")[0] for k in detail})

    for mode in ("veconly", "hybrid", "rrf"):
        print("=" * 74)
        print(f"[{mode}]")
        print(f"  {'질문작성':9} {'n':>4} {'OpenAI만':>9} {'Solar만':>8} {'차이(문항)':>11} {'p':>8}  판정")
        pooled_b = pooled_c = 0
        for gen in gens:
            o = hits(detail.get(f"{gen}|openai_{mode}", []))
            s = hits(detail.get(f"{gen}|solar_{mode}", []))
            ids = sorted(set(o) & set(s))
            if not ids:
                continue
            b = sum(1 for i in ids if o[i] and not s[i])   # OpenAI만 맞힘
            c = sum(1 for i in ids if s[i] and not o[i])   # Solar만 맞힘
            pooled_b += b
            pooled_c += c
            p = exact_mcnemar(b, c)
            verdict = "유의" if p < 0.05 else "검출못함"
            print(f"  {gen:9} {len(ids):>4} {b:>9} {c:>8} {c - b:>+11} {p:>8.3f}  {verdict}")
        p = exact_mcnemar(pooled_b, pooled_c)
        verdict = "유의" if p < 0.05 else "검출못함"
        print(f"  {'전체합산':9} {'':>4} {pooled_b:>9} {pooled_c:>8} {pooled_c - pooled_b:>+11} {p:>8.3f}  {verdict}")
        print()

    print("=" * 74)
    print("읽는 법:")
    print("  'OpenAI만'/'Solar만' = 그 모델만 정답을 top-5에 넣은 문항 수(불일치 쌍).")
    print("  McNemar는 이 두 숫자만 본다 — 둘 다 맞힌 문항은 변별력이 없다.")
    print()
    print("  ⚠️ '검출못함'을 '무차이'로 읽지 마라. 이 표본은 검정력이 부족하다 —")
    print("     불일치 쌍이 14~27개뿐이라 **승률 76~84% 수준의 압도적 차이만** 잡힌다.")
    print("     실제 효과 범위는 `power.py`의 신뢰구간을 봐라(대략 ±5~7%p 이내).")
    print("     → 결론은 '동등함이 증명됐다'가 아니라 '큰 차이는 없다'까지다.")


if __name__ == "__main__":
    main()
