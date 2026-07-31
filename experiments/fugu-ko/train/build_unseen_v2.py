"""D6 Phase B — unseen-synthetic structured holdout 빌더 + headroom 프리체크.

주판정 holdout(owner 2026-07-15): **학습에 안 쓴 held-out DB**(gen2.HELDOUT_DBS)에서
count/groupby/filter/ambig 문항을 생성 → gold 실DB 계산 → 3워커로 돌려 **oracle vs static
headroom**을 잰다. 이게 진짜 go/no-go다 — headroom이 없으면 5,000 라벨링·A100이 낭비.

D5 G3 실패(F8: static이 이미 오라클)와의 차이:
① unseen DB(학습 미포함) ② ambig/filter 가중(불일치 원천, D5 F3) → 구조적으로 headroom 확보.

freeze: golden/t3_unseen_holdout.json. 프리체크는 3워커만(baseline 옵션).
실행: (company node.env + FUGU_KEYS, DSN=0706)
  python train/build_unseen_v2.py --run   # 생성+freeze+headroom
  python train/build_unseen_v2.py         # 생성+freeze만
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from gen2 import HELDOUT_DBS, dedupe, extract_schema, gen_items, golden_norms, validate_specs  # noqa: E402
from t3_gold import gold_numbers, model_numbers  # noqa: E402

GOLDEN = ROOT / "golden"
RAW = ROOT / "analysis" / "raw"
WORKERS = ["solar", "ax", "exaone"]


def build() -> list[dict]:
    # held-out DB만 include (min_rows 낮춤 — held-out은 소형 포함)
    schema = extract_schema(min_rows=10, include=HELDOUT_DBS)
    print(f"[unseen] held-out DB {len(schema)}개: "
          + ", ".join(f"{d}{'*' if s['hard'] else ''}" for d, s in schema.items()))
    rows = gen_items(schema, hard_boost=2, ambig_boost=2, seed=7)  # 학습과 다른 seed
    rows = dedupe(rows, golden_norms())
    rows = validate_specs(rows)
    for i, r in enumerate(rows):
        r["id"] = f"t3u-{i:03d}"
        r["gold"] = sorted(gold_numbers(*r["spec"]))
        r["src"] = "unseen-synth"
    out = {"task": "T3_unseen", "desc": "held-out DB structured holdout (주판정). ambig/filter 가중.",
           "heldout_dbs": HELDOUT_DBS, "items": rows}
    (GOLDEN / "t3_unseen_holdout.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    tags: dict[str, int] = {}
    for r in rows:
        tags[r["tag"]] = tags.get(r["tag"], 0) + 1
    print(f"[unseen] {len(rows)}문항 freeze → golden/t3_unseen_holdout.json · 유형 {tags} "
          f"· hard {sum(1 for r in rows if r['hard'])}")
    return rows


def headroom(rows: list[dict]) -> None:
    sys.path.insert(0, str(ROOT))
    from harness import run_t3  # noqa: E402
    from pool import build_pool  # noqa: E402

    pool = build_pool(WORKERS)
    RAW.mkdir(parents=True, exist_ok=True)
    correct: dict[str, dict[str, bool]] = {m: {} for m in WORKERS}
    raw: dict[str, dict] = {m: {} for m in WORKERS}
    for m in WORKERS:
        for it in rows:
            r = run_t3(pool[m], it)
            g = set(it["gold"])
            correct[m][it["id"]] = bool(
                r.get("status") == "executed" and g and g <= model_numbers(r.get("rows")))
            raw[m][it["id"]] = {k: v for k, v in r.items() if k != "rows"}
        k = sum(correct[m].values())
        print(f"  {m:9} {k}/{len(rows)}  {k/len(rows)*100:5.1f}%")
    (RAW / "unseen_headroom.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    # ★ holdout 워커 정오 동결 — eval_v2.py 주판정 ground truth (모든 ablation 동일 기준)
    (RAW / "unseen_worker_correct.json").write_text(
        json.dumps(correct, ensure_ascii=False, indent=1), encoding="utf-8")

    ids = [it["id"] for it in rows]
    static = "solar"
    s = sum(correct[static][i] for i in ids) / len(ids) * 100
    o = sum(1 for i in ids if any(correct[m][i] for m in WORKERS)) / len(ids) * 100
    dis = [i for i in ids if len({correct[m][i] for m in WORKERS}) > 1]
    print(f"\n══ unseen headroom ══")
    print(f"  static(solar) {s:.1f}% · oracle {o:.1f}% → **headroom {o-s:.1f}%p**")
    print(f"  불일치(모델 정오 갈림) {len(dis)}/{len(ids)} = {len(dis)/len(ids)*100:.0f}%")
    # solar 오답인데 타모델 정답 = 선택기가 회수할 대상
    recover = [i for i in ids if not correct[static][i] and any(correct[m][i] for m in WORKERS)]
    print(f"  solar 오답 & 타모델 정답(회수 가능) {len(recover)}문항")
    print(f"  → headroom≥7%p & 불일치≥15% 면 학습 진행 GO. 아니면 owner 에스컬레이션.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="headroom 프리체크까지 실행(3워커 호출)")
    args = ap.parse_args()
    print(f"DSN={os.environ.get('FUGU_DSN', '(default)')}")
    rows = build()
    if args.run:
        headroom(rows)


if __name__ == "__main__":
    main()
