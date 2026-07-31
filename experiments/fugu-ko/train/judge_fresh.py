"""D7 본판정 — prereg §10 동결 명세의 1회 실행 (fresh 판정셋 1,063).

입력(전부 사전 캡처 — 본 스크립트는 신규 LLM 콜 0):
  golden/t3_fresh_holdout.json          판정셋(gold 동결)
  analysis/raw/fresh_worker_sql.jsonl   워커 캡처 (solar k=5 + ax + exaone, SQL 포함)
  train/data/sft_fresh_gen_v2.jsonl     c2(SFT 워커) 생성물

동결 정의(§10.1/§10.2) 구현:
  c2  = SFT 답 → db_name 스냅 → 실행 → null(에러|빈|{0})이면 solar(t0) → ax
  R+  = solar k=5 자기일관성(null 제외 다수결) → 스냅 → 결정론 수리(총계질문 GROUP BY/
        NOT NULL — gold 무참조, 조건 발화 시 무조건 적용) → null이면 ax → exaone
  최종 답 semantics(양쪽 동일): 폴백 체인에서 첫 non-null 답 채택, 전부 null이면 1차 답 유지
  (→ 정답 0 문항에서 {0}을 낸 1차가 그대로 최종 답이 되는 대칭 구조)

판정(§6): ①헤드룸 게이트(oracle−R+<3%p → 중단 보고) ②McNemar c2 vs R+ ③paired 부트스트랩
10k P(c2>R+) ④정답0 슬라이스 비열등 ⑤단일 답(구조상 항상 충족).

실행: python train/judge_fresh.py            # 1회 — 결과는 stdout + judge_fresh_result.json
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

import re  # noqa: E402

from build_sft_data import load_db_keys  # noqa: E402
from repair import propose_repairs  # noqa: E402
from sft_score import execute, gate_ok, parse_sql  # noqa: E402

GOLDEN = ROOT / "golden"
RAW = ROOT / "analysis" / "raw"
DATA = HERE / "data"
CATALOG = set(load_db_keys())


def snap_dbname(sql: str) -> str:
    m = re.search(r"db_name = '([^']*)'", sql)
    if not m:
        return sql
    name = m.group(1)
    if name in CATALOG:
        return sql
    cand = [n for n in CATALOG if n.strip() == name.strip()]
    if len(cand) == 1:
        return sql[: m.start(1)] + cand[0].replace("'", "''") + sql[m.end(1) :]
    return sql


def is_null(nums: set[int] | list[int] | None, status: str = "executed") -> bool:
    s = set(nums or [])
    return status != "executed" or not s or s == {0}


def run_sql(sql: str | None) -> tuple[str, set[int]]:
    if not sql or not gate_ok(sql):
        return "rejected", set()
    return execute(sql)


def main() -> None:
    items = {it["id"]: it for it in json.loads((GOLDEN / "t3_fresh_holdout.json").read_text(encoding="utf-8"))["items"]}
    qids = list(items)
    gold = {i: set(items[i]["gold"]) for i in qids}

    # 캡처 로드: cap[id][model] = [sample0, sample1, ...]
    cap: dict[str, dict[str, list[dict]]] = {i: {} for i in qids}
    for line in (RAW / "fresh_worker_sql.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        cap[r["id"]].setdefault(r["model"], []).append(r)
    for i in qids:
        for m in cap[i]:
            cap[i][m].sort(key=lambda x: x["sample_i"])

    gen = {json.loads(x)["id"]: json.loads(x)["raw"] for x in (DATA / "sft_fresh_gen_v2.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()}

    def covers(nums: set[int], i: str) -> bool:
        return bool(gold[i]) and gold[i] <= nums

    def worker_t0(i: str, m: str) -> tuple[str, set[int]]:
        s = cap[i].get(m, [{}])[0]
        return s.get("status", "err"), set(s.get("nums", []))

    # ── R+ (동결 §10.2) ───────────────────────────────────────────────────────
    def rplus_answer(i: str) -> set[int]:
        samples = cap[i].get("solar", [])
        valid = [s for s in samples if not is_null(s.get("nums"), s.get("status", "err"))]
        pool = valid or samples
        if pool:
            top = Counter(tuple(s.get("nums", [])) for s in pool).most_common(1)[0][0]
            nums = set(top)
            sql = next((s.get("sql") for s in pool if tuple(s.get("nums", [])) == top), None)
        else:
            nums, sql = set(), None
        # 스냅 (변경 시 재실행)
        if sql:
            snapped = snap_dbname(sql)
            if snapped != sql:
                st, nums2 = run_sql(snapped)
                if st == "executed":
                    sql, nums = snapped, nums2
        # 결정론 수리 (gold 무참조 — 조건 발화 시 마지막 수리안 채택)
        if sql:
            reps = propose_repairs(items[i]["q"], sql)
            if reps:
                st, nums2 = run_sql(reps[-1][1])
                if st == "executed":
                    nums = nums2
        primary = nums
        if not is_null(nums):
            return nums
        for m in ("ax", "exaone"):
            st, wn = worker_t0(i, m)
            if not is_null(wn, st):
                return wn
        return primary  # 전부 null → 1차 답 유지

    # ── c2 (동결 §10.1) ───────────────────────────────────────────────────────
    def c2_answer(i: str) -> set[int]:
        sql = parse_sql(gen.get(i, ""))
        if sql:
            sql = snap_dbname(sql)
        st, nums = run_sql(sql)
        primary = nums if st == "executed" else set()
        if not is_null(primary, "executed" if st == "executed" else st):
            return primary
        for m in ("solar", "ax"):
            wst, wn = worker_t0(i, m)
            if not is_null(wn, wst):
                return wn
        return primary

    R = {i: covers(rplus_answer(i), i) for i in qids}
    C = {i: covers(c2_answer(i), i) for i in qids}
    n = len(qids)

    # 사다리·오라클 (보고 의무 §3)
    w_ok = {m: {i: covers(worker_t0(i, m)[1], i) for i in qids} for m in ("solar", "ax", "exaone")}
    oracle = {i: any(w_ok[m][i] for m in w_ok) for i in qids}

    def pct(d: dict) -> float:
        return sum(d.values()) / n * 100

    print(f"\n══ D7 본판정 (fresh n={n}, prereg §10 동결 명세) ══")
    print(f"solar {pct(w_ok['solar']):.1f}% · ax {pct(w_ok['ax']):.1f}% · exaone {pct(w_ok['exaone']):.1f}%")
    print(f"oracle(3워커 고르기 천장) {pct(oracle):.1f}%")
    print(f"규칙기 R+ {pct(R):.1f}%   학습기 c2 {pct(C):.1f}%")

    # ① 헤드룸 게이트
    gate = pct(oracle) - pct(R)
    print(f"\n[게이트] oracle − R+ = {gate:+.1f}%p → {'PASS(≥3)' if gate >= 3 else 'FAIL(<3) — §2 명세상 중단 보고'}")

    # ② McNemar
    b = sum(1 for i in qids if C[i] and not R[i])
    c = sum(1 for i in qids if not C[i] and R[i])
    m2 = b + c
    p = min(1.0, 2 * sum(comb(m2, k) for k in range(min(b, c) + 1)) / 2**m2) if m2 else 1.0
    print(f"[McNemar] c2 득{b}/실{c}, p={p:.3g} → {'충족' if p < 0.05 and b > c else '미충족'}")

    # ③ paired 부트스트랩 10k
    rng = random.Random(42)
    ids = list(qids)
    wins = 0
    for _ in range(10_000):
        s = [ids[rng.randrange(n)] for _ in range(n)]
        dc = sum(C[i] for i in s) - sum(R[i] for i in s)
        wins += dc > 0
    print(f"[부트스트랩] P(c2>R+) = {wins / 100:.1f}% → {'충족(>95)' if wins > 9500 else '미충족'}")

    # ④ 정답 0 슬라이스 비열등
    z = [i for i in qids if items[i]["tag"] == "zero"]
    zb = sum(1 for i in z if C[i] and not R[i])
    zc = sum(1 for i in z if not C[i] and R[i])
    zm = zb + zc
    zp = min(1.0, 2 * sum(comb(zm, k) for k in range(min(zb, zc) + 1)) / 2**zm) if zm else 1.0
    worse = zp < 0.05 and zc > zb
    print(f"[정답0 슬라이스 n={len(z)}] c2 {sum(C[i] for i in z) / len(z) * 100:.1f}% vs R+ {sum(R[i] for i in z) / len(z) * 100:.1f}% (득{zb}/실{zc}, p={zp:.3g}) → {'열등(FAIL)' if worse else '비열등 충족'}")

    # 태그별 분해 (보고용)
    print("\n[태그별] tag: c2 / R+")
    for t in ("count", "filter", "filter2", "groupby", "ambig", "zero"):
        ti = [i for i in qids if items[i]["tag"] == t]
        if ti:
            print(f"  {t:8s}: {sum(C[i] for i in ti) / len(ti) * 100:5.1f}% / {sum(R[i] for i in ti) / len(ti) * 100:5.1f}%  (n={len(ti)})")

    verdict = gate >= 3 and p < 0.05 and b > c and wins > 9500 and not worse
    print(f"\n══ 판정: {'c2(학습기) 승 — §6 전 기준 충족 (재현 셋 통과 시 최종)' if verdict else 'R+ 미돌파 또는 게이트 미통과 — §9 문안 검토'} ══")

    (DATA / "judge_fresh_result.json").write_text(
        json.dumps({"n": n, "solar": pct(w_ok["solar"]), "ax": pct(w_ok["ax"]), "exaone": pct(w_ok["exaone"]),
                    "oracle": pct(oracle), "rplus": pct(R), "c2": pct(C), "gate": gate,
                    "mcnemar": {"b": b, "c": c, "p": p}, "bootstrap_win_pct": wins / 100,
                    "zero": {"b": zb, "c": zc, "p": zp}, "verdict_win": verdict,
                    "per_item": {i: {"c2": C[i], "rplus": R[i]} for i in qids}}, ensure_ascii=False),
        encoding="utf-8")
    print("[저장] train/data/judge_fresh_result.json")


if __name__ == "__main__":
    main()
