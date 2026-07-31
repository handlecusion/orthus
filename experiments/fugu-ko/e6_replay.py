"""E6 Phase 0 — SVC 캐스케이드 오프라인 재생 (신규 LLM 콜 0회).

핸드오프: docs/cascade-verification-handoff.md
계획: ~/.claude/plans/squishy-rolling-wren.md

무엇을 재는가:
  기존 t3(18) + t3h(41) structured 원자료(analysis/raw/t3_{m}.jsonl, t3h_{m}.jsonl)를
  써서, **1차 트리거 → 2차 채택** SVC 캐스케이드를 arm별(1차·2차 모델 쌍)로 오프라인
  재생한다. 채점 gold는 실DB(orthus_company) canonical 집계 → number-set 비교.

**prod 규칙 충실 재현 (e1_vote.py와의 차이):**
  e1_vote.report_cascade는 트리거 발동 시 2차를 **무조건** 채택한다. prod
  (orthus/structured/query.py:411-420)는 "2차도 트리거 뜨면 1차 유지"다. 이 스크립트는
  prod 규칙을 그대로 구현한다:
    trigger1 = retry_signal(primary)
    if trigger1 is None:            채택 = primary
    else:
        trigger2 = retry_signal(secondary)
        채택 = secondary if trigger2 is None else primary   # 2차도 신호면 1차 유지

지표(핸드오프 §4):
  - 회수율     = 2차 채택으로 오답→정답이 된 문항 / 1차 트리거 오답 문항
  - 오발동(-)  = 2차 채택으로 정답→오답이 된 문항 (재생 t3/t3h엔 진짜-0 TN 없음 → 라이브 TN에서 측정)
  - 비상관     = 1차 오답셋 vs 2차 오답셋 Jaccard
  - 비용 배율  = (n + 트리거발동수) / n

실행:
  set -a; source ~/.orthus/nodes/company/node.env; set +a   # (또는 FUGU_DSN 지정)
  FUGU_DSN=postgresql://orthus:orthus@localhost:5433/orthus_company \
    PYTHONPATH=$PWD python experiments/fugu-ko/e6_replay.py
"""
from __future__ import annotations

import json
import os
import re
from itertools import combinations
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")

# arm = (라벨, 1차 원자료 접두, 2차 원자료 접두). 원자료 모델명: solar/ax/exaone/baseline(gpt-4o-mini).
ARMS = [
    ("A (baseline gpt-4o-mini → A.X)", "baseline", "ax"),
    ("C (solar → A.X)", "solar", "ax"),
    ("D (solar → EXAONE)", "solar", "exaone"),
    # arm B(같은 모델)는 오프라인 재생에서 항진명제(회수 0)라 라이브 전용 — 여기서 제외.
]

# gold 스펙 두 벌: t3(18) + t3h(41). 둘 다 (db, kind, group_key|None, where|None).
from e1_vote import SPECS as T3_SPECS  # noqa: E402  18문항
from t3_holdout import HOLDOUT_SPECS as T3H_SPECS  # noqa: E402  41문항


def gold_numbers(db: str, kind: str, gkey, where) -> set[int]:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        w = "scope='company' AND db_name=%s" + (f" AND {where}" if where else "")
        if kind == "count":
            cur.execute(f"SELECT count(*) FROM notion_rows WHERE {w}", (db,))
            return {int(cur.fetchone()[0])}
        cur.execute(
            f"SELECT count(*) FROM notion_rows WHERE {w} GROUP BY properties->>%s", (db, gkey)
        )
        return {int(r[0]) for r in cur.fetchall()}


def model_numbers(rows) -> set[int]:
    if not rows:
        return set()
    nums: set[int] = set()
    for row in rows:
        for c in row:
            if isinstance(c, bool):
                continue
            if isinstance(c, int):
                nums.add(c)
            elif isinstance(c, str) and re.fullmatch(r"-?\d+", c.strip()):
                nums.add(int(c))
    return nums


def load_raw(prefix: str, kind: str) -> dict[str, dict]:
    """kind='t3' or 't3h'. prefix=모델명."""
    f = RAW / f"{kind}_{prefix}.jsonl"
    return {r["id"]: r for r in map(json.loads, f.read_text(encoding="utf-8").splitlines()) if r}


def retry_signal(r: dict) -> str | None:
    """orthus/structured/query.py:309 _retry_signal 재현. r은 원자료 dict."""
    if not r or not r.get("gate_passed"):
        return "gate_fail"
    if r.get("status") != "executed":
        return "not_executed"
    rows = r.get("rows")
    if (r.get("row_count") or 0) == 0 or not rows:
        return "empty_rows"
    nums = model_numbers(rows)
    if nums and nums == {0}:
        return "zero_answer"
    return None


def is_correct(r: dict, gold: set[int]) -> bool:
    if not r or r.get("status") != "executed":
        return False
    return bool(gold and gold <= model_numbers(r.get("rows")))


def build_gold() -> dict[str, set[int]]:
    gold: dict[str, set[int]] = {}
    for i, spec in T3_SPECS.items():
        gold[i] = gold_numbers(*spec)
    for i, spec in T3H_SPECS.items():
        gold[i] = gold_numbers(*spec)
    return gold


def cascade(primary: dict, secondary: dict) -> tuple[str, dict]:
    """prod 규칙. 반환 (adoption, 채택된 원자료 dict)."""
    t1 = retry_signal(primary)
    if t1 is None:
        return "keep_primary(no_trigger)", primary
    t2 = retry_signal(secondary)
    if t2 is None:
        return f"adopt_secondary(t1={t1})", secondary
    return f"keep_primary(t1={t1},t2={t2})", primary


def run_arm(label: str, p_pref: str, s_pref: str, gold: dict[str, set[int]]) -> dict:
    print(f"\n{'=' * 78}\n[arm] {label}\n{'=' * 78}")
    ids: list[str] = []
    p_raw: dict[str, dict] = {}
    s_raw: dict[str, dict] = {}
    for kind in ("t3", "t3h"):
        pr = load_raw(p_pref, kind)
        sr = load_raw(s_pref, kind)
        for i in gold:
            if i in pr and i in sr:
                ids.append(i)
                p_raw[i] = pr[i]
                s_raw[i] = sr[i]
    n = len(ids)

    base_ok = sum(1 for i in ids if is_correct(p_raw[i], gold[i]))
    sec_ok = sum(1 for i in ids if is_correct(s_raw[i], gold[i]))

    fired: list[str] = []
    recovered: list[str] = []          # 오답→정답
    false_adopt: list[str] = []        # 정답→오답 (오발동)
    adopted_still_wrong: list[str] = []  # 트리거났고 2차 채택했지만 여전히 오답
    kept_wrong: list[str] = []         # 트리거났으나 2차도 신호 → 1차 유지, 여전히 오답
    final_ok = 0
    for i in ids:
        p, s = p_raw[i], s_raw[i]
        p_ok = is_correct(p, gold[i])
        adoption, chosen = cascade(p, s)
        c_ok = is_correct(chosen, gold[i])
        final_ok += c_ok
        if retry_signal(p) is not None:
            fired.append(i)
            if c_ok and not p_ok:
                recovered.append(i)
            elif not c_ok and p_ok:
                false_adopt.append(i)
            elif not c_ok and adoption.startswith("adopt"):
                adopted_still_wrong.append(i)
            elif not c_ok:
                kept_wrong.append(i)

    # 1차 트리거 오답 (회수율 분모)
    trig_wrong = [i for i in ids if retry_signal(p_raw[i]) is not None and not is_correct(p_raw[i], gold[i])]
    rec_rate = len(recovered) / len(trig_wrong) if trig_wrong else float("nan")

    # 비상관 Jaccard (1차 오답셋 vs 2차 오답셋)
    err_p = {i for i in ids if not is_correct(p_raw[i], gold[i])}
    err_s = {i for i in ids if not is_correct(s_raw[i], gold[i])}
    inter, union = len(err_p & err_s), len(err_p | err_s)
    jac = inter / union if union else 0.0

    calls = (n + len(fired)) / n

    print(f"  채점 문항 n = {n} (t3+t3h 교집합)")
    print(f"  1차 단독 정답  {base_ok}/{n} = {base_ok / n * 100:.1f}%   ({p_pref})")
    print(f"  2차 단독 정답  {sec_ok}/{n} = {sec_ok / n * 100:.1f}%   ({s_pref})")
    print(f"  캐스케이드 최종 {final_ok}/{n} = {final_ok / n * 100:.1f}%")
    print(f"  트리거 발동    {len(fired)}문항  {fired}")
    print(f"  1차 트리거 오답(회수 분모) {len(trig_wrong)}  {trig_wrong}")
    print(f"  ★ 회수(+)  {len(recovered)}/{len(trig_wrong)} = {rec_rate * 100:.1f}%  {recovered}")
    print(f"  ★ 오발동(-) 정답→오답  {len(false_adopt)}  {false_adopt}")
    print(f"  2차 채택했으나 여전히 오답  {len(adopted_still_wrong)}  {adopted_still_wrong}")
    print(f"  2차도 신호→1차 유지, 여전히 오답  {len(kept_wrong)}  {kept_wrong}")
    print(f"  비상관 Jaccard(1차오답 ∩ 2차오답)  {inter}/{union} = {jac:.2f}")
    print(f"  비용 배율  {calls:.3f}×  (트리거율 {len(fired) / n * 100:.1f}%)")
    return {
        "arm": label, "n": n, "base_ok": base_ok, "final_ok": final_ok,
        "recovered": recovered, "false_adopt": false_adopt, "jaccard": jac, "calls": calls,
        "trig_wrong": trig_wrong,
    }


def main() -> None:
    print("SVC 캐스케이드 오프라인 재생 (Phase 0) — 신규 LLM 콜 0")
    print(f"DSN={DSN}")
    gold = build_gold()
    print(f"gold 스펙 {len(gold)}문항 (t3 {len(T3_SPECS)} + t3h {len(T3H_SPECS)})")
    # 대표 drift 게이트
    print(f"  drift 확인: 협업업무표 완료(t3-02) gold={gold.get('t3-02')}  (기대 {{771}})")
    print(f"  이모지 DB count(t3-10) gold={gold.get('t3-10')}  (기대 {{19}})")

    results = [run_arm(*a, gold) for a in ARMS]

    print(f"\n{'=' * 78}\n[요약] arm별 회수·오발동·비상관·비용\n{'=' * 78}")
    print(f"  {'arm':34} {'1차%':>6} {'최종%':>6} {'회수':>5} {'오발동':>6} {'Jac':>5} {'비용':>6}")
    for r in results:
        print(
            f"  {r['arm']:34} {r['base_ok'] / r['n'] * 100:5.1f} {r['final_ok'] / r['n'] * 100:5.1f} "
            f"{len(r['recovered']):>5} {len(r['false_adopt']):>6} {r['jaccard']:>5.2f} {r['calls']:>5.2f}×"
        )
    out = RAW / "e6_replay_summary.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n요약 저장: {out}")


if __name__ == "__main__":
    main()
