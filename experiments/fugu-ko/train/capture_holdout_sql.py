"""D7 — 주판정 holdout 463에 워커별 (SQL + 실행결과) 캡처.

기존 `analysis/raw/holdout_worker_answers.json`은 (status, nums)만 있고 **SQL이 없다**.
D7의 신호는 (질문 의도 ↔ SQL 모양) 정합성이므로 SQL이 필수다. 같은 홀드아웃 문항을
워커별로 다시 돌려 SQL까지 캡처한다.

⚠️ 무결성 게이트: 재실행 solar 정답률이 동결 GT(61.1%)와 크게 다르면(±5%p 초과) 캡처가
오염된 것이니 결과를 쓰지 말 것(D6 §4-2 교훈).

⚠️ 오염 주의: 이 캡처는 **신호 개발용이 아니라 검증용**이다. 신호/임계값은 학습셋
(`sc_pilot.py`)에서 고정한 뒤 여기서 한 번만 확인한다. 최종 승리 선언은 신규 held-out DB
에서 별도 판정한다(`analysis/d7-execution-guide.md` §4.6).

실행(로컬, node.env + FUGU_KEYS + 0706 DSN 3종):
  python train/capture_holdout_sql.py --models solar,ax,exaone
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
from t3_gold import model_numbers  # noqa: E402
from orthus.structured.query import query_structured  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
GOLDEN = ROOT / "golden"
RAW = ROOT / "analysis" / "raw"
OUT = RAW / "holdout_worker_sql.jsonl"


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="solar,ax,exaone")
    a = ap.parse_args()
    models = a.models.split(",")

    items = json.loads((GOLDEN / "t3_unseen_holdout.json").read_text(encoding="utf-8"))["items"]
    done: set[tuple[str, str]] = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done.add((r["id"], r["model"]))

    with OUT.open("a", encoding="utf-8") as f:
        for slug, chat in build_pool(models).items():
            todo = [it for it in items if (it["id"], slug) not in done]
            print(f"[{slug}] 남음 {len(todo)}/{len(items)}", flush=True)
            t0 = time.monotonic()
            for n, it in enumerate(todo, 1):
                r, err = _query_retry(chat, it["q"])
                rec = {"id": it["id"], "model": slug, "q": it["q"], "tag": it["tag"], "gold": it["gold"]}
                if r is None:
                    rec |= {"status": f"err:{err}", "sql": None, "nums": []}
                else:
                    rec |= {
                        "status": r.status,
                        "sql": r.compiled.sql if r.compiled else None,
                        "nums": sorted(model_numbers(r.rows)) if r.status == "executed" else [],
                        "reject": r.validation.rejected_reason,
                    }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                if n % 50 == 0:
                    el = time.monotonic() - t0
                    print(f"  [{slug}] {n}/{len(todo)} ({el / n:.1f}s/문항, 남은 ~{el / n * (len(todo) - n) / 60:.0f}분)", flush=True)
            print(f"[{slug}] 완료 ({(time.monotonic() - t0) / 60:.1f}분)", flush=True)

    # 무결성 게이트 — 재실행 solar 정답률 vs 동결 GT
    rows = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()]
    sol = [r for r in rows if r["model"] == "solar"]
    if sol:
        acc = sum(1 for r in sol if r["status"] == "executed" and set(r["gold"]) <= set(r["nums"])) / len(sol) * 100
        print(f"\n[무결성] 재실행 solar {acc:.1f}% vs 동결 GT 61.1% → {'OK' if abs(acc - 61.1) <= 5 else '⚠️ 캡처 오염 의심 — 쓰지 말 것'}")


if __name__ == "__main__":
    main()
