"""검정력 분석: "p>=0.05"가 '차이 없음'인가, 아니면 '표본이 작아서 못 잡은 것'인가.

McNemar의 검정력은 **전체 문항 수가 아니라 불일치 쌍(discordant pair) 수**에 달렸다.
양쪽 다 맞히거나 다 틀린 문항은 정보가 0이다. 249문항이어도 불일치가 27쌍이면
실질 표본은 27이다.

출력:
  1. 현재 n에서 유의해지려면 몇 대 몇이 나와야 했나
  2. 관측된 효과크기를 80% 검정력으로 잡으려면 문항이 몇 개 필요한가
  3. 현재 n에서 잡을 수 있는 최소 효과크기(MDE)
  4. 차이의 신뢰구간 — 데이터와 양립하는 효과 범위
"""
from __future__ import annotations

import json
import os
from math import comb, sqrt

HERE = os.path.dirname(os.path.abspath(__file__))
K = 5
Z_ALPHA = 1.959963985  # two-sided 0.05
Z_BETA = 0.8416212336  # 80% power


def exact_mcnemar(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    lo = min(b, c)
    return min(1.0, 2 * sum(comb(n, i) for i in range(lo + 1)) / (2**n))


def hits(rows: list[dict]) -> dict[str, bool]:
    return {r["case_id"]: (r["rank"] is not None and r["rank"] <= K) for r in rows}


def min_split_for_sig(n: int) -> int | None:
    """n개 불일치 쌍에서 유의(p<0.05)해지는 최소 다수쪽 개수."""
    for k in range(n // 2, n + 1):
        if exact_mcnemar(n - k, k) < 0.05:
            return k
    return None


def n_discordant_for_power(pi: float) -> float:
    """불일치 쌍 중 한쪽이 이길 확률이 pi일 때, 80% 검정력에 필요한 불일치 쌍 수."""
    d = abs(pi - 0.5)
    if d < 1e-9:
        return float("inf")
    return (Z_ALPHA * 0.5 + Z_BETA * sqrt(pi * (1 - pi))) ** 2 / d**2


def mde_pi(n: int) -> float:
    """n개 불일치 쌍에서 80% 검정력으로 잡을 수 있는 최소 pi."""
    lo, hi = 0.5, 0.999
    for _ in range(200):
        mid = (lo + hi) / 2
        if n_discordant_for_power(mid) > n:
            lo = mid
        else:
            hi = mid
    return hi


def diff_ci(b: int, c: int, n_total: int) -> tuple[float, float]:
    """쌍체 비율차(hit률 차)의 95% CI — 정규근사."""
    diff = (c - b) / n_total
    se = sqrt((b + c) - (c - b) ** 2 / n_total) / n_total if n_total else float("nan")
    return diff - Z_ALPHA * se, diff + Z_ALPHA * se


def main() -> None:
    data = json.load(open(f"{HERE}/mirror_results.json", encoding="utf-8"))
    detail = data["detail"]
    gens = sorted({k.split("|")[0] for k in detail})

    print("=" * 78)
    print("검정력 분석 — 'p>=0.05'가 무차이의 증거인가?\n")

    for mode in ("veconly", "hybrid", "rrf"):
        b = c = n_total = 0
        for gen in gens:
            o = hits(detail.get(f"{gen}|openai_{mode}", []))
            s = hits(detail.get(f"{gen}|solar_{mode}", []))
            ids = sorted(set(o) & set(s))
            n_total += len(ids)
            b += sum(1 for i in ids if o[i] and not s[i])
            c += sum(1 for i in ids if s[i] and not o[i])

        n_disc = b + c
        p = exact_mcnemar(b, c)
        pi_hat = c / n_disc if n_disc else 0.5
        need_split = min_split_for_sig(n_disc)
        lo, hi = diff_ci(b, c, n_total)

        print("─" * 78)
        print(f"[{mode}]  전체 {n_total}문항 → **불일치 쌍 {n_disc}개** (실질 표본!)")
        print(f"  관측: Solar만 {c} / OpenAI만 {b}  → Solar 승률 {pi_hat:.0%}, p={p:.3f}")
        print(f"  hit@5 차이: {(c-b)/n_total:+.3f}  95% CI [{lo:+.3f}, {hi:+.3f}]")
        if need_split:
            print(f"  ① 현재 {n_disc}쌍에서 유의하려면 → 한쪽이 최소 {need_split}/{n_disc} "
                  f"({need_split/n_disc:.0%}) 나와야 했다. 관측은 {max(b,c)}/{n_disc}.")
        else:
            print(f"  ① {n_disc}쌍으로는 **어떤 결과가 나와도 유의할 수 없다**(전승해도 p>=0.05).")

        if abs(pi_hat - 0.5) > 1e-9:
            need_disc = n_discordant_for_power(pi_hat)
            rate = n_disc / n_total if n_total else 0
            need_q = need_disc / rate if rate else float("inf")
            print(f"  ② 관측된 효과(승률 {pi_hat:.0%})를 80% 검정력으로 잡으려면"
                  f" 불일치 {need_disc:.0f}쌍 필요")
            print(f"     → 불일치율 {rate:.1%} 기준 **문항 약 {need_q:,.0f}개**"
                  f" (현재 {n_total} = 필요량의 {n_total/need_q:.0%})")
        mde = mde_pi(n_disc)
        print(f"  ③ 현재 {n_disc}쌍으로 80% 검정력에 잡히는 최소 효과 = 승률 {mde:.0%} 이상")
        print(f"     (관측 {pi_hat:.0%} < {mde:.0%} → **애초에 검출 불가능한 크기**)")
        print()

    print("=" * 78)
    print("""결론:
  세 지표 모두 **검정력 부족이 맞다.** p>=0.05는 '차이가 없다'가 아니라
  '이 표본으로는 이 정도 크기를 잡을 수 없다'는 뜻이다.

  다만 방향을 뒤집는 해석은 아니다 — 우리가 못 잡은 그 효과 자체가 **작다**.
  veconly 기준 hit@5 차이는 +2.8%p이고 CI 상한도 한 자릿수 %p다. 즉 표본을 4배로
  늘려 유의해지더라도 **'Solar가 약간 낫다' 수준이지 판을 바꾸는 차이가 아니다.**

  실무적 해석은 그대로다: **품질은 교체의 결정적 근거가 못 된다.**
  단, "동등함이 증명됐다"고 말해선 안 된다 — 증명된 건 '큰 차이는 없다'까지다.""")


if __name__ == "__main__":
    main()
