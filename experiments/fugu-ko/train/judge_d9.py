"""D9 판정 — "현행 GPT → 국내 solar 교체, 손해 없나?" (d9-prereg §3 비열등성, 1회 실행).

arm-G = gpt-4o-mini 1차 + 공통 글루   (캡처 analysis/raw/d9_baseline_sql.jsonl, k=5)
arm-K = solar       1차 + 공통 글루   (캡처 analysis/raw/d8_worker_sql.jsonl, k=5) = D8의 R+
공통 글루(양 arm 완전 동일): k=5 자기일관성 → snap → repair → probe-0 → 폴백(ax→exaone)
  ※ 1차 워커만 교체. 폴백 워커(ax/exaone)는 양 arm 공통이므로 글루의 일부다.

판정(§3, 결과 보기 전 고정): 차이 d = arm-K − arm-G.
  CI 하한 > 0       → "국내가 유의하게 낫다"(우월)
  CI 하한 > −2.0%p  → "바꿔도 손해 없다"(비열등)
  그 외             → "실질 손해" → §5 문안

실행: python train/judge_d9.py   # 1회
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from judge_d8 import is_null, probe_zero, run_sql, snap_dbname  # noqa: E402 — 동일 글루 재사용
from repair import propose_repairs  # noqa: E402

GOLDEN = ROOT / "golden"
RAW = ROOT / "analysis" / "raw"
MARGIN = 2.0  # %p — d9-prereg §3에서 결과 보기 전 고정


def load_cap(path: Path) -> dict:
    cap: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            cap.setdefault(r["id"], {}).setdefault(r["model"], []).append(r)
    for i in cap:
        for m in cap[i]:
            cap[i][m].sort(key=lambda x: x["sample_i"])
    return cap


def main() -> None:
    items = {it["id"]: it for it in json.loads((GOLDEN / "t3_d8_holdout.json").read_text(encoding="utf-8"))["items"]}
    qids = list(items)
    n = len(qids)
    gold = {i: set(items[i]["gold"]) for i in qids}

    k_cap = load_cap(RAW / "d8_worker_sql.jsonl")       # solar k5 + ax + exaone
    g_cap = load_cap(RAW / "d9_baseline_sql.jsonl")     # gpt-4o-mini k5
    miss = [i for i in qids if len(g_cap.get(i, {}).get("baseline", [])) < 5]
    assert not miss, f"baseline 캡처 누락 {len(miss)}건: {miss[:3]}"

    covers = lambda nums, i: bool(gold[i]) and gold[i] <= nums  # noqa: E731

    def worker_t0(i: str, m: str) -> tuple[str, set[int]]:
        s = k_cap[i].get(m, [{}])[0]
        return s.get("status", "err"), set(s.get("nums", []))

    def arm(i: str, samples: list[dict]) -> set[int]:
        """공통 글루 — 1차 워커의 k=5 캡처만 인자로 받는다(양 arm 동일 코드)."""
        valid = [s for s in samples if not is_null(s.get("nums"), s.get("status", "err"))]
        pool = valid or samples
        if pool:
            top = Counter(tuple(s.get("nums", [])) for s in pool).most_common(1)[0][0]
            nums = set(top)
            sql = next((s.get("sql") for s in pool if tuple(s.get("nums", [])) == top), None)
        else:
            nums, sql = set(), None
        if sql:
            snapped = snap_dbname(sql)
            if snapped != sql:
                st, n2 = run_sql(snapped)
                if st == "executed":
                    sql, nums = snapped, n2
        if sql:
            reps = propose_repairs(items[i]["q"], sql)
            if reps:
                st, n2 = run_sql(reps[-1][1])
                if st == "executed":
                    sql, nums = reps[-1][1], n2
        primary = nums
        if not is_null(nums):
            return nums
        if probe_zero(sql):
            return {0}
        for m in ("ax", "exaone"):
            st, wn = worker_t0(i, m)
            if not is_null(wn, st):
                return wn
        return primary

    # ── C2b 재료: SFT EXAONE 1.2B + snap + 자기일관성 (외부 API 0콜 순수 자체 스택) ──
    from sft_score import parse_sql

    gen: dict = {}
    for line in (HERE / "data" / "sft_d8_gen.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            gen.setdefault(r["id"], {})[r.get("sample_i", 0)] = r["raw"]

    def sft_stack(i: str) -> set[int]:
        """SFT k=5 → snap → 실행결과 null제외 다수결. 폴백(solar/ax) **없음** = 자체 스택."""
        runs = []
        for si in sorted(gen.get(i, {})):
            sql = parse_sql(gen[i][si])
            runs.append(run_sql(snap_dbname(sql)) if sql else ("rejected", set()))
        valid = [r for r in runs if not is_null(r[1], r[0])]
        pool = valid or runs
        if not pool:
            return set()
        top = Counter(tuple(sorted(r[1])) for r in pool).most_common(1)[0][0]
        return set(top)

    # ── 전 arm 산출 ────────────────────────────────────────────────────────────
    K = {i: covers(arm(i, k_cap[i].get("solar", [])), i) for i in qids}        # solar + 글루 (=R+)
    G = {i: covers(arm(i, g_cap[i].get("baseline", [])), i) for i in qids}     # gpt + 글루
    Gb = {i: covers(set(g_cap[i]["baseline"][0].get("nums", [])), i) for i in qids}  # gpt 맨몸 = 현행 배포
    Kb = {i: covers(set(k_cap[i]["solar"][0].get("nums", [])), i) for i in qids}     # solar 맨몸
    S = {i: covers(sft_stack(i), i) for i in qids}                             # SFT 자체 스택

    pct = lambda d: sum(d.values()) / n * 100  # noqa: E731
    rng = random.Random(42)
    boot_idx = [[qids[rng.randrange(n)] for _ in range(n)] for _ in range(10_000)]

    def ci(a: dict, b: dict) -> tuple[float, float]:
        ds = sorted((sum(a[i] for i in s) - sum(b[i] for i in s)) / n * 100 for s in boot_idx)
        return ds[250], ds[9750]

    def mcnemar(a: dict, b: dict) -> tuple[int, int, float]:
        x = sum(1 for i in qids if a[i] and not b[i])
        y = sum(1 for i in qids if not a[i] and b[i])
        m = x + y
        p = min(1.0, 2 * sum(comb(m, k) for k in range(min(x, y) + 1)) / 2**m) if m else 1.0
        return x, y, p

    print(f"\n══ D9 판정 (n={n}, d9-prereg §3+§7 — 1회 실행) ══")
    print("\n[전 arm 정답률]")
    print(f"  현행 배포 = gpt-4o-mini 맨몸(글루 없음)     {pct(Gb):5.1f}%")
    print(f"  solar 맨몸                                  {pct(Kb):5.1f}%")
    print(f"  C2b 자체 스택 = SFT 1.2B + snap + SC        {pct(S):5.1f}%   (외부 API 0콜)")
    print(f"  arm-G = gpt + 글루 전부                     {pct(G):5.1f}%")
    print(f"  C2a arm-K = solar + 글루 전부 (=R+)         {pct(K):5.1f}%")

    # ── C1 (부품, §3 비열등) ─────────────────────────────────────────────────
    b, c, p = mcnemar(K, G)
    lo, hi = ci(K, G)
    print(f"\n── C1 부품: 국내(solar)+글루 vs GPT+글루 [글루 동일] ──")
    print(f"  차이 {pct(K) - pct(G):+.1f}p · McNemar 국내만맞 {b}/현행만맞 {c}, p={p:.3g}")
    print(f"  부트스트랩 95% CI = [{lo:+.2f}, {hi:+.2f}] %p  (마진 δ=−{MARGIN}%p)")
    if lo > 0:
        c1 = "국내가 유의하게 낫다 (우월)"
    elif lo > -MARGIN:
        c1 = f"뒤지지 않는다 (비열등 성립 — CI 하한 {lo:+.2f} > −{MARGIN})"
    else:
        c1 = f"국내가 실질 열등 (CI 하한 {lo:+.2f} ≤ −{MARGIN}) → §5 문안"
    print(f"  → C1 판정: {c1}")

    # ── C2 (배포, §7 우월성) ─────────────────────────────────────────────────
    c2 = {}
    for tag, arm_d, name in (("C2a", K, "solar + R+ 글루"), ("C2b", S, "SFT 1.2B 자체 스택")):
        bb, cc, pp = mcnemar(arm_d, Gb)
        l2, h2 = ci(arm_d, Gb)
        win = pp < 0.05 and bb > cc and l2 > 0
        print(f"\n── {tag} 배포: {name} vs 현행(gpt 맨몸) ──")
        print(f"  {pct(arm_d):5.1f}% vs {pct(Gb):5.1f}%  ({pct(arm_d) - pct(Gb):+.1f}p) · 신규만맞 {bb}/현행만맞 {cc}, p={pp:.3g}")
        print(f"  부트스트랩 95% CI = [{l2:+.2f}, {h2:+.2f}] %p")
        print(f"  → {tag} 판정: {'현행을 유의하게 이긴다 (우월 성립)' if win else '우월 미성립'}")
        c2[tag] = {"name": name, "pct": pct(arm_d), "diff": pct(arm_d) - pct(Gb),
                   "mcnemar": {"new_only": bb, "cur_only": cc, "p": pp}, "ci": [l2, h2], "win": win}

    print("\n══ 보고 문안 (§7.2 인과 단서 — 병기 의무) ══")
    print("  C2 이득의 대부분은 **글루**에서 온다(E15: 스냅 단독 +22.7p). 국내 모델의 우수성이")
    print(f"  아니다 — 같은 글루를 GPT에 얹으면 {pct(G):.1f}%로 오른다. 국내 모델의 기여는 C1('{c1}')까지다.")
    if min(pct(K), pct(G)) > 97:
        print("  ⚠️ 한계: C1 양 arm 모두 천장(>97%) 근접 — 이 셋의 변별력 낮음(§3 단서, 병기 필수)")

    (HERE / "data" / "judge_d9_result.json").write_text(
        json.dumps({"n": n,
                    "arms": {"gpt_bare_current": pct(Gb), "solar_bare": pct(Kb), "sft_stack": pct(S),
                             "gpt_glue": pct(G), "solar_glue_rplus": pct(K)},
                    "C1": {"diff": pct(K) - pct(G), "mcnemar": {"k_only": b, "g_only": c, "p": p},
                           "ci": [lo, hi], "margin": MARGIN, "verdict": c1},
                    "C2": c2}, ensure_ascii=False),
        encoding="utf-8")
    print("\n[저장] train/data/judge_d9_result.json")


if __name__ == "__main__":
    main()
