"""E1 Phase B — 합성 test structured 63문항 재실행 (rows 확보) → 투표/캐스케이드 실측.

왜 필요한가(Phase A가 남긴 공백): `train/data/labels_*.jsonl`은 정오(bool)와 status만 저장하고
**rows를 버렸다**. 골든에서 회수를 만든 트리거가 바로 `zero_answer`(결과 number-set == {0})라
rows 없이는 합성 분포에서 캐스케이드를 재현할 수 없다. 합성 test는 (c) 학습 선택기가 (b)를
이겼던 유일한 분포(87.3 vs 85.5)이므로, E1의 진짜 판정은 여기서 난다.

재실행 대상 = split.json의 test 중 task=="structured" 63문항 × 워커 3종 (routing 47문항은
E1 스코프 밖 — 헤드룸 0). 신규 콜 = 189회.

출력: analysis/raw/e1_synth_{solar,ax,exaone}.jsonl  (t3_*.jsonl과 동일 스키마)
부수 산출: 재실행 정오 vs 기존 라벨 정오 드리프트(모델/서비스 변동 신호).

실행:
  set -a; source ~/.orthus/nodes/company/node.env; set +a
  export ORTHUS_NODE=company ORTHUS_NODE_DB=orthus_company
  PYTHONPATH=$PWD python experiments/fugu-ko/e1_synth_rerun.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

from pool import build_pool  # noqa: E402
from t3_gold import gold_numbers, model_numbers  # noqa: E402

from orthus.structured.query import query_structured  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
DATA = HERE / "train" / "data"
RAW = HERE / "analysis" / "raw"
WORKERS = ["solar", "ax", "exaone"]

_gold_cache: dict[tuple, set[int]] = {}


def gold_for(spec: list) -> set[int]:
    key = tuple(spec)
    if key not in _gold_cache:
        _gold_cache[key] = gold_numbers(*spec)
    return _gold_cache[key]


def run_one(chat, item: dict) -> dict:
    """harness.run_t3와 동일 스키마 + gold 대조 correct."""
    t0 = time.monotonic()
    try:
        r = query_structured(UID, item["q"], scope="company", chat_model=chat)
        g = gold_for(item["spec"])
        nums = model_numbers(r.rows)
        return {
            "id": item["id"],
            "tag": item.get("tag"),
            "status": r.status,
            "gate_passed": bool(r.validation.passed),
            "sql": (r.compiled.sql if r.compiled else None),
            "rows": r.rows,
            "row_count": r.row_count,
            "correct": bool(r.status == "executed" and g and g <= nums),
            "ms": int((time.monotonic() - t0) * 1000),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "id": item["id"],
            "tag": item.get("tag"),
            "status": "error",
            "gate_passed": False,
            "rows": None,
            "row_count": 0,
            "correct": False,
            "error": f"{type(e).__name__}: {str(e)[:120]}",
            "ms": int((time.monotonic() - t0) * 1000),
        }


def main() -> None:
    questions = {
        json.loads(l)["id"]: json.loads(l)
        for l in open(DATA / "questions.jsonl", encoding="utf-8")
    }
    split = json.load(open(DATA / "split.json", encoding="utf-8"))
    items = [
        questions[i]
        for i in split["test"]
        if i in questions and questions[i]["task"] == "structured"
    ]
    old = {
        m: {
            json.loads(l)["id"]: json.loads(l)
            for l in open(DATA / f"labels_{m}.jsonl", encoding="utf-8")
        }
        for m in WORKERS
    }

    print(f"[E1 Phase B] 합성 test structured {len(items)}문항 × {WORKERS} = {len(items) * 3}콜\n")
    RAW.mkdir(parents=True, exist_ok=True)
    pool = build_pool(WORKERS)
    for slug in WORKERS:
        chat = pool[slug]
        t0 = time.monotonic()
        results = [run_one(chat, it) for it in items]
        out = RAW / f"e1_synth_{slug}.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        ok = sum(r["correct"] for r in results)
        drift = sum(
            1 for r in results if r["correct"] != bool(old[slug].get(r["id"], {}).get("correct"))
        )
        lat = sorted(r["ms"] for r in results)
        prev = sum(1 for i in (it["id"] for it in items) if old[slug].get(i, {}).get("correct"))
        print(
            f"  {slug:8} correct {ok:2}/{len(items)} ({ok / len(items) * 100:4.1f}%)  "
            f"[기존 라벨 {prev}/{len(items)}]  드리프트 {drift}문항  "
            f"p50 {lat[len(lat) // 2]}ms  {int(time.monotonic() - t0)}s"
        )
    print(f"\n원자료: {RAW}/e1_synth_*.jsonl → 평가는 e1_synth_eval.py")


if __name__ == "__main__":
    main()
