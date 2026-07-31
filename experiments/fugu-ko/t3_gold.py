"""T3 ground-truth 채점: baseline 일치도가 아니라 **실DB 정답**으로 채점.

동기(D2 발견): 기준선 gpt-4o-mini가 '🎯 NOVA 영입 후보 리스트' db_name에서 이모지를
누락해 0을 반환(오답)했는데, 정답(19) 낸 solar/exaone가 "baseline 불일치"로 감점됨.
→ baseline은 정답이 아니다. 실DB에서 canonical 집계로 정답 number-set을 만들어 채점.

채점법: 각 모델 결과 rows에서 **정수 집합**을 추출해 gold 정수 집합과 비교(number-set match).
groupby vs scalar 같은 shape 차이에 강건하고("완료 몇 개"의 [[771]]과 [['완료',771]] 둘 다
{771} 포함), 틀린 질의는 숫자가 어긋난다. 정답이 모호한 문항(대상 db 불명확)은 제외.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import psycopg

RAW = Path(__file__).resolve().parent / "analysis" / "raw"
MODELS = ["solar", "ax", "exaone", "baseline"]
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")

# id -> (db_name, kind, group_key|None, extra_where|None). kind: count | groupby
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
# 제외(정답 모호 — 대상 db/해석 불명확): t3-08(Blocked 표기), t3-r03(직원 db 모호),
# t3-r04(열려있는=상태 해석), t3-r05(프로젝트 목록), t3-r08(회고 db 부재 가능),
# 그리고 필터/정렬/메타형 t3-r01/r02/r06/r07/r10 (number-set 채점 부적합) → 게이트통과만 집계.


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


def load(model: str) -> dict[str, dict]:
    f = RAW / f"t3_{model}.jsonl"
    return {r["id"]: r for r in map(json.loads, f.read_text(encoding="utf-8").splitlines()) if r}


def main() -> None:
    data = {m: load(m) for m in MODELS}
    gold = {i: gold_numbers(*spec) for i, spec in SPECS.items()}

    print("== T3 ground-truth 채점 (number-set match, 채점가능 %d문항) ==" % len(SPECS))
    print(f"{'model':9} {'correct':>9} {'rate':>6}")
    score: dict[str, int] = {}
    for m in MODELS:
        ok = 0
        for i, g in gold.items():
            r = data[m].get(i, {})
            if r.get("status") == "executed" and g and g <= model_numbers(r.get("rows")):
                ok += 1
        score[m] = ok
        tag = " (기준선)" if m == "baseline" else ""
        print(f"{m:9} {ok}/{len(gold):<7} {ok/len(gold)*100:5.0f}%{tag}")

    print("\n  [문항별 정오 — G=정답 X=오답 · gold]")
    print(f"    {'id':8} {'gold':<18} " + " ".join(f"{m[:4]:>4}" for m in MODELS))
    for i, g in gold.items():
        marks = []
        for m in MODELS:
            r = data[m].get(i, {})
            good = r.get("status") == "executed" and g and g <= model_numbers(r.get("rows"))
            marks.append("  G " if good else "  X ")
        print(f"    {i:8} {str(sorted(g))[:18]:<18} " + "".join(marks))


if __name__ == "__main__":
    main()
