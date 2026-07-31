"""D6 ambig 승리 재현(C) — FRESH 모호질문 생성 + 워커 라벨링.

원 승리(by_task=ambig, +3.6p p=0.031)가 '한 번 운/짜맞춤'인지 확인하기 위한 **사전선언 단일 재현**:
학습 데이터/모델 레시피는 그대로 두고, **T_AMBIG에 없는 새 표현 8종**으로 만든 완전히 새 모호질문에
같은 레시피 모델을 시험한다. 여기서도 이기면 진짜, 아니면 원래 건 운.

- 대상 DB = 학습 DB(HELDOUT 제외) — 원 승리와 같은 조건(task 이동만, DB 이동 없음).
- gold = db 총개수(ambig spec = (db,count,None,None)). 기존 3000 q-norm과 dedup → 겹침 0 보장.
- 라벨 = solar/ax/exaone (build_pool + label_one 재사용). baseline 제외.
출력: data/fresh_ambig.jsonl (id/q/tag/gold/correct_solar/ax/exaone).
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent.parent))

import gen2 as G  # noqa: E402  (extract_schema/josa/norm/is_hard)
from t3_gold import gold_numbers  # noqa: E402
from pool import build_pool  # noqa: E402
from orthus.structured.query import query_structured  # noqa: E402
from t3_gold import model_numbers  # noqa: E402
from uuid import UUID  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
DATA = HERE / "data"
WORKERS = ["solar", "ax", "exaone"]

# T_AMBIG(gen2)에 전혀 없는 새 표현 — held-out '표현' 세트
T_AMBIG_FRESH = [
    "{db} 말인데, {f} 그런 거 따지지 말고 그냥 전체가 몇 개인지 알려줄래?",
    "{f} 이런 걸로 쪼개지 말고 {db} 다 합친 숫자 하나만 줘",
    "{db} 개수 궁금한데 {f}[는] 신경 안 써도 되고 총량만",
    "{f} 항목이 뭐든 간에 {db}[가] 몇 개 있는지 통틀어서 세줘",
    "{db} 전체 몇 건이야? {f}별로 나눌 필요는 없어",
    "그냥 {db} 총 몇 개인지만, {f} 구분은 빼고",
    "{db}에 {f} 있는데 그거 말고 전부 몇 개인지 총합으로",
    "{f} 기준 분류는 됐고 {db} 전체 카운트만 알려줘",
]


def existing_norms() -> set[str]:
    rows = [json.loads(l) for l in (DATA / "labeled2.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return {G.norm(r["q"]) for r in rows}


def main() -> None:
    schema = G.extract_schema(min_rows=10, exclude=G.HELDOUT_DBS)
    print(f"[schema] 학습 DB {len(schema)}개")
    rng = random.Random(20260716)  # 새 seed
    seen = existing_norms()
    print(f"[dedup] 기존 3000 q-norm {len(seen)}개 기준 중복 제거")
    raw = []
    for db, sch in schema.items():
        for f, _vals in sch["gb"].items():
            for t in rng.sample(T_AMBIG_FRESH, k=min(4, len(T_AMBIG_FRESH))):
                q = G.josa(t.format(db=db, f=f))
                n = G.norm(q)
                if n in seen:
                    continue
                seen.add(n)
                raw.append({"q": q, "spec": [db, "count", None, None], "tag": "ambig", "hard": G.is_hard(db)})
    rng.shuffle(raw)
    # gold 계산(db 총개수) + 유효만
    items, cache = [], {}
    for r in raw:
        db = r["spec"][0]
        if db not in cache:
            try:
                g = gold_numbers(db, "count", None, None)
                cache[db] = sorted(g) if g and g != {0} else None
            except Exception:  # noqa: BLE001
                cache[db] = None
        if cache[db]:
            r["gold"] = cache[db]
            r["id"] = f"fresh-{len(items):04d}"
            items.append(r)
        if len(items) >= 180:
            break
    print(f"[gen] fresh ambig {len(items)}개 (gold 유효)")

    # 라벨링 — solar/ax/exaone 순차
    pool = build_pool(WORKERS)
    out_path = DATA / "fresh_ambig.jsonl"
    labeled = {it["id"]: dict(it) for it in items}
    for slug, chat in pool.items():
        t0 = time.monotonic()
        ok = 0
        for n, it in enumerate(items, 1):
            try:
                r = query_structured(UID, it["q"], scope="company", chat_model=chat)
                g = set(it["gold"])
                c = bool(r.status == "executed" and g and g <= model_numbers(r.rows))
            except Exception as e:  # noqa: BLE001
                c = False
                if n <= 2:
                    print(f"  [{slug}] err {type(e).__name__}: {str(e)[:80]}", flush=True)
            labeled[it["id"]][f"correct_{slug}"] = c
            ok += int(c)
            if n <= 2:
                print(f"  [{slug}] 샘플 {it['id']} correct={c}", flush=True)
            if n % 50 == 0:
                print(f"  [{slug}] {n}/{len(items)} ({(time.monotonic()-t0)/n:.1f}s/문항)", flush=True)
        print(f"[{slug}] 정답 {ok}/{len(items)} ({ok/len(items)*100:.0f}%)", flush=True)

    with out_path.open("w", encoding="utf-8") as f:
        for it in labeled.values():
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    # 요약
    b = sum(labeled[i]["correct_solar"] for i in labeled) / len(labeled) * 100
    o = sum(1 for i in labeled if any(labeled[i][f"correct_{m}"] for m in WORKERS)) / len(labeled) * 100
    dis = sum(1 for i in labeled if len({labeled[i][f"correct_{m}"] for m in WORKERS}) > 1)
    print(f"\n[요약] fresh ambig {len(labeled)}개 · static(solar) {b:.1f} · oracle {o:.1f} · headroom {o-b:+.1f}p · 불일치 {dis}")
    print(f"저장: {out_path}")


if __name__ == "__main__":
    main()
