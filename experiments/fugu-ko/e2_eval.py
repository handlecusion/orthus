"""E2 판정 — H-E2a(E2E 성공률) / H-E2b(실패 귀속) / grounding 술어 3종 대조.

신규 LLM 콜 0회. `analysis/raw/e2_e2e_<arm>.jsonl` 재생만 한다.

판정선(계획 §5, 사전 고정):
- H-E2a 지지: (b) 배정 E2E 성공률 ≥ baseline (동일 시나리오 paired)
- 무효 조건: 셀렉터 미매칭 콜 > 0
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
RAW = HERE / "analysis" / "raw"

ARMS = ["baseline", "solar", "assign"]
LABEL = {"baseline": "baseline(gpt-4o-mini)", "solar": "solar 단일", "assign": "(b) 배정"}
STAGES = ["gate_miss", "split_bad", "leaf_wrong", "synthesis_drop"]


def load(arm: str) -> list[dict]:
    f = RAW / f"e2_e2e_{arm}.jsonl"
    if not f.exists():
        return []
    return [json.loads(x) for x in f.read_text("utf-8").splitlines() if x.strip()]


def main() -> None:
    data = {a: load(a) for a in ARMS}
    arms = [a for a in ARMS if data[a]]
    n = len(data[arms[0]])

    # ── 무효 조건 (§5) ──────────────────────────────────────────────────────
    print("== 무효 조건 점검 (§5) ==")
    invalid = False
    for a in arms:
        um = sum(len(r.get("unmatched", [])) for r in data[a])
        err = sum(1 for r in data[a] if r.get("error"))
        if um:
            invalid = True
        print(f"  {a:9} 셀렉터 미매칭 콜 {um}  · 실행 에러 {err}")
    print(f"  → {'무효!' if invalid else '유효 (미매칭 0)'}\n")

    # ── H-E2a: E2E 태스크 성공률 ────────────────────────────────────────────
    print(f"== H-E2a: E2E 태스크 성공률 (paired, {n}문항) ==")
    print(f"  {'arm':22} {'전체':>10} {'프리필터 도달':>14} {'프리필터 사각':>14}")
    print("  " + "-" * 64)
    succ = {}
    for a in arms:
        rows = data[a]
        reach = [r for r in rows if not r["prefilter_blind"]]
        blind = [r for r in rows if r["prefilter_blind"]]
        ok = sum(1 for r in rows if r["success"])
        okr = sum(1 for r in reach if r["success"])
        okb = sum(1 for r in blind if r["success"])
        succ[a] = ok
        print(f"  {LABEL[a]:22} {ok:2}/{len(rows):<2} {ok/len(rows)*100:4.0f}% "
              f"{okr:3}/{len(reach):<2} {okr/len(reach)*100:5.0f}% "
              f"{okb:5}/{len(blind):<2} {okb/max(1,len(blind))*100:5.0f}%")

    base, asg = succ.get("baseline"), succ.get("assign")
    if base is not None and asg is not None:
        verdict = "지지" if asg >= base else "기각"
        print(f"\n  [H-E2a] (b)={asg}/{n} vs baseline={base}/{n} → **{verdict}** "
              f"(기준: (b) ≥ baseline)")

    # 문항 유형별
    print(f"\n  [유형별 성공률]")
    types = sorted({r["type"] for r in data[arms[0]]})
    print(f"    {'arm':22} " + " ".join(f"{t:>8}" for t in types))
    for a in arms:
        cells = []
        for t in types:
            rows = [r for r in data[a] if r["type"] == t]
            ok = sum(1 for r in rows if r["success"])
            cells.append(f"{ok:2}/{len(rows):<2}   ")
        print(f"    {LABEL[a]:22} " + " ".join(cells))

    # ── H-E2b: 실패 귀속 ────────────────────────────────────────────────────
    print(f"\n== H-E2b: 단계별 실패 귀속 (실패 앵커 단위) ==")
    print(f"  {'arm':22} " + " ".join(f"{s:>14}" for s in STAGES) + f" {'총실패앵커':>10}")
    print("  " + "-" * 90)
    attribs = {}
    for a in arms:
        c = Counter()
        for r in data[a]:
            for an in r.get("anchors", []):
                if an["attrib"]:
                    c[an["attrib"]] += 1
        attribs[a] = c
        tot = sum(c.values())
        cells = " ".join(f"{c[s]:6} ({c[s]/max(1,tot)*100:3.0f}%)" for s in STAGES)
        print(f"  {LABEL[a]:22} {cells} {tot:>10}")

    # 오케 결정(gate_miss+split_bad+synthesis_drop) vs 잎 오답(leaf_wrong)
    print(f"\n  [H-E2b 판정: 실패의 주원인이 오케 결정인가, 잎 오답인가]")
    for a in arms:
        c = attribs[a]
        orch = c["gate_miss"] + c["split_bad"] + c["synthesis_drop"]
        leaf = c["leaf_wrong"]
        tot = orch + leaf
        if not tot:
            continue
        print(f"    {LABEL[a]:22} 오케 결정 {orch:2} ({orch/tot*100:3.0f}%) "
              f"vs 잎 오답 {leaf:2} ({leaf/tot*100:3.0f}%)")

    # ── grounding 술어 3종 대조 ─────────────────────────────────────────────
    print(f"\n== grounding 술어 대조 (프로덕션 무수정, 같은 실행 기록에 사후 적용) ==")
    print(f"  {'arm':22} {'리프':>6} {'prod':>12} {'gap_aware':>12} {'extended':>12}")
    print("  " + "-" * 68)
    for a in arms:
        tot = 0
        cnt = {"prod": 0, "gap_aware": 0, "extended": 0}
        for r in data[a]:
            for lf in r.get("leaves", []):
                if lf.get("mode") is None:
                    continue
                tot += 1
                for k in cnt:
                    if lf["grounded_pred"].get(k):
                        cnt[k] += 1
        if not tot:
            continue
        print(f"  {LABEL[a]:22} {tot:6} " + " ".join(
            f"{cnt[k]:5} ({cnt[k]/tot*100:3.0f}%)" for k in ("prod", "gap_aware", "extended")))
    print("\n  prod = 현행(센티널 '근거 없음'만) · gap_aware = orthus가 이미 가진 detect_gap 사용")
    print("  extended = gap + 실측에서 새던 말투 보강 (e2_grounding.py)")

    # 거짓 grounded가 실제로 synthesize 입력에 들어간 규모
    print(f"\n  [거짓 grounded — prod는 통과시키지만 extended는 거절로 보는 리프]")
    for a in arms:
        false_g = 0
        for r in data[a]:
            for lf in r.get("leaves", []):
                p = lf.get("grounded_pred", {})
                if p.get("prod") and not p.get("extended"):
                    false_g += 1
        print(f"    {LABEL[a]:22} {false_g} 리프가 거절/공허인데 grounded=True로 합성 입력에 진입")

    # ── 비용/지연 ───────────────────────────────────────────────────────────
    print(f"\n== 비용/지연 ==")
    print(f"  {'arm':22} {'평균콜':>7} {'총콜':>6} {'p50':>8} {'p95':>8}")
    for a in arms:
        rows = data[a]
        calls = [r.get("n_calls", 0) for r in rows]
        lat = sorted(r["ms"] for r in rows)
        p50 = lat[len(lat) // 2]
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        print(f"  {LABEL[a]:22} {sum(calls)/len(rows):7.1f} {sum(calls):6} "
              f"{p50/1000:7.1f}s {p95/1000:7.1f}s")

    # ── (b) 배정의 스테이지별 워커 분포 ─────────────────────────────────────
    if "assign" in arms:
        print(f"\n== (b) 배정 실행 회계 (스테이지 → 워커) ==")
        c = Counter()
        for r in data["assign"]:
            for call in r.get("calls", []):
                c[(call["stage"], call["worker"])] += 1
        for (stage, worker), k in sorted(c.items(), key=lambda x: -x[1]):
            print(f"    {stage:16} → {worker:8} {k:3}콜")


if __name__ == "__main__":
    main()
