"""E2 절제실험 판정 — split이 범인인가? (신규 LLM 콜 0회)

대조군은 기존 e2_e2e 실행분(LLM split):
  baseline / solar / assign      … analysis/raw/e2_e2e_*.jsonl
실험군은 절제분:
  fixed_baseline / fixed_solar / fixed_assign   … split 우회(골든 subq 주입)
  cross                                          … decompose→baseline, structured→solar

판정축은 **수치 앵커**(실DB gold, 정답 유일 · 모델 편향 0). keyword 앵커는 다중정답 취약(§13)이라
참고로만 병기한다.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

RAW = Path(__file__).resolve().parent / "analysis" / "raw"

import sys

# --tag v2 → v2 골든(page 앵커) 기준. 기본값 v2 — v1 keyword 앵커는 고정-split에서 순환한다.
TAG = "v2"
for i, a in enumerate(sys.argv):
    if a == "--tag" and i + 1 < len(sys.argv):
        TAG = sys.argv[i + 1]
_T = f"{TAG}_" if TAG else ""

CONTROL = {
    "baseline": f"e2_e2e_{_T}baseline",
    "solar": f"e2_e2e_{_T}solar",
    "assign": f"e2_e2e_{_T}assign",
}
ABL = {
    "fixed_baseline": f"e2_abl_{_T}fixed_baseline",
    "fixed_solar": f"e2_abl_{_T}fixed_solar",
    "fixed_assign": f"e2_abl_{_T}fixed_assign",
    "cross": f"e2_abl_{_T}cross",
}
LABEL = {
    "baseline": "baseline (LLM split)",
    "solar": "solar (LLM split)",
    "assign": "(b) 배정 (LLM split)",
    "fixed_baseline": "baseline + 고정split",
    "fixed_solar": "solar + 고정split",
    "fixed_assign": "(b) 배정 + 고정split",
    "cross": "교차: split→baseline, SQL→solar",
}


EXPECT_N = 16 if TAG == "v2" else 24  # 골든 문항 수 — 부분 실행 산출물(스모크 잔재)을 배제한다


def load(stem: str) -> list[dict]:
    f = RAW / f"{stem}.jsonl"
    if not f.exists():
        return []
    rows = [json.loads(x) for x in f.read_text("utf-8").splitlines() if x.strip()]
    if len(rows) < EXPECT_N:
        # 스모크(--limit)로 남은 부분 파일을 전체 실행분으로 오인하면 arm 간 비교가 깨진다
        print(f"  ⚠ {stem}: {len(rows)}/{EXPECT_N}문항뿐 → 비교에서 제외")
        return []
    return rows


def num_acc(rows: list[dict], reachable_only: bool) -> tuple[int, int]:
    hit = tot = 0
    for r in rows:
        if reachable_only and r.get("prefilter_blind"):
            continue
        for a in r.get("anchors", []):
            if a["kind"] == "number":
                tot += 1
                hit += bool(a["in_final"])
    return hit, tot


def kw_acc(rows: list[dict], reachable_only: bool) -> tuple[int, int]:
    hit = tot = 0
    for r in rows:
        if reachable_only and r.get("prefilter_blind"):
            continue
        for a in r.get("anchors", []):
            if a["kind"] != "number":
                tot += 1
                hit += bool(a["in_final"])
    return hit, tot


def zeros(rows: list[dict]) -> int:
    n = 0
    for r in rows:
        for lf in r.get("leaves", []):
            if lf.get("mode") == "structured" and re.search(r"\[count\]\s*0\b", lf.get("body") or ""):
                n += 1
    return n


def main() -> None:
    data = {k: load(v) for k, v in {**CONTROL, **ABL}.items()}
    have = [k for k in data if data[k]]

    print("== 절제실험: split이 범인인가 ==")
    print("   판정축 = 수치 앵커(실DB gold, 정답 유일). 프리필터 도달 20문항 기준.\n")
    print(f"  {'구성':32} {'수치앵커':>12} {'keyword':>12} {'structured zero':>16}")
    print("  " + "-" * 76)

    def row(k, mark=""):
        rows = data[k]
        nh, nt = num_acc(rows, True)
        kh, kt = kw_acc(rows, True)
        z = zeros(rows)
        print(f"  {LABEL[k]:32} {nh:3}/{nt:<3}{nh/nt*100:5.0f}% {kh:3}/{kt:<3}{kh/kt*100:5.0f}% "
              f"{z:>10}{mark}")
        return nh / nt * 100

    print("  [대조군 — 현행 LLM split]")
    ctrl = {k: row(k) for k in CONTROL if k in have}
    print("\n  [실험군 A — split 우회(골든 subq 주입). 달라진 변수는 split 하나뿐]")
    fixed = {k: row(k) for k in ("fixed_baseline", "fixed_solar", "fixed_assign") if k in have}
    print("\n  [실험군 B — 교차 배정(split은 baseline, SQL은 solar)]")
    cross = {k: row(k) for k in ("cross",) if k in have}

    # ── 판정 ────────────────────────────────────────────────────────────────
    print("\n== 판정 ==")
    if "solar" in ctrl and "baseline" in ctrl:
        d_llm = ctrl["solar"] - ctrl["baseline"]
        print(f"  LLM split 하에서  solar − baseline = {d_llm:+.1f}%p   (잎 우위 D2: +16%p)")
    if "fixed_solar" in fixed and "fixed_baseline" in fixed:
        d_fix = fixed["fixed_solar"] - fixed["fixed_baseline"]
        print(f"  고정 split 하에서 solar − baseline = {d_fix:+.1f}%p")
        if "solar" in ctrl:
            print()
            if d_fix > d_llm:
                print("  → ★ split을 제거하자 solar의 상대 우위가 회복된다.")
                print("     **범인은 split이다** — 잎 우위는 '깨끗한 입력'에서만 유효하다.")
            elif abs(d_fix - d_llm) < 3:
                print("  → split을 제거해도 격차가 그대로다.")
                print("     **범인은 split이 아니다** — 이 분포에서 잎 우위가 애초에 재현되지 않는다.")
            else:
                print("  → split 제거가 오히려 solar에 불리하다(예상 밖).")

    if "fixed_solar" in fixed and "solar" in ctrl:
        print(f"\n  solar: LLM split {ctrl['solar']:.0f}% → 고정 split {fixed['fixed_solar']:.0f}% "
              f"({fixed['fixed_solar'] - ctrl['solar']:+.0f}%p)  ← split이 solar에게 물린 비용")
    if "fixed_baseline" in fixed and "baseline" in ctrl:
        print(f"  base : LLM split {ctrl['baseline']:.0f}% → 고정 split {fixed['fixed_baseline']:.0f}% "
              f"({fixed['fixed_baseline'] - ctrl['baseline']:+.0f}%p)  ← split이 baseline에게 물린 비용")

    if cross and "assign" in ctrl:
        c = cross["cross"]
        print(f"\n  교차 배정 {c:.0f}% vs (b) 배정 {ctrl['assign']:.0f}% "
              f"({c - ctrl['assign']:+.0f}%p) · vs baseline {ctrl['baseline']:.0f}%")
        if c > ctrl["assign"]:
            print("  → ★ 스테이지별 최강 ≠ 파이프라인 최강. 배정표는 스테이지 상호작용을 봐야 한다.")

    # ── 전체(사각 포함) 참고 ────────────────────────────────────────────────
    print("\n== 참고: 전체 24문항 (프리필터 사각 포함) ==")
    print("   고정 split arm은 게이트를 우회하므로 사각 문항도 분해된다 → 프리필터 비용 계량")
    for k in have:
        rows = data[k]
        ok = sum(1 for r in rows if r["success"])
        blind = [r for r in rows if r.get("prefilter_blind")]
        okb = sum(1 for r in blind if r["success"])
        print(f"  {LABEL[k]:32} 전체 {ok:2}/{len(rows)}   사각 {okb}/{len(blind)}")

    # ── 콜 회계 ─────────────────────────────────────────────────────────────
    print("\n== 콜 회계 ==")
    for k in have:
        rows = data[k]
        calls = sum(r.get("n_calls", 0) for r in rows)
        lat = sorted(r["ms"] for r in rows)
        print(f"  {LABEL[k]:32} {calls:4}콜  p50 {lat[len(lat)//2]/1000:5.1f}s")

    if "cross" in have:
        print("\n== 교차 arm 스테이지→워커 ==")
        c = Counter()
        for r in data["cross"]:
            for call in r.get("calls", []):
                c[(call["stage"], call["worker"])] += 1
        for (s, w), n in sorted(c.items(), key=lambda x: -x[1]):
            print(f"    {s:16} → {w:9} {n:3}콜")


if __name__ == "__main__":
    main()
