"""R1 TN 버킷 — **정답이 '없음'인** 집계 질문 (`docs/routing-holdout-plan.md` §3.1/§6-R4).

C3(교차평면 회수)는 structured가 0행이면 wiki로 재질의한다. 그런데 **0행은 두 가지를 뜻한다**:

  (a) 평면이 틀렸다 — 답이 위키에 있는데 DB를 봤다      → 회수해야 한다
  (b) 진짜 없다     — "취소된 티켓 얼마나 되나요?" → 0건이 **정답**  → 회수하면 안 된다

`row_count=0` 하나로는 둘을 구분할 수 없다. (b)에 wiki 폴백을 걸면 정답 "없습니다"가 그럴듯한
답으로 바뀐다 — zero-answer 안전장치(11%→0%)를 스스로 되돌리는 셈이다.

그래서 이 버킷이 **선택이 아니라 필수**다. 여기서 환각이 1건이라도 나오면 R4는 즉시 기각이다.

구성: 실재하는 (표, 필드)에 **실재하지 않는 값**을 물어 0행을 보장한다. 0행임을 SQL로 확인한
것만 남긴다(구성으로 라벨, 검증으로 확인 — 의견 아님).

실행: python r1_tn.py --n 40
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

import psycopg  # noqa: E402

from pool import build_pool  # noqa: E402

OUT = HERE / "analysis" / "r1"
DSN = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")

_SYS = (
    "사내 표에서 **조건에 맞는 항목이 얼마나 되는지** 묻는 자연스러운 한국어 질문 1개를 만들어라.\n"
    "★ 금지어: '몇 개', '개수', '건수', '목록', '리스트'. 대신 '얼마나 되나요/있나요' 등을 써라.\n"
    "★ '표'·'DB'·'데이터베이스' 같은 말을 쓰지 마라.\n"
    "★ 조건을 그대로 반영하되, 그 값이 실제로 존재하는지는 묻지 마라(존재를 전제하고 물어라).\n"
    'JSON만 출력: {"q": "질문"}'
)


def build(n: int) -> None:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        # (표, 필드)별 실재 값 집합
        cur.execute(
            """select db_name, key, array_agg(distinct value) from notion_rows, jsonb_each_text(properties)
               where scope='company' and db_name is not null and value <> ''
                 and length(value) between 2 and 20 and value !~ '\\d{4}-\\d{2}-\\d{2}'
               group by 1,2 having count(distinct value) between 2 and 30""",
        )
        fields = [(d, k, set(vs)) for d, k, vs in cur.fetchall()]

        cands: list[dict] = []
        for db_a, key_a, vals_a in fields:
            # 같은 필드명을 가진 **다른 표**의 값 → 이 표에는 없는 값(도메인은 그럴듯하다)
            for db_b, key_b, vals_b in fields:
                if db_b == db_a or key_b != key_a:
                    continue
                for v in sorted(vals_b - vals_a):
                    cands.append({"db_name": db_a, "field": key_a, "value": v})
                    break
        # 0행임을 **SQL로 확인**한 것만 남긴다 (구성으로 라벨 → 검증으로 확인)
        checked: list[dict] = []
        for c in cands:
            cur.execute(
                """select count(*) from notion_rows
                   where scope='company' and db_name=%s and properties->>%s = %s""",
                (c["db_name"], c["field"], c["value"]),
            )
            if cur.fetchone()[0] == 0:
                checked.append(c)
            if len(checked) >= n:
                break

    if not checked:
        raise SystemExit("TN 후보 없음 — 필드 도메인이 표마다 완전히 겹치는 듯하다")

    pool = build_pool(["solar"])
    chat = pool["solar"]
    out: list[dict] = []
    for i, c in enumerate(checked):
        user = f"표: {c['db_name']}\n조건: '{c['field']}'가 '{c['value']}'인 항목"
        try:
            q = (json.loads(chat.complete(_SYS, user, json_only=True)).get("q") or "").strip()
        except Exception as e:  # noqa: BLE001
            print(f"  실패 {i}: {type(e).__name__}")
            continue
        if not q:
            continue
        out.append({
            "id": f"r1tn-{i:03d}",
            "q": q,
            "expected": "structured",
            "gold_answer": "none",  # 정답 = 없음/0건
            "src_db": c["db_name"],
            "src_field": c["field"],
            "src_value": c["value"],
            "verified_rows": 0,  # SQL로 확인함
        })

    OUT.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(OUT / "tn.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[TN] {len(out)}문항 → {OUT/'tn.json'}  (전건 SQL로 0행 확인)")
    for o in out[:4]:
        print(f"   · {o['q'][:66]}   [{o['src_field']}='{o['src_value']}' → 0행]")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    build(ap.parse_args().n)
