"""G3 준비 — 골든 holdout(미학습 79문항)의 문항별 워커 정오 벡터 생성.

학습 선택기(c)는 합성 730문항으로 학습됐고 **골든 79문항은 절대 미학습**(gen.py dedupe).
G3는 이 골든에서 (a)단일 / (b)static / (c)학습 / oracle 4자를 대조한다.

라벨 소스(전부 기존 원자료 재사용, 신규 LLM 콜 0):
- DB질의(T3): `t3_gold.py`와 동일한 실DB number-set 채점 → correct_*(bool). 채점가능 18문항.
- 라우팅(T5): `analysis/raw/t5_*.jsonl`의 `correct` → correct_*(bool). 21문항.
- 지식응답(T2): `analysis/raw/t2_judge.jsonl`의 양방향 쌍대 결과 → score_*(win=1/tie=0.5/loss=0).
  30문항. **선택기 학습 대상이 아님**(계획 §1) — G3에서는 oracle/static 참고용으로만 집계.

출력: train/data/golden_labeled.jsonl
  {id, task: structured|routing|qa, q, correct_solar/ax/exaone (bool),
   score_solar/ax/exaone (float, qa만 0.5 가능)}

실행: FUGU_DSN=... python train/g3_gold.py
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RAW = ROOT / "analysis" / "raw"
GOLDEN = ROOT / "golden"
OUT = HERE / "data" / "golden_labeled.jsonl"
WORKERS = ["solar", "ax", "exaone"]
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")

# t3_gold.py와 동일 (정답이 명확한 count/groupby 18문항; 필터/정렬/메타형은 채점 부적합)
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
SCORE = {"win": 1.0, "tie": 0.5, "loss": 0.0}


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


def load_raw(task: str, model: str) -> dict[str, dict]:
    f = RAW / f"{task}_{model}.jsonl"
    return {r["id"]: r for r in map(json.loads, f.read_text(encoding="utf-8").splitlines()) if r}


def load_golden(task: str) -> dict[str, str]:
    d = json.load(open(GOLDEN / f"{task}.json", encoding="utf-8"))
    return {it["id"]: it["q"] for it in d["items"]}


def main() -> None:
    out: list[dict] = []

    # --- DB질의 (T3): 실DB number-set 채점 ---
    q3 = load_golden("t3")
    raw3 = {m: load_raw("t3", m) for m in WORKERS}
    gold = {i: gold_numbers(*spec) for i, spec in SPECS.items()}
    for i, g in gold.items():
        row = {"id": i, "task": "structured", "q": q3[i]}
        for m in WORKERS:
            r = raw3[m].get(i, {})
            ok = bool(r.get("status") == "executed" and g and g <= model_numbers(r.get("rows")))
            row[f"correct_{m}"] = ok
            row[f"score_{m}"] = float(ok)
        out.append(row)

    # --- 라우팅 (T5): 기대 route 일치 ---
    q5 = load_golden("t5")
    raw5 = {m: load_raw("t5", m) for m in WORKERS}
    for i, q in q5.items():
        row = {"id": i, "task": "routing", "q": q}
        for m in WORKERS:
            ok = bool(raw5[m].get(i, {}).get("correct"))
            row[f"correct_{m}"] = ok
            row[f"score_{m}"] = float(ok)
        out.append(row)

    # --- 지식응답 (T2): 양방향 쌍대 judge (선택기 미학습 — 참고 집계용) ---
    q2 = load_golden("t2")
    judge: dict[tuple[str, str], str] = {}
    for line in (RAW / "t2_judge.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            judge[(r["worker"], r["id"])] = r["result"]
    for i, q in q2.items():
        row = {"id": i, "task": "qa", "q": q}
        for m in WORKERS:
            s = SCORE.get(judge.get((m, i), "tie"), 0.5)
            row[f"score_{m}"] = s
            row[f"correct_{m}"] = s >= 1.0  # 승리만 '정답'으로 이진화(참고)
        out.append(row)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by = {}
    for r in out:
        by.setdefault(r["task"], []).append(r)
    print(f"골든 라벨 {len(out)}문항 → {OUT}")
    for t, rows in by.items():
        line = f"  {t:11} n={len(rows):3}  "
        for m in WORKERS:
            tot = sum(r[f"score_{m}"] for r in rows)
            line += f"{m}={tot:.1f}/{len(rows)} "
        dis = sum(1 for r in rows if len({r[f"score_{m}"] for m in WORKERS}) > 1)
        print(line + f"| 불일치 {dis}")


if __name__ == "__main__":
    main()
