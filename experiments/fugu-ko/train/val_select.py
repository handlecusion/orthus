"""D8 — 주판정 백본 선택 (학습셋 val 전용, 판정셋 무접촉).

배경(T17): 학습셋 val은 v2/v4 증강 문항을 포함한다. 증강 문항의 타깃 db_name(끝공백·nbsp·
이모지 변형)은 **실 DB에 없는 이름**이라, 모델이 타깃과 글자 하나까지 같은 SQL을 내도 실행하면
0행 → 실행 채점이 정답을 오답으로 찍는다(1.2B v4 실측 51.0%의 정체). 따라서 실행 채점은
증강 val에 무효다.

선택 기준(판정 전 고정 — d8-prereg §3 "학습셋 val로만 선택"의 구체화):
  1차: **타깃 SQL 정확일치율**(공백 정규화) — 전 val 문항에 잘 정의되고 증강/클린 모두 공정.
  동률 시: **클린 부분집합 실행 정답률**(타깃 db_name이 실 카탈로그에 있는 문항만).
  두 지표 모두 동률이면 더 작은 백본(1.2B) 채택 — 생성 비용이 싸고 재현이 쉬움(tie-break 고정).

실행: python train/val_select.py --gen data/sft_val_gen_12b_v4.jsonl --gen2 data/sft_val_gen_32b_v4.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from build_sft_data import load_db_keys  # noqa: E402
from sft_score import execute, gate_ok, parse_sql  # noqa: E402

CATALOG = set(load_db_keys())
_DB_RE = re.compile(r"db_name = '((?:[^']|'')*)'")


def norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().rstrip(";")


def is_clean(target_sql: str) -> bool:
    """타깃 db_name이 실 카탈로그에 있으면 클린(=실행 채점 유효)."""
    m = _DB_RE.search(target_sql or "")
    return bool(m) and m.group(1).replace("''", "'") in CATALOG


def evaluate(gen_path: Path, val_rows: list[dict]) -> dict:
    gen = {}
    for x in gen_path.read_text(encoding="utf-8").splitlines():
        if x.strip():
            r = json.loads(x)
            gen.setdefault(r["id"], {})[r.get("sample_i", 0)] = r["raw"]
    exact = tot = clean_ok = clean_n = 0
    for r in val_rows:
        raw = gen.get(r["id"], {}).get(0)
        if raw is None:
            continue
        tgt = next((m["content"] for m in r["messages"] if m["role"] == "assistant"), None)
        tgt_sql = parse_sql(tgt or "")
        if not tgt_sql:
            continue
        tot += 1
        out_sql = parse_sql(raw)
        if out_sql and norm(out_sql) == norm(tgt_sql):
            exact += 1
        if is_clean(tgt_sql):
            clean_n += 1
            t_st, t_nums = execute(tgt_sql)  # 타깃 실행 결과 = 이 문항의 사실상 gold
            if t_st == "executed" and t_nums:
                if out_sql and gate_ok(out_sql):
                    o_st, o_nums = execute(out_sql)
                    if o_st == "executed" and t_nums <= o_nums:
                        clean_ok += 1
            else:
                clean_n -= 1  # 타깃조차 빈 결과 → 채점 불가 문항 제외
    return {"n": tot, "exact": exact, "exact_pct": exact / tot * 100 if tot else 0.0,
            "clean_n": clean_n, "clean_ok": clean_ok,
            "clean_pct": clean_ok / clean_n * 100 if clean_n else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gen", required=True, help="후보 A 생성물 (예: 1.2B)")
    ap.add_argument("--gen2", default=None, help="후보 B 생성물 (예: 32B)")
    ap.add_argument("--val", default="data/sft_val.jsonl")
    ap.add_argument("--label", default="sft12b_v4")
    ap.add_argument("--label2", default="sft32b_v4")
    a = ap.parse_args()

    val_rows = [json.loads(x) for x in (HERE / a.val).read_text(encoding="utf-8").splitlines() if x.strip()]
    res = {a.label: evaluate(HERE / a.gen if not a.gen.startswith("/") else Path(a.gen), val_rows)}
    if a.gen2:
        res[a.label2] = evaluate(HERE / a.gen2 if not a.gen2.startswith("/") else Path(a.gen2), val_rows)

    print("\n══ 백본 선택 — 학습셋 val (판정셋 무접촉) ══")
    for k, v in res.items():
        print(f"  {k:12s}: 타깃 정확일치 {v['exact_pct']:5.1f}% ({v['exact']}/{v['n']})  |  "
              f"클린 실행 {v['clean_pct']:5.1f}% ({v['clean_ok']}/{v['clean_n']})")
    if len(res) == 2:
        (ka, va), (kb, vb) = res.items()
        if va["exact"] != vb["exact"]:
            win = ka if va["exact_pct"] > vb["exact_pct"] else kb
            why = "타깃 정확일치"
        elif va["clean_ok"] != vb["clean_ok"]:
            win = ka if va["clean_pct"] > vb["clean_pct"] else kb
            why = "클린 실행(1차 동률)"
        else:
            win = "sft12b_v4"
            why = "전 지표 동률 → 고정 tie-break(작은 백본)"
        print(f"\n══ 주판정 백본 = {win}  (근거: {why}) ══")
        (HERE / "data" / "backbone_choice.json").write_text(
            json.dumps({"winner": win, "why": why, "results": res}, ensure_ascii=False), encoding="utf-8")
        print("[저장] train/data/backbone_choice.json")


if __name__ == "__main__":
    main()
