"""D6 Phase B — G0′ headroom 게이트 (학습 전 필수 관문).

D5 G3 실패의 근본원인(F8: 골든 static이 이미 오라클 → 이길 여지 0)을 **학습 전에** 잡는다.
실 anchor(golden/t3_v2_holdout·t5_v2_holdout)에서 3워커+baseline을 돌려 oracle vs static
headroom을 실측한다. headroom < 5%p면 "실분포에선 규칙으로 충분" → owner 에스컬레이션.

라우팅은 `_rule_based_route`가 먼저 걸리면 LLM 무관(모델 변별 불가) — rule-decided를 분리 집계.
structured는 t3_gold number-set 채점.

실행(company node.env + FUGU_KEYS 로드, DSN은 0706으로 override):
  set -a; source ~/.orthus/nodes/company/node.env; set +a
  export ORTHUS_PG_DSN=postgresql://orthus:orthus@localhost:5433/orthus_company_0706
  export ORTHUS_PG_DSN_READONLY=$ORTHUS_PG_DSN FUGU_DSN=$ORTHUS_PG_DSN
  export FUGU_KEYS="/mnt/c/.../keys.json"
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/train/g0_gate_v2.py
"""
from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from harness import run_t3, run_t5  # noqa: E402
from pool import build_pool  # noqa: E402
from t3_gold import gold_numbers, model_numbers  # noqa: E402
from orthus.router.route import _rule_based_route  # noqa: E402

WORKERS = ["solar", "ax", "exaone"]
MODELS = WORKERS + ["baseline"]
GOLDEN = ROOT / "golden"
RAW = ROOT / "analysis" / "raw"
# 현행 프로덕션 배정(model-orchestration §11.4): 전 작업 solar. D5 static은 routing→ax였음 — 둘 다 본다.
STATIC_PROD = {"structured": "solar", "routing": "solar"}
STATIC_D5 = {"structured": "solar", "routing": "ax"}


def load(name: str) -> list[dict]:
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))["items"]


def acc(correct: dict[str, dict[str, bool]], choose) -> float:
    ids = next(iter(correct.values())).keys()
    ok = sum(1 for i in ids if correct[choose(i)][i])
    return ok / len(list(ids)) * 100


def oracle(correct: dict[str, dict[str, bool]]) -> float:
    ids = next(iter(correct.values())).keys()
    ok = sum(1 for i in ids if any(correct[m][i] for m in WORKERS))
    return ok / len(list(ids)) * 100


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    pool = build_pool(MODELS)
    t3, t5 = load("t3_v2_holdout"), load("t5_v2_holdout")

    # ── 실행 ──
    t5_res: dict[str, dict[str, str]] = {m: {} for m in MODELS}
    for m in MODELS:
        for it in t5:
            r = run_t5(pool[m], it)
            t5_res[m][it["id"]] = r.get("route")
        print(f"  [t5 {m}] done")
    t3_res: dict[str, dict[str, dict]] = {m: {} for m in MODELS}
    for m in MODELS:
        for it in t3:
            t3_res[m][it["id"]] = run_t3(pool[m], it)
        print(f"  [t3 {m}] done")

    # raw 저장
    (RAW / "g0v2_t5.json").write_text(json.dumps(t5_res, ensure_ascii=False, indent=1), encoding="utf-8")
    (RAW / "g0v2_t3.json").write_text(json.dumps(
        {m: {i: {k: v for k, v in r.items() if k != "rows"} for i, r in d.items()}
         for m, d in t3_res.items()}, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── 채점: routing ──
    exp5 = {it["id"]: it["expected"] for it in t5}
    rule_decided = {it["id"]: _rule_based_route(it["q"]) is not None for it in t5}
    c5 = {m: {i: (t5_res[m][i] == exp5[i]) for i in exp5} for m in MODELS}
    n_rule = sum(rule_decided.values())
    print(f"\n══ 라우팅 실 anchor ({len(t5)}문항 · rule-decided {n_rule} · LLM-decided {len(t5)-n_rule}) ══")
    for m in MODELS:
        k = sum(c5[m].values())
        print(f"  {m:9} {k:2}/{len(t5)}  {k/len(t5)*100:5.1f}%")
    # LLM-decided 부분집합(모델 변별 가능한 곳)
    llm_ids = [i for i in exp5 if not rule_decided[i]]
    if llm_ids:
        c5_llm = {m: {i: c5[m][i] for i in llm_ids} for m in MODELS}
        print(f"  [LLM-decided {len(llm_ids)}문항만]")
        for m in WORKERS:
            k = sum(c5_llm[m].values())
            print(f"    {m:9} {k}/{len(llm_ids)}  {k/len(llm_ids)*100:5.1f}%")
        o = oracle(c5_llm)
        sp = acc(c5_llm, lambda i: STATIC_PROD["routing"])
        sd = acc(c5_llm, lambda i: STATIC_D5["routing"])
        print(f"    static(prod=solar) {sp:.1f}% · static(D5=ax) {sd:.1f}% · oracle {o:.1f}% "
              f"→ headroom {o-max(sp,sd):.1f}%p")
    else:
        print("  ⚠ LLM-decided 문항 0 — 라우팅 실 anchor는 전부 룰 결정(모델 변별 불가).")

    # ── 채점: structured (gold number-set) ──
    print(f"\n══ structured 실 anchor ({len(t3)}문항, gold number-set) ══")
    gold = {it["id"]: set(it["gold"]) for it in t3}
    c3 = {m: {} for m in MODELS}
    for m in MODELS:
        for it in t3:
            r = t3_res[m][it["id"]]
            g = gold[it["id"]]
            c3[m][it["id"]] = bool(r.get("status") == "executed" and g and g <= model_numbers(r.get("rows")))
        k = sum(c3[m].values())
        print(f"  {m:9} {k}/{len(t3)}  {k/len(t3)*100:5.1f}%")
    if t3:
        o = oracle(c3)
        sp = acc(c3, lambda i: "solar")
        print(f"  static(solar) {sp:.1f}% · oracle {o:.1f}% → headroom {o-sp:.1f}%p")

    # ── 종합 판정 ──
    print("\n══ G0′ 판정 ══")
    print("  실 anchor는 얇다(structured 4·routing LLM-decided 소수). headroom이 낮으면")
    print("  '실분포에선 규칙으로 충분'(F8 재현) → 하이브리드의 unseen/hard-case가 승부처.")


if __name__ == "__main__":
    main()
