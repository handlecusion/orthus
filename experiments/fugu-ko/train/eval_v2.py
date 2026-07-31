"""D6 Phase F — 주판정: unseen holdout에서 선택기(c) vs static(b) vs oracle + McNemar.

선택기의 holdout 선택(sel_choice_holdout_<tag>.json)을 **동결된 워커 정오**
(analysis/raw/unseen_worker_correct.json)로 채점한다. 이게 D6의 진짜 판정:
- (a) 항상 solar / (b) static=solar / (c) 학습 선택기(margin) / oracle
- McNemar (b) vs (c): 유의하지 않으면 "이겼다" 안 씀(I3 교훈).

여러 tag(ablation) 동시 비교. 실행: python train/eval_v2.py --tags v2,nofeat,nodep
"""
from __future__ import annotations

import argparse
import json
import sys
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GOLDEN = HERE.parent / "golden"
RAW = HERE.parent / "analysis" / "raw"
WORKERS = ["solar", "ax", "exaone"]
STATIC = "solar"


def mcnemar(a: dict, b: dict):
    ids = set(a) & set(b)
    b10 = sum(1 for i in ids if a[i] and not b[i])
    b01 = sum(1 for i in ids if not a[i] and b[i])
    n = b10 + b01
    if n == 0:
        return b10, b01, 1.0
    k = min(b10, b01)
    return b10, b01, min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", default="v2")
    a = ap.parse_args()
    wc = json.loads((RAW / "unseen_worker_correct.json").read_text(encoding="utf-8"))
    items = json.loads((GOLDEN / "t3_unseen_holdout.json").read_text(encoding="utf-8"))["items"]
    ids = [it["id"] for it in items]

    def worker_ok(m, i):
        return bool(wc[m].get(i, False))

    b_correct = {i: worker_ok(STATIC, i) for i in ids}
    o_rate = sum(1 for i in ids if any(worker_ok(m, i) for m in WORKERS)) / len(ids) * 100
    b_rate = sum(b_correct.values()) / len(ids) * 100
    best_single = max(WORKERS, key=lambda m: sum(worker_ok(m, i) for i in ids))
    print(f"══ unseen holdout 주판정 (n={len(ids)}) ══")
    print(f"  (a/b) static(solar) {b_rate:.1f}%  ·  최강단일({best_single}) "
          f"{sum(worker_ok(best_single,i) for i in ids)/len(ids)*100:.1f}%  ·  oracle {o_rate:.1f}%")

    for tag in [t.strip() for t in a.tags.split(",") if t.strip()]:
        p = DATA / f"sel_choice_holdout_{tag}.json"
        if not p.exists():
            print(f"\n  [{tag}] 파일 없음 — 스킵")
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        choice = d["choice"]
        c_correct = {i: worker_ok(choice.get(i, STATIC), i) for i in ids}
        c_rate = sum(c_correct.values()) / len(ids) * 100
        w, lo, pv = mcnemar(c_correct, b_correct)
        sig = "★유의(이김)" if pv < 0.05 and c_rate > b_rate else ("유의차 없음" if c_rate != b_rate else "동률")
        gain = c_rate - b_rate
        from collections import Counter
        print(f"\n  ── [{tag}] features={d.get('features')} dep={d.get('dep')} tau={d.get('tau')} ──")
        print(f"    (c) 선택기 {c_rate:.1f}%  (b 대비 {gain:+.1f}%p)  · 선택분포 {dict(Counter(choice.values()))}")
        print(f"    McNemar (c)vs(b): c만맞음 {w}, b만맞음 {lo} → p={pv:.4f}  {sig}")
        # 불일치 부분집합
        dis = [i for i in ids if len({worker_ok(m, i) for m in WORKERS}) > 1]
        if dis:
            cd = sum(1 for i in dis if c_correct[i]) / len(dis) * 100
            bd = sum(1 for i in dis if b_correct[i]) / len(dis) * 100
            print(f"    [불일치 {len(dis)}문항] static {bd:.1f}% · 선택기 {cd:.1f}% · oracle 100%")


if __name__ == "__main__":
    main()
