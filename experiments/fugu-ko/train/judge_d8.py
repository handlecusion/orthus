"""D8 본판정 — d8-prereg §3/§4 동결 명세의 1회 실행 (D8 판정셋 1,438).

입력(전부 사전 캡처/생성 — 본 스크립트는 신규 LLM 콜 0):
  golden/t3_d8_holdout.json           판정셋(gold 동결, zero=gold [0])
  analysis/raw/d8_worker_sql.jsonl    워커 캡처 (solar k=5 + ax + exaone, SQL 포함)
  train/data/sft_d8_gen.jsonl         c8(SFT 워커) k=5 생성물 (--gen_file로 변경 가능)

동결 정의(d8-prereg §3) 구현:
  공통 글루(양쪽 동등):
    snap     db_name이 카탈로그에 없으면 공백무시 유일일치로 교체
    probe-0  최종 후보가 null(에러|빈|{0})이고 채택 SQL의 모든 equality 필터값이
             해당 컬럼(실재 확인)에 SELECT EXISTS로 부재 확인되면 최종 답 {0} 확정.
             LIKE/ILIKE 등 비등호 필터가 섞이면 부재 확인 불가 → 미발화.
             db_name이 카탈로그 밖이거나 필터가 없으면 미발화(깨진 SQL에 0 확정 방지).
    폴백 체인에서 첫 non-null 채택, 전부 null이면 1차 답 유지
  R+  = solar k=5 자기일관성(null 제외 다수결) → snap → 결정론 수리(repair.py)
        → probe-0 → null 폴백(ax → exaone)
  c8  = SFT k=5 자기일관성(greedy+4×t0.7, 실행결과 null 제외 다수결; 유효 0이면
        greedy 채택) → snap(샘플별) → probe-0 → null 폴백(solar t0 → ax)

판정(§4 = D7 §6 상속, 헤드룸 게이트 폐지):
  McNemar p<0.05(c8>R+) ∧ 부트스트랩 10k P(c8>R+)>95% ∧ zero 슬라이스 비열등 ∧ 단일 답.
  R0~R+ 사다리·oracle 병기 보고(게이트 아님). 승리 시 독립 재현셋 1회 통과가 최종.

실행: python train/judge_d8.py [--gen_file data/sft_d8_gen.jsonl]   # 1회
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from math import comb
from pathlib import Path

import sqlalchemy as sa

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from build_sft_data import load_db_keys  # noqa: E402
from repair import propose_repairs  # noqa: E402
from sft_score import _engine, execute, gate_ok, parse_sql  # noqa: E402

GOLDEN = ROOT / "golden"
RAW = ROOT / "analysis" / "raw"
DATA = HERE / "data"
CATALOG = set(load_db_keys())

_DB_RE = re.compile(r"db_name = '((?:[^']|'')*)'")
_FILTER_RE = re.compile(r"properties\s*->>\s*'((?:[^']|'')+)'\s*=\s*'((?:[^']|'')*)'")


def snap_dbname(sql: str) -> str:
    m = _DB_RE.search(sql)
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


def probe_zero(sql: str | None) -> bool:
    """채택 SQL의 모든 equality 필터값 부재가 확인되면 True (→ 최종 답 {0})."""
    if not sql:
        return False
    m = _DB_RE.search(sql)
    if not m:
        return False
    db = m.group(1).replace("''", "'")
    if db not in CATALOG:
        return False
    if re.search(r"(?i)\b(like|ilike)\b", sql):
        return False  # 비등호 필터 — 부재 확인 불가
    filters = _FILTER_RE.findall(sql)
    if not filters:
        return False
    try:
        with _engine.connect() as c:
            for col, val in filters:
                col_u, val_u = col.replace("''", "'"), val.replace("''", "'")
                col_exists = c.execute(
                    sa.text("SELECT EXISTS(SELECT 1 FROM notion_rows WHERE scope='company' AND db_name=:db AND properties ? :col)"),
                    {"db": db, "col": col_u},
                ).scalar()
                if not col_exists:
                    return False  # 컬럼 자체가 없음 — 깨진 SQL에 0 확정 방지
                val_exists = c.execute(
                    sa.text("SELECT EXISTS(SELECT 1 FROM notion_rows WHERE scope='company' AND db_name=:db AND properties ->> :col = :val)"),
                    {"db": db, "col": col_u, "val": val_u},
                ).scalar()
                if val_exists:
                    return False  # 값이 실재 — null은 다른 원인
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen_file", default="data/sft_d8_gen.jsonl", help="c8 k=5 생성물 (HERE 기준 상대)")
    ap.add_argument("--out_json", default="data/judge_d8_result.json")
    a = ap.parse_args()

    items = {it["id"]: it for it in json.loads((GOLDEN / "t3_d8_holdout.json").read_text(encoding="utf-8"))["items"]}
    qids = list(items)
    gold = {i: set(items[i]["gold"]) for i in qids}
    n = len(qids)

    # 워커 캡처: cap[id][model] = [sample0, ...]
    cap: dict[str, dict[str, list[dict]]] = {i: {} for i in qids}
    for line in (RAW / "d8_worker_sql.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        cap[r["id"]].setdefault(r["model"], []).append(r)
    for i in qids:
        for m in cap[i]:
            cap[i][m].sort(key=lambda x: x["sample_i"])

    # c8 생성물: gen[id] = [raw_sample0, ...]
    gen: dict[str, dict[int, str]] = {}
    gen_path = HERE / a.gen_file if not a.gen_file.startswith("/") else Path(a.gen_file)
    for line in gen_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            gen.setdefault(r["id"], {})[r.get("sample_i", 0)] = r["raw"]
    missing = [i for i in qids if 0 not in gen.get(i, {})]
    assert not missing, f"c8 생성 누락 {len(missing)}건 (greedy 샘플 없음): {missing[:3]}"

    def covers(nums: set[int], i: str) -> bool:
        return bool(gold[i]) and gold[i] <= nums

    def worker_t0(i: str, m: str) -> tuple[str, set[int]]:
        s = cap[i].get(m, [{}])[0]
        return s.get("status", "err"), set(s.get("nums", []))

    # ── R+ (동결 §3) ─────────────────────────────────────────────────────────
    def rplus_answer(i: str) -> tuple[set[int], dict]:
        ev: dict = {}
        samples = cap[i].get("solar", [])
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
                st, nums2 = run_sql(snapped)
                if st == "executed":
                    sql, nums = snapped, nums2
                    ev["snap"] = True
        if sql:
            reps = propose_repairs(items[i]["q"], sql)
            if reps:
                st, nums2 = run_sql(reps[-1][1])
                if st == "executed":
                    sql, nums = reps[-1][1], nums2
                    ev["repair"] = True
        primary = nums
        if not is_null(nums):
            return nums, ev
        if probe_zero(sql):
            ev["probe0"] = True
            return {0}, ev
        for m in ("ax", "exaone"):
            st, wn = worker_t0(i, m)
            if not is_null(wn, st):
                ev["fallback"] = m
                return wn, ev
        return primary, ev

    # ── c8 (동결 §3) ─────────────────────────────────────────────────────────
    def c8_answer(i: str) -> tuple[set[int], dict]:
        ev: dict = {}
        runs: list[tuple[str | None, str, set[int]]] = []  # (sql, status, nums) — 샘플순
        for si in sorted(gen.get(i, {})):
            sql = parse_sql(gen[i][si])
            if sql:
                sql = snap_dbname(sql)
            st, nums = run_sql(sql)
            runs.append((sql, st, nums))
        valid = [r for r in runs if not is_null(r[2], r[1])]
        if valid:
            top = Counter(tuple(sorted(r[2])) for r in valid).most_common(1)[0][0]
            sql, _, nums = next(r for r in valid if tuple(sorted(r[2])) == top)
            ev["sc_valid"] = len(valid)
        else:
            sql, st, nums = runs[0] if runs else (None, "err", set())
            nums = nums if (runs and runs[0][1] == "executed") else set()
        primary = nums
        if not is_null(nums):
            return nums, ev
        if probe_zero(sql):
            ev["probe0"] = True
            return {0}, ev
        for m in ("solar", "ax"):
            st, wn = worker_t0(i, m)
            if not is_null(wn, st):
                ev["fallback"] = m
                return wn, ev
        return primary, ev

    RE, CE = {}, {}
    R, C = {}, {}
    for i in qids:
        rn, rev = rplus_answer(i)
        cn, cev = c8_answer(i)
        R[i], C[i] = covers(rn, i), covers(cn, i)
        RE[i], CE[i] = rev, cev

    # 사다리·오라클 병기 (§4 — 게이트 아님)
    w_ok = {m: {i: covers(worker_t0(i, m)[1], i) for i in qids} for m in ("solar", "ax", "exaone")}
    oracle = {i: any(w_ok[m][i] for m in w_ok) for i in qids}

    def pct(d: dict) -> float:
        return sum(d.values()) / n * 100

    print(f"\n══ D8 본판정 (n={n}, d8-prereg §3/§4 동결 명세) ══")
    print(f"solar {pct(w_ok['solar']):.1f}% · ax {pct(w_ok['ax']):.1f}% · exaone {pct(w_ok['exaone']):.1f}%")
    print(f"oracle(3워커 고르기 천장) {pct(oracle):.1f}%  ← 병기(게이트 폐지)")
    print(f"규칙기 R+ {pct(R):.1f}%   학습기 c8 {pct(C):.1f}%")

    # McNemar
    b = sum(1 for i in qids if C[i] and not R[i])
    c = sum(1 for i in qids if not C[i] and R[i])
    m2 = b + c
    p = min(1.0, 2 * sum(comb(m2, k) for k in range(min(b, c) + 1)) / 2**m2) if m2 else 1.0
    print(f"\n[McNemar] c8 득{b}/실{c}, p={p:.3g} → {'충족' if p < 0.05 and b > c else '미충족'}")

    # paired 부트스트랩 10k
    rng = random.Random(42)
    ids = list(qids)
    wins = 0
    for _ in range(10_000):
        s = [ids[rng.randrange(n)] for _ in range(n)]
        dc = sum(C[i] for i in s) - sum(R[i] for i in s)
        wins += dc > 0
    print(f"[부트스트랩] P(c8>R+) = {wins / 100:.1f}% → {'충족(>95)' if wins > 9500 else '미충족'}")

    # zero 슬라이스 비열등
    z = [i for i in qids if items[i]["tag"] == "zero"]
    zb = sum(1 for i in z if C[i] and not R[i])
    zc = sum(1 for i in z if not C[i] and R[i])
    zm = zb + zc
    zp = min(1.0, 2 * sum(comb(zm, k) for k in range(min(zb, zc) + 1)) / 2**zm) if zm else 1.0
    worse = zp < 0.05 and zc > zb
    print(f"[zero 슬라이스 n={len(z)}] c8 {sum(C[i] for i in z) / len(z) * 100:.1f}% vs R+ {sum(R[i] for i in z) / len(z) * 100:.1f}% (득{zb}/실{zc}, p={zp:.3g}) → {'열등(FAIL)' if worse else '비열등 충족'}")

    print("\n[태그별] tag: c8 / R+")
    for t in ("count", "filter", "filter2", "groupby", "ambig", "zero"):
        ti = [i for i in qids if items[i]["tag"] == t]
        if ti:
            print(f"  {t:8s}: {sum(C[i] for i in ti) / len(ti) * 100:5.1f}% / {sum(R[i] for i in ti) / len(ti) * 100:5.1f}%  (n={len(ti)})")

    probe_r = sum(1 for i in qids if RE[i].get("probe0"))
    probe_c = sum(1 for i in qids if CE[i].get("probe0"))
    print(f"\n[probe-0 발화] R+ {probe_r}건 · c8 {probe_c}건")

    verdict = p < 0.05 and b > c and wins > 9500 and not worse
    print(f"\n══ 판정: {'c8(학습기) 승 — §4 전 기준 충족 (독립 재현셋 통과 시 최종)' if verdict else 'c8 미돌파 — D7 §9 문안으로 보고'} ══")

    out_p = HERE / a.out_json if not a.out_json.startswith("/") else Path(a.out_json)
    out_p.write_text(
        json.dumps({"n": n, "solar": pct(w_ok["solar"]), "ax": pct(w_ok["ax"]), "exaone": pct(w_ok["exaone"]),
                    "oracle": pct(oracle), "rplus": pct(R), "c8": pct(C),
                    "mcnemar": {"b": b, "c": c, "p": p}, "bootstrap_win_pct": wins / 100,
                    "zero": {"b": zb, "c": zc, "p": zp}, "probe0": {"rplus": probe_r, "c8": probe_c},
                    "gen_file": str(gen_path), "verdict_win": verdict,
                    "per_item": {i: {"c8": C[i], "rplus": R[i]} for i in qids}}, ensure_ascii=False),
        encoding="utf-8")
    print(f"[저장] {out_p}")


if __name__ == "__main__":
    main()
