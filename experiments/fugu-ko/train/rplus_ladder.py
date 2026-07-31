"""D7 — R+ 사다리 측정 (R0→R5) on **학습셋 파일럿**.

정정당당의 핵심(prereg §0/§3): 내가 짤 수 있는 결정론/추론시점 기법을 **전부 R+에 준다.**
그 위에서 학습기 몫이 얼마나 남는지를 학습셋에서 먼저 잰다("남은 게 없으면 D6가 옳다").

입력 = `sc_pilot_solar.jsonl`(solar k=5, SQL 포함). R+ 각 단계는 **캡처된 SQL을 수리해
격리 DB에 재실행**(읽기전용) — 새 API 호출 0. ax/exaone 폴백은 `labeled2.jsonl`의 정오
라벨을 쓴다(그 워커가 그 문항에서 맞았는지 = 이미 라벨됨).

R0 always solar (single, 샘플0=temp0)
R1 + self-consistency: k=5 실행결과 다수결 (null 답 제외 후)
R2 + null 폴백: 채택안이 에러/빈/{0} → ax → exaone (labeled2 라벨)
R3 + SQL 수리: 총계질문 GROUP BY 제거 재실행 (repair.py)
R4 + SQL 수리: 군더더기 NOT NULL 제거 재실행
R5 = R1~R4 전부 결합 (실질 R+)

실행: python train/rplus_ladder.py   (node.env + 0706 DSN)
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from math import comb
from pathlib import Path

import sqlalchemy as sa

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from repair import propose_repairs  # noqa: E402
from t3_gold import model_numbers  # noqa: E402

DATA = HERE / "data"
DSN = "postgresql://orthus:orthus@localhost:5433/orthus_company_0706"
_engine = sa.create_engine(DSN)


def run_sql(sql: str) -> tuple[str, list[int]]:
    """읽기전용 재실행 → (status, 숫자셋). 캡처된 SQL은 이미 게이트 통과분이라 안전."""
    try:
        with _engine.connect() as c:
            rows = [list(r) for r in c.execute(sa.text(sql)).fetchall()]
        return "executed", sorted(model_numbers(rows))
    except Exception:  # noqa: BLE001
        return "err", []


def is_null(nums: list[int], status: str = "executed") -> bool:
    return status != "executed" or not nums or set(nums) == {0}


def ok(nums: list[int], gold: list[int]) -> bool:
    return bool(gold) and set(gold) <= set(nums)


def mcnemar(a: dict, b: dict) -> tuple[int, int, float]:
    ids = set(a) & set(b)
    w = sum(1 for i in ids if a[i] and not b[i])
    lo = sum(1 for i in ids if not a[i] and b[i])
    n = w + lo
    if n == 0:
        return w, lo, 1.0
    k = min(w, lo)
    return w, lo, min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n)


def main() -> None:
    rows = [json.loads(x) for x in (DATA / "sc_pilot_solar.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    labeled = {r["id"]: r for r in (json.loads(x) for x in (DATA / "labeled2.jsonl").read_text(encoding="utf-8").splitlines() if x.strip())}

    # 각 문항에서 정책별 채택 답(nums) 산출 → 정오
    res: dict[str, dict[str, bool]] = {}
    for r in rows:
        qid, q, gold = r["id"], r["q"], r["gold"]
        s0 = r["samples"][0]  # temp0 = 현행 단일

        # R0: solar 단일
        r0_nums = s0["nums"]

        # R1: self-consistency (null 제외 다수결)
        valid = [s for s in r["samples"] if not is_null(s["nums"], s["status"])]
        pool = valid or r["samples"]
        sc_nums = list(Counter(tuple(s["nums"]) for s in pool).most_common(1)[0][0])
        # 다수결 채택안의 SQL(수리에 필요) — 다수표 나온 첫 샘플의 SQL
        sc_sql = next((s["sql"] for s in pool if tuple(s["nums"]) == tuple(sc_nums)), s0["sql"])

        lab = labeled.get(qid, {})
        ax_ok = bool(lab.get("correct_ax"))
        ex_ok = bool(lab.get("correct_exaone"))

        # R2: R1 + null 폴백 (채택안이 null이면 ax→exaone 라벨로 대체)
        def null_fallback(base_ok: bool, base_null: bool) -> bool:
            if not base_null:
                return base_ok
            return ax_ok or ex_ok  # 그 워커가 그 문항 맞았나(labeled2)

        # R3/R4: SQL 수리 — 수리 후 재실행이 gold 맞추면 정답
        def repaired_ok(sql: str | None) -> bool | None:
            for _name, rp in propose_repairs(q, sql):
                st, nums = run_sql(rp)
                if ok(nums, gold):
                    return True
            return None  # 수리 무효/실패

        r["_r0_ok"] = ok(r0_nums, gold)
        r["_r1_ok"] = ok(sc_nums, gold)
        r["_r2_ok"] = null_fallback(ok(sc_nums, gold), is_null(sc_nums))
        # R3 = R2 + groupby/notnull 수리(채택안 기준)
        rep = repaired_ok(sc_sql)
        r["_r5_ok"] = r["_r2_ok"] or (rep is True)
        res[qid] = r

    ids = list(res)
    n = len(ids)
    ladders = [
        ("R0 solar 단일", "_r0_ok"),
        ("R1 +self-consistency", "_r1_ok"),
        ("R2 +null 폴백", "_r2_ok"),
        ("R5 +SQL수리 (=R+)", "_r5_ok"),
    ]
    oracle = {}
    for r in rows:
        lab = labeled.get(r["id"], {})
        oracle[r["id"]] = bool(lab.get("correct_solar") or lab.get("correct_ax") or lab.get("correct_exaone"))

    print(f"\n══ R+ 사다리 (학습셋 파일럿 n={n}) — 정정당당: 규칙 다 R+에 줌 ══\n")
    print(f"{'정책':26s} {'정답률':>7s} {'vs R0':>7s} {'누적득':>7s}")
    r0 = {i: res[i]["_r0_ok"] for i in ids}
    base = sum(r0.values()) / n * 100
    for name, key in ladders:
        cur = {i: res[i][key] for i in ids}
        acc = sum(cur.values()) / n * 100
        print(f"{name:26s} {acc:6.1f}% {acc - base:+6.1f}p")
    orc = sum(oracle.values()) / n * 100
    print(f"{'oracle(고르기 천장)':26s} {orc:6.1f}%")
    # all-wrong = SFT 워커만 갈 수 있는 땅
    aw = sum(1 for i in ids if not oracle[i])
    rplus = {i: res[i]["_r5_ok"] for i in ids}
    print(f"\n전 워커 오답(=SFT 워커 몫): {aw}/{n} ({aw / n * 100:.0f}%)")
    print(f"R+ 최종 {sum(rplus.values()) / n * 100:.1f}% → 학습기가 이 위를 유의하게 넘어야 승리")
    print("  (이 수치는 학습셋 — 판정은 fresh 셋에서, prereg §6)")


if __name__ == "__main__":
    main()
