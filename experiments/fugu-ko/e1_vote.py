"""E1 Phase A — 교차모델 투표 + 결정론 캐스케이드 오프라인 재생 (신규 LLM 콜 0회).

계획: analysis/e1-vote-cascade-plan.md

동기(G3 F8의 역설): DB질의 골든에서 오라클(문항별 최선)=100% > (b) 규칙=94.4% —
모델 오류가 문항 단위로 **비상관**이다. 학습 선택기(c)는 "어느 모델이 맞을지 예측"해야 해서
기각됐지만(G3), 투표/캐스케이드는 예측 없이 **실행 후 결정론 신호**로 합치므로 원리적으로
오라클에 더 가깝다. orthus는 sqlglot 게이트 + 실DB 실행이라는 결정론 검증기를 이미 보유.

입력(전부 기존 원자료 — 신규 콜 0):
- analysis/raw/t3_{solar,ax,exaone,baseline}.jsonl (sql/rows/status/gate_passed)
- 실DB(orthus_company) canonical 집계 → gold number-set (t3_gold.py와 동일 채점)
- train/data/labeled.jsonl + split.json (합성 test 110 정오 라벨)

채점(t3_gold.py와 동일): correct ⟺ status=="executed" AND gold ⊆ model_numbers(rows).
  → 정오는 오직 (status, number-set)의 함수. 따라서 **number-set이 같은 두 모델은 정오도 같다**
    (투표 시뮬레이션의 건전성 근거).

실행: FUGU_DSN=... PYTHONPATH=$PWD python experiments/fugu-ko/e1_vote.py
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
DATA = HERE / "train" / "data"
WORKERS = ["solar", "ax", "exaone"]
ALL_MODELS = [*WORKERS, "baseline"]
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")

# (b) 규칙 배정 (selectors/static_map.py와 동일)
STATIC = {"structured": "solar", "routing": "ax"}
PRIMARY = "solar"  # 캐스케이드 1차 = (b) 배정
SECONDARY = "exaone"  # 캐스케이드 2차 = DB질의 동률 최강(94%)이며 solar와 다른 벤더

# t3_gold.py SPECS와 동일 (정답 명확한 count/groupby 18문항)
SPECS: dict[str, tuple] = {
    "t3-01": ("협업업무표", "groupby", "상태", None),
    "t3-02": ("협업업무표", "count", None, "properties->>'상태'='완료'"),
    "t3-03": ("협업업무표", "groupby", "담당자", None),
    "t3-04": ("협업업무표", "groupby", "우선순위", None),
    "t3-05": ("협업업무표", "count", None, "properties->>'상태'='보류'"),
    "t3-06": ("Nova 개발 로드맵", "groupby", "상태", None),
    "t3-07": ("Nova 개발 로드맵", "groupby", "우선순위", None),
    "t3-09": ("Nova 개발 로드맵", "groupby", "오너", None),
    "t3-10": ("🎯 NOVA 영입 후보 리스트", "count", None, None),
    "t3-11": ("🎯 NOVA 영입 후보 리스트", "groupby", "Tier", None),
    "t3-12": ("🎯 NOVA 영입 후보 리스트", "groupby", "발송 상태", None),
    "t3-13": ("회사 미팅 기록", "count", None, None),
    "t3-14": ("회사 미팅 기록", "groupby", "파트너사", None),
    "t3-15": ("AI관련툴", "count", None, None),
    "t3-16": ("AI관련툴", "groupby", "카테고리", None),
    "t3-17": ("파트너", "groupby", "분야", None),
    "t3-18": ("배포 공지", "groupby", "공지 상태", None),
    "t3-r09": ("배포 공지", "count", None, None),
}


# ── 채점 (t3_gold.py 동일) ────────────────────────────────────────────────────


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


def load_raw(model: str) -> dict[str, dict]:
    f = RAW / f"t3_{model}.jsonl"
    return {r["id"]: r for r in map(json.loads, f.read_text(encoding="utf-8").splitlines()) if r}


# ── 문항 테이블 ───────────────────────────────────────────────────────────────


def build_table() -> list[dict]:
    """문항 × 모델 → {executed, nums(frozenset), correct, gate_passed, row_count, ms}."""
    raw = {m: load_raw(m) for m in ALL_MODELS}
    gold = {i: gold_numbers(*spec) for i, spec in SPECS.items()}
    table: list[dict] = []
    for i, g in gold.items():
        item: dict = {"id": i, "gold": g, "m": {}}
        for m in ALL_MODELS:
            r = raw[m].get(i, {})
            executed = r.get("status") == "executed"
            nums = model_numbers(r.get("rows")) if executed else set()
            item["m"][m] = {
                "executed": executed,
                "status": r.get("status"),
                "gate_passed": bool(r.get("gate_passed")),
                "row_count": r.get("row_count") or 0,
                "nums": frozenset(nums),
                "correct": bool(executed and g and g <= nums),
                "ms": r.get("ms", 0),
            }
        table.append(item)
    return table


# ── (1) 오류 비상관 진단 ──────────────────────────────────────────────────────


def report_correlation(table: list[dict]) -> None:
    n = len(table)
    print(f"\n{'=' * 78}\n[1] 오류 비상관 진단 — 골든 DB질의 {n}문항\n{'=' * 78}")
    print(f"{'model':10} {'correct':>9} {'rate':>7}")
    for m in ALL_MODELS:
        ok = sum(1 for it in table if it["m"][m]["correct"])
        tag = " (현행 기준선)" if m == "baseline" else ""
        print(f"{m:10} {ok:>4}/{n:<4} {ok / n * 100:6.1f}%{tag}")

    oracle = sum(1 for it in table if any(it["m"][m]["correct"] for m in WORKERS))
    static = sum(1 for it in table if it["m"][STATIC["structured"]]["correct"])
    print(f"\n  (b) 규칙(solar)  {static}/{n} = {static / n * 100:.1f}%")
    print(f"  오라클(국내3종)  {oracle}/{n} = {oracle / n * 100:.1f}%  → 헤드룸 {oracle - static}문항")

    # 오답 겹침: 몇 개 모델이 동시에 틀리나
    print("\n  [오답 겹침 분포 — 국내 3종]")
    dist: dict[int, list[str]] = {}
    for it in table:
        k = sum(1 for m in WORKERS if not it["m"][m]["correct"])
        dist.setdefault(k, []).append(it["id"])
    for k in sorted(dist):
        label = {0: "전원 정답", 1: "1개만 오답", 2: "2개 오답", 3: "전원 오답"}[k]
        print(f"    {k}개 오답 ({label:9}): {len(dist[k]):2}문항  {dist[k] if k else ''}")

    # 쌍별 오류 상관 (Jaccard on error sets)
    print("\n  [쌍별 오답 상관 — 오답 집합 Jaccard]")
    err = {m: {it["id"] for it in table if not it["m"][m]["correct"]} for m in WORKERS}
    for a, b in combinations(WORKERS, 2):
        inter, union = len(err[a] & err[b]), len(err[a] | err[b])
        j = inter / union if union else 0.0
        print(f"    {a:7} ∩ {b:7} = {inter} / {union} → J={j:.2f}")


# ── (2) 투표 시뮬레이션 ───────────────────────────────────────────────────────


def vote(item: dict, members: list[str], fallback: str) -> tuple[str, frozenset, bool]:
    """number-set 다수결. 반환 (결정 사유, 채택 nums, correct).

    executed 모델만 투표. 동일 number-set 그룹 최대 크기 ≥2 → 그 그룹 채택.
    과반 없음 → fallback((b) 배정) 답 유지.
    """
    elig = [m for m in members if item["m"][m]["executed"]]
    groups: dict[frozenset, list[str]] = {}
    for m in elig:
        groups.setdefault(item["m"][m]["nums"], []).append(m)
    best = max(groups.items(), key=lambda kv: len(kv[1]), default=(frozenset(), []))
    if len(best[1]) >= 2:
        nums = best[0]
        correct = bool(item["gold"] and item["gold"] <= nums)
        return f"majority({'+'.join(best[1])})", nums, correct
    fb = item["m"][fallback]
    return f"fallback({fallback})", fb["nums"], fb["correct"]


def report_vote(table: list[dict]) -> dict:
    n = len(table)
    print(f"\n{'=' * 78}\n[2] 교차모델 투표 시뮬레이션 (number-set 다수결)\n{'=' * 78}")

    configs = {
        "V1 (2-of-3: solar+ax+exaone)": (WORKERS, PRIMARY),
        "V2 (solar+exaone, 불일치 시 ax tiebreak)": (WORKERS, PRIMARY),  # 동일 규칙 — 아래 주석
    }
    # V2는 호출 절약형: solar/exaone 일치 → 즉시 채택(ax 미호출). 불일치 시에만 ax 호출.
    # 채택 결과는 V1과 동일하나 **평균 호출 수**가 다르다 → 별도 집계.

    base = sum(1 for it in table if it["m"][PRIMARY]["correct"])
    out: dict = {}
    for name, (members, fb) in configs.items():
        ok, changed, gained, lost = 0, [], [], []
        for it in table:
            reason, nums, correct = vote(it, members, fb)
            ok += correct
            if nums != it["m"][PRIMARY]["nums"]:
                changed.append(it["id"])
                if correct and not it["m"][PRIMARY]["correct"]:
                    gained.append(it["id"])
                if not correct and it["m"][PRIMARY]["correct"]:
                    lost.append(it["id"])
        print(f"\n  {name}")
        print(f"    정답률   {ok}/{n} = {ok / n * 100:.1f}%   ((b) 규칙 {base}/{n} = {base / n * 100:.1f}%)")
        print(f"    답 변경  {len(changed)}문항 {changed}")
        print(f"    회수(+)  {len(gained)} {gained}")
        print(f"    손실(-)  {len(lost)} {lost}")
        out[name] = ok
        break  # V2는 채택 결과 동일 — 호출수만 아래에서 집계

    # V2 호출 절약 집계: solar/exaone 일치하면 ax 미호출
    agree_se = sum(
        1
        for it in table
        if it["m"]["solar"]["executed"]
        and it["m"]["exaone"]["executed"]
        and it["m"]["solar"]["nums"] == it["m"]["exaone"]["nums"]
    )
    calls_v1 = 3.0
    calls_v2 = (2 * agree_se + 3 * (n - agree_se)) / n
    print(f"\n  [호출 수] V1 = {calls_v1:.1f}× / 문항")
    print(f"            V2 = {calls_v2:.2f}× / 문항 (solar≡exaone {agree_se}/{n}문항에서 ax 생략)")

    # ★ 오답 수렴 (correlated wrong) — 투표의 최대 리스크
    print("\n  [★ 오답 수렴 검사 — 2개 이상이 '같은 오답'에 합의하는가]")
    conv = []
    for it in table:
        groups: dict[frozenset, list[str]] = {}
        for m in WORKERS:
            if it["m"][m]["executed"]:
                groups.setdefault(it["m"][m]["nums"], []).append(m)
        for nums, ms in groups.items():
            if len(ms) >= 2 and not (it["gold"] and it["gold"] <= nums):
                conv.append((it["id"], "+".join(ms)))
    print(f"    오답 수렴 문항: {len(conv)}/{n}  {conv if conv else '없음'}")
    return out


# ── (3) 캐스케이드 시뮬레이션 ─────────────────────────────────────────────────

TRIGGERS = {
    "gate_fail": lambda r: not r["gate_passed"],
    "not_executed": lambda r: not r["executed"],
    "empty_rows": lambda r: r["executed"] and r["row_count"] == 0,
    "zero_answer": lambda r: r["executed"] and r["nums"] == frozenset({0}),
    # S8형(이모지 누락 → 유효 SQL이 0 반환) 결정론 탐지
}


def report_cascade(table: list[dict]) -> None:
    n = len(table)
    print(f"\n{'=' * 78}\n[3] 결정론 캐스케이드 ({PRIMARY} → 트리거 시 {SECONDARY})\n{'=' * 78}")
    base = sum(1 for it in table if it["m"][PRIMARY]["correct"])
    print(f"  1차 단독 ({PRIMARY}): {base}/{n} = {base / n * 100:.1f}%\n")
    print(f"  {'trigger':14} {'발동':>5} {'회수(+)':>8} {'손실(-)':>8} {'최종':>9} {'평균호출':>9}")

    for name, fn in {**TRIGGERS, "any(위 4종 OR)": None}.items():
        fired, gained, lost, ok = [], [], [], 0
        for it in table:
            p, s = it["m"][PRIMARY], it["m"][SECONDARY]
            hit = (
                any(f(p) for f in TRIGGERS.values()) if fn is None else fn(p)
            )
            if hit:
                fired.append(it["id"])
                ok += s["correct"]
                if s["correct"] and not p["correct"]:
                    gained.append(it["id"])
                if not s["correct"] and p["correct"]:
                    lost.append(it["id"])
            else:
                ok += p["correct"]
        calls = (n + len(fired)) / n
        print(
            f"  {name:14} {len(fired):>5} {len(gained):>8} {len(lost):>8} "
            f"{ok:>4}/{n:<4} {calls:>8.2f}×"
        )
        if fired and fn is None:
            print(f"    발동 문항: {fired}")
            if gained:
                print(f"    회수: {gained}")
            if lost:
                print(f"    손실: {lost}")

    # 놓치는 오답: 트리거가 안 걸리는 1차 오답 ("확신에 찬 오답")
    silent = [
        it["id"]
        for it in table
        if not it["m"][PRIMARY]["correct"] and not any(f(it["m"][PRIMARY]) for f in TRIGGERS.values())
    ]
    print(f"\n  [트리거 미발동 오답 — '확신에 찬 오답'] {len(silent)}문항 {silent}")
    print("    → 캐스케이드가 구조적으로 못 잡는 부분(1차의 실패 모드가 '조용한' 경우).")
    print("      투표가 커버할 수 있으나, 오답 수렴 시 오히려 오답을 확정한다(Phase B 실측: 합성 7/63).")


# ── (4) 합성 test 추정 ────────────────────────────────────────────────────────


def measure_params(table: list[dict]) -> tuple[float, float]:
    """골든에서 투표 시뮬레이션의 두 모수를 실측.

    P_agree_correct = P(두 정답 모델의 number-set이 동일 | 둘 다 정답)
    P_converge_wrong = P(두 오답 모델의 number-set이 동일 | 둘 다 오답, 둘 다 executed)
    """
    ac_hit = ac_tot = cw_hit = cw_tot = 0
    for it in table:
        for a, b in combinations(WORKERS, 2):
            ra, rb = it["m"][a], it["m"][b]
            if not (ra["executed"] and rb["executed"]):
                continue
            same = ra["nums"] == rb["nums"]
            if ra["correct"] and rb["correct"]:
                ac_tot += 1
                ac_hit += same
            elif not ra["correct"] and not rb["correct"]:
                cw_tot += 1
                cw_hit += same
    p_ac = ac_hit / ac_tot if ac_tot else 1.0
    p_cw = cw_hit / cw_tot if cw_tot else 0.0
    print(f"\n{'=' * 78}\n[4] 골든 실측 모수 → 합성 test 추정\n{'=' * 78}")
    print(f"  P_agree_correct  = {ac_hit}/{ac_tot} = {p_ac:.3f}  (정답 2모델의 답 형태 일치율)")
    print(f"  P_converge_wrong = {cw_hit}/{cw_tot} = {p_cw:.3f}  (오답 2모델의 오답 수렴율)")
    return p_ac, p_cw


def report_synth(p_ac: float, p_cw: float) -> None:
    """합성 test 110문항(structured만 투표, routing은 static ax 유지) 기대 정답률.

    ※ 이 절은 **Phase A의 확률 추정**이며 Phase B에서 실측으로 대체됐다(`e1_synth_eval.py`).
      실측: (b) 86.4% / 투표 89.1% / 캐스케이드 90.0% / oracle 90.9%. 추정이 투표를 과소평가한
      이유는 골든에 오답 2개가 겹치는 문항이 없어 P_converge_wrong이 0/0(미측정)이었기 때문이다.

    라벨(labels_*.jsonl)에는 rows가 없어 number-set 비교가 불가 → 골든 실측 모수로 확률 모델.
      k=3(전원 정답)          → 1.0
      k=2                     → P_ac + (1-P_ac)·1{solar 정답}   (과반 없으면 fallback=solar)
      k=1                     → (1-P_cw)·1{solar 정답}          (오답 2개가 수렴하면 오답 확정)
      k=0                     → 0.0
    """
    rows = {json.loads(l)["id"]: json.loads(l) for l in open(DATA / "labeled.jsonl", encoding="utf-8")}
    split = json.load(open(DATA / "split.json", encoding="utf-8"))
    test = [rows[i] for i in split["test"] if i in rows]
    n = len(test)

    def acc(fn) -> float:
        return sum(fn(r) for r in test) / n * 100

    static_acc = acc(lambda r: float(r[f"correct_{STATIC[r['task']]}"]))
    oracle_acc = acc(lambda r: float(any(r[f"correct_{m}"] for m in WORKERS)))
    solar_acc = acc(lambda r: float(r["correct_solar"]))

    def vote_p(r: dict) -> float:
        if r["task"] != "structured":
            return float(r[f"correct_{STATIC['routing']}"])  # 투표 미적용 (헤드룸 0)
        k = sum(r[f"correct_{m}"] for m in WORKERS)
        s = float(r["correct_solar"])
        if k == 3:
            return 1.0
        if k == 2:
            return p_ac + (1 - p_ac) * s
        if k == 1:
            return (1 - p_cw) * s
        return 0.0

    vote_acc = acc(vote_p)
    # 낙관/비관 밴드 (모수 불확실성)
    opt = acc(lambda r: vote_p(r) if r["task"] != "structured" else _band(r, 1.0, 0.0))
    pes = acc(lambda r: vote_p(r) if r["task"] != "structured" else _band(r, 0.0, 1.0))

    n_st = sum(1 for r in test if r["task"] == "structured")
    print(f"\n  합성 test {n}문항 (structured {n_st} / routing {n - n_st})")
    print(f"  {'구성':32} {'정답률':>8}")
    print(f"  {'(a) 항상 solar':32} {solar_acc:7.1f}%")
    print(f"  {'(b) static 규칙':32} {static_acc:7.1f}%   ← d5 실측 85.5% 재현 확인")
    print(f"  {'(c) 학습 선택기 최고(티어②/③)':32} {87.3:7.1f}%   (d5 기록)")
    print(f"  {'(d) 투표 [실측 모수 기대값]':32} {vote_acc:7.1f}%   밴드 [{pes:.1f}, {opt:.1f}]")
    print(f"  {'oracle 상한':32} {oracle_acc:7.1f}%")
    print("\n  ※ 밴드 = 모수 극단 가정(낙관: 정답끼리 항상 일치·오답끼리 절대 미수렴 /")
    print("     비관: 정답끼리 절대 불일치·오답끼리 항상 수렴). 골든 표본이 작아 모수 불확실.")


def _band(r: dict, p_ac: float, p_cw: float) -> float:
    k = sum(r[f"correct_{m}"] for m in WORKERS)
    s = float(r["correct_solar"])
    if k == 3:
        return 1.0
    if k == 2:
        return p_ac + (1 - p_ac) * s
    if k == 1:
        return (1 - p_cw) * s
    return 0.0


def main() -> None:
    table = build_table()
    report_correlation(table)
    report_vote(table)
    report_cascade(table)
    p_ac, p_cw = measure_params(table)
    report_synth(p_ac, p_cw)
    print(f"\n{'=' * 78}\nPhase A 완료 (신규 LLM 콜 0회). 게이트 G-A→B 판정은 계획서 §8 참조.\n{'=' * 78}")


if __name__ == "__main__":
    main()
