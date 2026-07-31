"""D7 — fresh 판정셋 워커 캡처 (prereg §10.3 워커 GT + §10.2 R+ 재료).

문항마다 캡처: solar 5샘플(t0 + 4×t0.7 — R+ 자기일관성 재료) + ax 1 + exaone 1.
각 샘플 = (status, sql, nums, reject). 결과는 append-jsonl(재개 지원).

실행(로컬, node.env + FUGU_KEYS + 0706 DSN 3종):
  python train/capture_fresh.py --models solar        # k=5
  python train/capture_fresh.py --models ax,exaone    # k=1씩
출력: analysis/raw/fresh_worker_sql.jsonl  (한 줄 = {id, model, sample_i, ...})
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from pool import build_pool  # noqa: E402
from sc_pilot import TempChat  # noqa: E402 — 온도 래퍼 재사용
from t3_gold import model_numbers  # noqa: E402
from orthus.structured.query import query_structured  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
GOLDEN = ROOT / "golden"
RAW = ROOT / "analysis" / "raw"
OUT = RAW / "fresh_worker_sql.jsonl"
K_SOLAR = 5


def _query_retry(chat, q: str, tries: int = 4):
    from orthus.db import get_engine, get_ro_engine

    last = None
    for a in range(tries):
        try:
            return query_structured(UID, q, scope="company", chat_model=chat), None
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
            if "Operational" not in last and "Operational" not in str(e):
                return None, f"{last}: {str(e)[:60]}"
        try:
            get_ro_engine().dispose()
            get_engine().dispose()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0 * (a + 1))
    return None, last


def run_one(chat, q: str) -> dict:
    r, err = _query_retry(chat, q)
    if r is None:
        return {"status": f"err:{err}", "sql": None, "nums": []}
    return {
        "status": r.status,
        "sql": r.compiled.sql if r.compiled else None,
        "nums": sorted(model_numbers(r.rows)) if r.status == "executed" else [],
        "reject": r.validation.rejected_reason,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="solar,ax,exaone")
    ap.add_argument("--items", default="t3_fresh_holdout.json")
    ap.add_argument("--out", default="fresh_worker_sql.jsonl")
    a = ap.parse_args()
    models = a.models.split(",")
    global OUT
    OUT = RAW / a.out

    items = json.loads((GOLDEN / a.items).read_text(encoding="utf-8"))["items"]
    done: set[tuple[str, str, int]] = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["id"], r["model"], r["sample_i"]))

    RAW.mkdir(parents=True, exist_ok=True)
    pool = build_pool(models)
    with OUT.open("a", encoding="utf-8") as f:
        for slug in models:
            base = pool[slug]
            # k=5 자기일관성 재료는 **1차 워커 후보**에게만 준다. D9(baseline=gpt-4o-mini)는
            # solar와 동일 글루로 겨루므로 baseline도 k=5다 (d9-prereg §2 "1차 워커만 교체").
            chats = (
                [TempChat(base, 0.0)] + [TempChat(base, 0.7) for _ in range(K_SOLAR - 1)]
                if slug in ("solar", "baseline")
                else [base]
            )
            todo = [(it, si) for it in items for si in range(len(chats)) if (it["id"], slug, si) not in done]
            print(f"[{slug}] 남은 캡처 {len(todo)} (k={len(chats)})", flush=True)
            t0 = time.monotonic()
            for n, (it, si) in enumerate(todo, 1):
                rec = {"id": it["id"], "model": slug, "sample_i": si, "tag": it["tag"], "gold": it["gold"]}
                rec |= run_one(chats[si], it["q"])
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                if n % 100 == 0:
                    el = time.monotonic() - t0
                    print(f"  [{slug}] {n}/{len(todo)} ({el / n:.1f}s/콜, 남은 ~{el / n * (len(todo) - n) / 60:.0f}분)", flush=True)
            print(f"[{slug}] 완료 ({(time.monotonic() - t0) / 60:.1f}분)", flush=True)

    # 요약 (t0 샘플 기준 단독 정답률 — 헤드룸 게이트 입력)
    rows = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()]
    for m in ("solar", "ax", "exaone"):
        r0 = [r for r in rows if r["model"] == m and r["sample_i"] == 0]
        if r0:
            acc = sum(1 for r in r0 if r["status"] == "executed" and set(r["gold"]) <= set(r["nums"])) / len(r0) * 100
            print(f"[요약] {m} t0 단독 {acc:.1f}% (n={len(r0)})")


if __name__ == "__main__":
    main()
