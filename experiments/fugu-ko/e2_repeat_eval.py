"""E2 루프 재측정 — **3회 반복** 집계 (2026-07-14).

왜 반복하나: 1회 실행 판정(#698)에는 결정적 결함이 있었다. `solar`와 `assign_final`은 루프 작업의
모델이 **동일한데도**(최종 배정에서 전부 Solar) 4문항에서 결과가 갈렸다 — 즉 **단일 실행 노이즈가
±4/38**이고, baseline 대비 순이득(6문항)이 그와 **같은 자릿수**였다. 그 숫자로는 owner에게
"낫다"고 말할 수 없다(자기감사 §9.3, PR #705).

이 스크립트가 답하는 질문은 하나다:
**반복으로 노이즈를 흡수했을 때, 배정의 효과가 그 노이즈를 넘는가?**

## 분석 설계 (결과를 보기 전에 고정)

1. **노이즈 바닥 (정밀화)** — 같은 arm을 3회 돌렸을 때 **문항 단위로 몇 개가 뒤집히나**.
   #698은 "모델이 같은 두 arm"으로 이걸 간접 추정했다(±4). 이제 **직접** 잰다.
2. **arm별 성공률** — 평균 ± 범위(3회). 범위가 arm 간 차이보다 크면 그 차이는 말할 수 없다.
3. **다수결 문항 성공** (3회 중 2회 이상) → 페어드 McNemar. 반복이 문항 수준 노이즈를 흡수한다.
4. **zero-answer 풀링** — 3회 합산(분모 3배)으로 Fisher. 사건 수가 적은 지표라 표본을 키워야 한다.

실행:
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e2_repeat_eval.py \
    --tags final,rep2,rep3
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
RAW = HERE / "analysis" / "raw"

from e2_final_eval import fisher_p, mcnemar_p  # noqa: E402

ARMS = ["baseline", "solar", "assign_final"]
LABEL = {
    "baseline": "baseline (현행 gpt-4o-mini)",
    "solar": "solar 단일",
    "assign_final": "★ 최종배정 (프로덕션)",
}
_ZERO = re.compile(r"\[count\]\s*0\b")


def load(tag: str, arm: str) -> list[dict] | None:
    p = RAW / f"e2_e2e_{tag}_{arm}.jsonl"
    if not p.exists():
        return None
    rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # I5: 실행 오류가 섞인 arm은 무효다. 인프라 실패를 모델 실패로 집계하지 않는다.
    errs = [r["id"] for r in rows if r.get("error")]
    if errs:
        print(f"   ⚠️  {tag}/{arm}: 실행 오류 {len(errs)}건 → **이 반복은 제외**(I5) {errs[:4]}")
        return None
    return rows


def zero_counts(rows: list[dict]) -> tuple[int, int]:
    total = zero = 0
    for r in rows:
        for lf in r.get("leaves", []):
            if lf.get("mode") != "structured":
                continue
            total += 1
            if _ZERO.search(lf.get("body") or ""):
                zero += 1
    return total, zero


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", default="final,rep2,rep3")
    args = ap.parse_args()
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]

    print(f"\n{'=' * 78}\nE2 루프 재측정 — 반복 집계 ({len(tags)}회: {', '.join(tags)})\n{'=' * 78}\n")

    # arm → [rows per run]
    runs: dict[str, list[list[dict]]] = {}
    for arm in ARMS:
        got = [load(t, arm) for t in tags]
        runs[arm] = [r for r in got if r is not None]
    n_ok = min(len(v) for v in runs.values())
    if n_ok < 2:
        raise SystemExit(f"유효 반복이 {n_ok}회뿐 — 최소 2회 필요")
    if n_ok < len(tags):
        print(f"⚠️  유효 반복 {n_ok}/{len(tags)}회로 진행한다\n")
    for arm in ARMS:
        runs[arm] = runs[arm][:n_ok]

    # ── 1. 노이즈 바닥 (정밀화) — 같은 arm을 반복했을 때 문항이 몇 개 뒤집히나 ──
    print("── 1. 노이즈 바닥 — **같은 arm**을 반복하면 문항이 몇 개 뒤집히나\n")
    print("   #698은 '모델이 같은 두 arm'으로 이걸 간접 추정했다(±4/38). 이제 직접 잰다.\n")
    flips: dict[str, int] = {}
    for arm in ARMS:
        per_item: dict[str, list[bool]] = {}
        for rows in runs[arm]:
            for r in rows:
                per_item.setdefault(r["id"], []).append(bool(r.get("success")))
        unstable = [i for i, v in per_item.items() if len(set(v)) > 1]
        flips[arm] = len(unstable)
        n = len(per_item)
        print(f"   {LABEL[arm]:26} 불안정 문항 {len(unstable):2}/{n}  ({len(unstable) / n * 100:.0f}%)")
    floor = max(flips.values())
    print(f"\n   → **노이즈 바닥 = ±{floor}/38** (최댓값 기준, 보수적)\n")

    # ── 2. arm별 성공률: 평균 ± 범위 ──
    print("── 2. arm별 E2E 성공률 (반복 평균 ± 범위)\n")
    print(f"   {'구성':28} {'회차별':<18} {'평균':>7} {'범위':>7}")
    print("   " + "-" * 66)
    rates: dict[str, list[float]] = {}
    for arm in ARMS:
        per_run = [sum(1 for r in rows if r.get("success")) for rows in runs[arm]]
        n = len(runs[arm][0])
        rr = [c / n * 100 for c in per_run]
        rates[arm] = rr
        print(f"   {LABEL[arm]:28} {str(per_run):<18} {mean(rr):6.1f}% {max(rr) - min(rr):6.1f}%p")
    spread = max(max(v) - min(v) for v in rates.values())
    gap = mean(rates["assign_final"]) - mean(rates["baseline"])
    print(f"\n   arm 내 최대 변동폭 {spread:.1f}%p  vs  배정−baseline 격차 {gap:+.1f}%p")
    print(f"   → {'격차가 변동폭보다 크다' if gap > spread else '**격차가 arm 내 변동폭에 묻힌다**'}\n")

    # ── 3. 다수결 문항 성공 → 페어드 McNemar ──
    print("── 3. 다수결(3회 중 2회 이상 성공) 기준 페어드 비교\n")

    def majority(arm: str) -> dict[str, bool]:
        acc: dict[str, list[bool]] = {}
        for rows in runs[arm]:
            for r in rows:
                acc.setdefault(r["id"], []).append(bool(r.get("success")))
        return {i: sum(v) * 2 > len(v) for i, v in acc.items()}

    mb, mf, ms = majority("baseline"), majority("assign_final"), majority("solar")
    common = sorted(set(mb) & set(mf))
    only_b = sum(1 for i in common if mb[i] and not mf[i])
    only_f = sum(1 for i in common if mf[i] and not mb[i])
    p_mc = mcnemar_p(only_b, only_f)
    print(f"   baseline 다수결 성공  {sum(mb.values())}/{len(mb)}")
    print(f"   최종배정 다수결 성공  {sum(mf.values())}/{len(mf)}")
    print(f"   baseline만 {only_b} · 배정만 {only_f} · **McNemar p = {p_mc:.3f}** "
          f"→ {'유의' if p_mc < 0.05 else '**유의하지 않음**'}\n")
    # 같은 모델(solar↔assign_final)의 다수결 불일치 = 계층이 만드는 차이(있다면)
    ds = sum(1 for i in common if ms.get(i) != mf.get(i))
    print(f"   [참고] solar↔최종배정 다수결 불일치 {ds}문항 — 둘은 **모델이 같다**. "
          f"{'0이면 계층은 무해' if ds == 0 else '남으면 계층/어댑터 차이'}\n")

    # ── 4. zero-answer 풀링 ──
    print("── 4. zero-answer (3회 풀링 — 사건 수가 적은 지표라 표본을 키운다)\n")
    pooled: dict[str, tuple[int, int]] = {}
    for arm in ARMS:
        t = z = 0
        for rows in runs[arm]:
            a, b = zero_counts(rows)
            t += a
            z += b
        pooled[arm] = (t, z)
        print(f"   {LABEL[arm]:28} {z:3}/{t:<3} ({z / t * 100:4.1f}%)" if t else "")
    (tb, zb), (tf, zf) = pooled["baseline"], pooled["assign_final"]
    p_z = fisher_p(zf, tf - zf, zb, tb - zb)
    print(f"\n   Fisher exact p = {p_z:.3f} → {'유의' if p_z < 0.05 else '**유의하지 않음**'}\n")

    # ── 판정 ──
    print("=" * 78)
    print("판정\n")
    zero_ok = (zf / tf if tf else 0) <= (zb / tb if tb else 0)
    print(f"  zero-answer 배정 ≤ baseline : {'✅' if zero_ok else '❌'} "
          f"({zf / tf * 100:.1f}% vs {zb / tb * 100:.1f}%, p={p_z:.3f})")
    print(f"  E2E 성공 우위가 노이즈 초과 : {'✅' if gap > spread and p_mc < 0.05 else '❌'} "
          f"(격차 {gap:+.1f}%p vs 변동폭 {spread:.1f}%p, McNemar p={p_mc:.3f})")
    print("\n  → 반복해도 유의하지 않으면, 정직한 결론은 **'배정이 해가 되지 않는다'**이지")
    print("     **'배정이 낫다'가 아니다.** 그 구분을 owner에게 그대로 전달한다.")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    main()
