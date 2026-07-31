"""G1b — 학습 데이터 라벨러 (D5 §2). 3워커 실행 + 결정론 채점(LLM 판단 0회).

questions.jsonl의 각 문항을 워커별로 orthus 실파이프라인에 태우고:
- structured: `query_structured` 실행 → 스펙 gold number-set ⊆ 결과 number-set (t3_gold 방식)
- routing: `classify` → expected_route 일치
결과를 워커별 labels_<slug>.jsonl에 append. **재개 지원** — 이미 라벨된 id는 스킵
(A.X RPS3 스로틀로 장시간 실행 대비, 중단 후 같은 명령 재실행).

완료 후 `--merge`로 문항별 정답 벡터 labeled.jsonl 생성 + 불일치율 게이트(G1) 보고.

실행: (company env, FUGU_KEYS 로드 후)
  python experiments/fugu-ko/train/label.py --models solar,exaone      # 빠른 둘 먼저
  python experiments/fugu-ko/train/label.py --models ax                # 스로틀 병목 별도
  python experiments/fugu-ko/train/label.py --merge                    # 병합 + 불일치율
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
sys.path.insert(0, str(ROOT))

from pool import build_pool  # noqa: E402
from t3_gold import gold_numbers, model_numbers  # noqa: E402
from orthus.router.route import classify  # noqa: E402
from orthus.structured.query import query_structured  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
DATA = HERE / "data"
WORKERS = ["solar", "ax", "exaone"]

_gold_cache: dict[tuple, set[int]] = {}


def gold_for(spec: list) -> set[int]:
    key = tuple(spec)
    if key not in _gold_cache:
        _gold_cache[key] = gold_numbers(*spec)
    return _gold_cache[key]


def label_one(chat, item: dict) -> dict:
    t0 = time.monotonic()
    out = {"id": item["id"], "task": item["task"], "tag": item.get("tag")}
    try:
        if item["task"] == "structured":
            r = query_structured(UID, item["q"], scope="company", chat_model=chat)
            g = gold_for(item["spec"])
            out["correct"] = bool(
                r.status == "executed" and g and g <= model_numbers(r.rows)
            )
            out["status"] = r.status
        else:  # routing
            route = classify(item["q"], chat_model=chat)
            out["correct"] = route == item["expected_route"]
            out["route"] = route
    except Exception as e:  # noqa: BLE001
        out["correct"] = False
        out["error"] = f"{type(e).__name__}: {str(e)[:100]}"
    out["ms"] = int((time.monotonic() - t0) * 1000)
    return out


def run_models(models: list[str], limit: int) -> None:
    items = [json.loads(l) for l in open(DATA / "questions.jsonl", encoding="utf-8")]
    if limit:
        items = items[:limit]
    pool = build_pool(models)
    for slug, chat in pool.items():
        out_path = DATA / f"labels_{slug}.jsonl"
        done: set[str] = set()
        if out_path.exists():  # 재개: 기존 라벨 스킵
            done = {json.loads(l)["id"] for l in open(out_path, encoding="utf-8") if l.strip()}
        todo = [it for it in items if it["id"] not in done]
        print(f"[{slug}] 전체 {len(items)} / 완료 {len(done)} / 남음 {len(todo)}")
        t0 = time.monotonic()
        with open(out_path, "a", encoding="utf-8") as f:
            for n, it in enumerate(todo, 1):
                r = label_one(chat, it)
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                if n % 25 == 0:
                    el = time.monotonic() - t0
                    print(f"  [{slug}] {n}/{len(todo)}  ({el/n:.1f}s/문항, 경과 {el/60:.0f}분)")
        ok = sum(1 for l in open(out_path, encoding="utf-8")
                 if json.loads(l).get("correct"))
        total = sum(1 for _ in open(out_path, encoding="utf-8"))
        print(f"[{slug}] 완료 — 정답 {ok}/{total} ({ok/total*100:.0f}%)")


def merge() -> None:
    items = {json.loads(l)["id"]: json.loads(l)
             for l in open(DATA / "questions.jsonl", encoding="utf-8")}
    labels: dict[str, dict[str, dict]] = {}
    for m in WORKERS:
        p = DATA / f"labels_{m}.jsonl"
        if not p.exists():
            print(f"  [merge] labels_{m}.jsonl 없음 — 먼저 라벨 실행")
            return
        labels[m] = {json.loads(l)["id"]: json.loads(l) for l in open(p, encoding="utf-8")}

    common = [i for i in items if all(i in labels[m] for m in WORKERS)]
    rows = []
    for i in common:
        it = items[i]
        vec = {f"correct_{m}": bool(labels[m][i]["correct"]) for m in WORKERS}
        rows.append({"id": i, "task": it["task"], "tag": it.get("tag"), "q": it["q"], **vec})

    out = DATA / "labeled.jsonl"
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # G1 게이트: 불일치율 (모델 간 정오가 갈린 문항)
    n = len(rows)
    dis = [r for r in rows if len({r[f"correct_{m}"] for m in WORKERS}) > 1]
    all_wrong = sum(1 for r in rows if not any(r[f"correct_{m}"] for m in WORKERS))
    all_right = sum(1 for r in rows if all(r[f"correct_{m}"] for m in WORKERS))
    print(f"[merge] 3워커 공통 라벨 {n}문항 → {out}")
    print(f"  모델별 정답률: " + "  ".join(
        f"{m} {sum(r[f'correct_{m}'] for r in rows)/n*100:.0f}%" for m in WORKERS))
    print(f"  전원 정답 {all_right} / 전원 오답 {all_wrong} / **불일치 {len(dis)} ({len(dis)/n*100:.0f}%)**")
    print(f"  → G1 게이트(≥10%): {'PASS' if len(dis)/n >= 0.10 else 'FAIL — hard-type 재표집 필요'}")
    tag_dis: dict[str, list[int]] = {}
    for r in rows:
        d = tag_dis.setdefault(r["tag"], [0, 0])
        d[1] += 1
        if len({r[f"correct_{m}"] for m in WORKERS}) > 1:
            d[0] += 1
    print("  유형별 불일치:", {t: f"{a}/{b}" for t, (a, b) in sorted(tag_dis.items())})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="solar,exaone")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--merge", action="store_true")
    args = ap.parse_args()
    if args.merge:
        merge()
    else:
        run_models(args.models.split(","), args.limit)


if __name__ == "__main__":
    main()
