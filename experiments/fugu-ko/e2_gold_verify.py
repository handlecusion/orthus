"""E2 재측정 0단계 — 골든 gold 앵커를 **측정 전에** 실DB/실 wiki-store로 재검증한다.

왜 필요한가: E2 v1/v2를 만든 시점 이후 코퍼스가 자랐다(documents 2,480 → 3,821). number 앵커는
실DB gold라 DB가 바뀌면 정답이 바뀌고, page 앵커는 wiki 페이지 slug가 사라졌으면 앵커가 영원히
불성립한다. **결과를 보기 전에** 앵커를 확정해야 사전 고정 원칙이 지켜진다(§3 채점 프로토콜).

검사 2종:
  number — t3_gold.gold_numbers()로 실DB 재계산 → 골든 values와 대조
  page   — wiki_pages에 그 slug가 실재하는가 (company scope)

--fix 를 주면 어긋난 number 앵커를 **실DB 값으로 갱신**하고 골든을 다시 쓴다. page 앵커가 죽은
문항은 자동 수정하지 않고 **보고만** 한다(질문 자체를 다시 지어야 하므로 — 저작 판단이다).

실행:
  set -a; source ~/.orthus/nodes/company/node.env; set +a
  export ORTHUS_NODE=company ORTHUS_NODE_DB=orthus_company
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/e2_gold_verify.py [--fix]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

from sqlalchemy import text  # noqa: E402

from orthus.db import get_engine  # noqa: E402

GOLDEN = HERE / "golden"


def live_page_slugs() -> set[str]:
    """company scope에 실재하는 wiki page slug 집합."""
    with get_engine().connect() as c:
        rows = c.execute(text("select slug from wiki_pages where scope = 'company'")).fetchall()
    return {r[0].lower() for r in rows}


# v1(e2.json)의 집계 앵커는 **손저작**이라 t3 원문과 텍스트가 일치하지 않는다. 질문 텍스트 부분일치로
# gold를 찾으면 엉뚱한 spec에 붙는다(초기 실행에서 E2-17이 279↔771로 오매칭됐다). 그래서 v1은
# **gold 값마다 그 값을 만든 집계 spec을 명시**한다 — 재현 가능하고, 오매칭이 원리적으로 불가능하다.
V1_SPECS: dict[int, tuple[str, str | None]] = {
    # gold 값 : (db_name, properties 필터 SQL | None)
    794: ("협업업무표", None),
    771: ("협업업무표", "properties->>'상태'='완료'"),
    16: ("협업업무표", "properties->>'상태'='보류'"),
    279: ("Nova 개발 로드맵", None),
    209: ("Nova 개발 로드맵", "properties->>'상태'='Done'"),
    19: ("🎯 NOVA 영입 후보 리스트", None),
    48: ("배포 공지", None),
    49: ("AI관련툴", None),
    28: ("회사 미팅 기록", None),
}


def live_count(db: str, where: str | None) -> int:
    sql = "select count(*) from notion_rows where db_name = :db"
    if where:
        sql += f" and {where}"
    with get_engine().connect() as c:
        return int(c.execute(text(sql), {"db": db}).scalar() or 0)


def numeric_gold_by_question() -> dict[str, set[int]]:
    """t3 골든 질문 텍스트 → 실DB gold number-set (지금 시점 재계산). v2 전용 — v2는 집계 절을
    t3 **원문 그대로** 이어 붙여 만들었으므로 부분일치가 정확하다."""
    from t3_gold import SPECS, gold_numbers

    t3 = {it["id"]: it["q"] for it in json.load(open(GOLDEN / "t3.json", encoding="utf-8"))["items"]}
    out: dict[str, set[int]] = {}
    for tid, spec in SPECS.items():
        if tid in t3:
            out[t3[tid]] = gold_numbers(*spec)
    return out


def check(path: Path, slugs: set[str], numgold: dict[str, set[int]], fix: bool) -> dict:
    doc = json.load(open(path, encoding="utf-8"))
    items = doc["items"]
    v1 = path.name == "e2.json"
    report = {"file": path.name, "n": len(items), "number_ok": 0, "number_drift": [],
              "page_ok": 0, "page_dead": [], "number_unmatched": []}

    def lookup(q_full: str) -> set[int] | None:
        for q, gold in numgold.items():
            if q.rstrip("?").rstrip() in q_full:
                return gold
        return None

    for it in items:
        for a in it["anchors"]:
            if a["kind"] == "number":
                have = {int(v) for v in a["values"]}
                if v1:  # 명시 spec으로 재계산 (오매칭 불가)
                    if len(have) != 1 or next(iter(have)) not in V1_SPECS:
                        report["number_unmatched"].append({"id": it["id"], "values": a["values"]})
                        continue
                    gold = {live_count(*V1_SPECS[next(iter(have))])}
                else:  # v2: t3 원문 부분일치
                    got = lookup(it["q"])
                    if got is None:
                        report["number_unmatched"].append({"id": it["id"], "values": a["values"]})
                        continue
                    gold = got
                if have <= gold:
                    report["number_ok"] += 1
                else:
                    report["number_drift"].append(
                        {"id": it["id"], "golden": sorted(have), "live": sorted(gold)}
                    )
                    if fix and len(gold) == 1:
                        a["values"] = sorted(gold)
            elif a["kind"] == "page":
                dead = [s for s in a.get("slugs", []) if s.lower() not in slugs]
                if dead:
                    report["page_dead"].append({"id": it["id"], "slugs": dead})
                else:
                    report["page_ok"] += 1

    if fix and report["number_drift"]:
        json.dump(doc, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="drift난 number 앵커를 실DB 값으로 갱신")
    ap.add_argument("--files", default="e2.json,e2_v2.json")
    args = ap.parse_args()

    slugs = live_page_slugs()
    numgold = numeric_gold_by_question()
    print(f"live company wiki pages: {len(slugs)} · t3 numeric gold specs: {len(numgold)}\n")

    bad = 0
    for name in args.files.split(","):
        r = check(GOLDEN / name.strip(), slugs, numgold, args.fix)
        print(f"── {r['file']} (n={r['n']})")
        print(f"   number 앵커 OK   : {r['number_ok']}")
        print(f"   number drift     : {len(r['number_drift'])} {r['number_drift'] or ''}")
        print(f"   number unmatched : {len(r['number_unmatched'])} "
              f"{[u['id'] for u in r['number_unmatched']] or ''}")
        print(f"   page 앵커 OK     : {r['page_ok']}")
        print(f"   page DEAD        : {len(r['page_dead'])} {r['page_dead'] or ''}\n")
        bad += len(r["page_dead"]) + (0 if args.fix else len(r["number_drift"]))

    if bad:
        print(f"⚠️  확정 전 처리 필요: {bad}건 (page dead는 문항 재저작, number drift는 --fix)")
    else:
        print("✅ 앵커 전수 유효 — 측정 시작 가능")


if __name__ == "__main__":
    main()
