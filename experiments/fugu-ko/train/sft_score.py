"""D7 — SFT 워커 생성물 채점 (로컬, 격리 DB).

입력: sft_worker.py --gen 산출(raw 생성 텍스트) + 원본 문항(gold).
절차: raw → {"sql"} 파싱 → **실검증 게이트**(sqlglot SELECT-only) → scope 래핑 → 격리 DB
읽기전용 실행 → number-set 채점(gold ⊆ 반환셋, D6 동일 메트릭).

파싱/게이트 실패 = 오답(실배포와 동일 계약 — compile_output_invalid는 rejected).

실행: python train/sft_score.py --gen data/sft_val_gen.jsonl --ref data/sft_val.jsonl \
        [--labeled data/labeled2.jsonl]   # labeled 주면 워커 3종과 나란히 비교
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import sqlalchemy as sa
import sqlglot
from sqlglot import exp

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from t3_gold import model_numbers  # noqa: E402

def _dsn() -> str:
    """드라이버 비종속 DSN — venv에 psycopg2/psycopg 중 뭐가 있든 붙는다(T15)."""
    import importlib.util as _u

    base = "orthus:orthus@localhost:5433/orthus_company_0706"
    driver = "psycopg2" if _u.find_spec("psycopg2") else "psycopg"
    return f"postgresql+{driver}://{base}"


DSN = _dsn()
_engine = sa.create_engine(DSN)


def parse_sql(raw: str) -> str | None:
    """생성 텍스트에서 {"sql": ...} 추출 — compile_query와 동일하게 JSON 우선."""
    raw = raw.strip()
    try:
        return json.loads(raw)["sql"]
    except Exception:  # noqa: BLE001
        m = re.search(r'\{\s*"sql"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.S)
        if m:
            try:
                return json.loads('"' + m.group(1) + '"')
            except Exception:  # noqa: BLE001
                return None
    return None


def gate_ok(sql: str) -> bool:
    """검증 게이트 축약판: 파싱 성공 + 단일 SELECT + 다중문 금지."""
    if ";" in sql.rstrip().rstrip(";"):
        return False
    try:
        t = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:  # noqa: BLE001
        return False
    return isinstance(t, (exp.Select,))


def execute(sql: str) -> tuple[str, set[int]]:
    scoped = sql.replace(
        "FROM notion_rows", "FROM (SELECT * FROM notion_rows WHERE scope='company') AS notion_rows", 1
    )
    if "LIMIT" not in scoped.upper():
        scoped += " LIMIT 1000"
    try:
        with _engine.connect() as c:
            rows = [list(r) for r in c.execute(sa.text(scoped)).fetchall()]
        return "executed", model_numbers(rows)
    except Exception:  # noqa: BLE001
        return "err", set()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", default="train/data/sft_val_gen.jsonl")
    ap.add_argument("--ref", default="train/data/sft_val.jsonl")
    ap.add_argument("--labeled", default="train/data/labeled2.jsonl")
    a = ap.parse_args()

    gen = {r["id"]: r["raw"] for r in map(json.loads, Path(a.gen).read_text(encoding="utf-8").splitlines()) if r}
    # gold 소스: ref 파일에 gold가 있으면(홀드아웃 프롬프트) 그걸, 없으면 labeled2에서.
    labeled = {r["id"]: r for r in map(json.loads, Path(a.labeled).read_text(encoding="utf-8").splitlines()) if r}
    for r in map(json.loads, Path(a.ref).read_text(encoding="utf-8").splitlines()):
        if r and "gold" in r and r["id"] not in labeled:
            q = next((m["content"] for m in r.get("messages", []) if m["role"] == "user"), "")
            labeled[r["id"]] = {"id": r["id"], "q": q.split("\n")[0], "gold": r["gold"], "tag": r.get("tag")}
    ids = [i for i in gen if i in labeled]

    n = len(ids)
    ok_cnt = 0
    fail = {"parse": 0, "gate": 0, "exec": 0, "wrong": 0}
    wrongs = []
    for i in ids:
        gold = set(labeled[i]["gold"])
        sql = parse_sql(gen[i])
        if sql is None:
            fail["parse"] += 1
            continue
        if not gate_ok(sql):
            fail["gate"] += 1
            continue
        st, nums = execute(sql)
        if st != "executed":
            fail["exec"] += 1
            continue
        if gold and gold <= nums:
            ok_cnt += 1
        else:
            fail["wrong"] += 1
            wrongs.append((i, labeled[i]["q"][:60], sorted(gold), sorted(nums)[:6], sql[:120]))

    print(f"\n══ SFT 워커 채점 (n={n}) ══")
    print(f"정답 {ok_cnt}/{n} = {ok_cnt / n * 100:.1f}%   실패내역 {fail}")
    # 워커 대조: labeled2 정오 or (홀드아웃이면) 동결 GT
    frozen_p = ROOT / "analysis" / "raw" / "unseen_worker_correct.json"
    frozen = json.loads(frozen_p.read_text(encoding="utf-8")) if frozen_p.exists() else {}
    for m in ("solar", "ax", "exaone"):
        vals = [labeled[i].get(f"correct_{m}", frozen.get(m, {}).get(i)) for i in ids]
        known = [v for v in vals if v is not None]
        if known:
            print(f"  (같은 문항 {m:7s} {sum(known) / len(known) * 100:.1f}%)")
    if wrongs:
        print("\n오답 샘플(≤8):")
        for w in wrongs[:8]:
            print("  ", w)


if __name__ == "__main__":
    main()
