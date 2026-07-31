"""D6 Phase D — 라벨링 (questions2_aug → 워커별 정오 벡터). label.py 계승 + baseline 포함.

각 문항을 orthus 실파이프라인(query_structured, chat_model 주입)으로 실행 → gold(spec 결정론)
number-set 채점. 재개 지원(labels2_<slug>.jsonl append). structured 단일 타깃(라우팅 제외).

DSN은 반드시 0706 3종 override(T11 교훈: readonly 미지정 시 다른 DB → 전부 0건).
baseline(gpt-4o-mini)도 라벨 — (b)/(c) 정직 대조용. mock 가드: 첫 응답 눈으로 확인.

실행(node.env + FUGU_KEYS + DSN override):
  python train/label2.py --models solar,exaone     # 빠른 둘 병렬
  python train/label2.py --models ax                # A.X 스로틀 단독(야간)
  python train/label2.py --models baseline          # 기준선
  python train/label2.py --merge                    # 병합 + 불일치 게이트
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
sys.path.insert(0, str(ROOT.parent.parent))

from pool import build_pool  # noqa: E402
from t3_gold import model_numbers  # noqa: E402
from orthus.structured.query import query_structured  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
DATA = HERE / "data"
QFILE = DATA / "questions2_aug.jsonl"
WORKERS = ["solar", "ax", "exaone"]          # 국내 워커(학습 타깃)
ALL = WORKERS + ["baseline"]                 # baseline은 대조만


def label_one(chat, item: dict) -> dict:
    t0 = time.monotonic()
    out = {"id": item["id"], "tag": item.get("tag"), "hard": item.get("hard")}
    try:
        r = query_structured(UID, item["q"], scope="company", chat_model=chat)
        g = set(item["gold"]) if "gold" in item else set()
        # gold 없으면(base엔 gold 미포함) 여기서 계산 안 함 — questions2_aug은 gold 포함 가정
        out["correct"] = bool(r.status == "executed" and g and g <= model_numbers(r.rows))
        out["status"] = r.status
    except Exception as e:  # noqa: BLE001
        out["correct"] = False
        out["error"] = f"{type(e).__name__}: {str(e)[:100]}"
    out["ms"] = int((time.monotonic() - t0) * 1000)
    return out


def run_models(models: list[str], limit: int) -> None:
    items = [json.loads(l) for l in QFILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    if limit:
        items = items[:limit]
    pool = build_pool(models)
    for slug, chat in pool.items():
        out_path = DATA / f"labels2_{slug}.jsonl"
        done: set[str] = set()
        if out_path.exists():
            done = {json.loads(l)["id"] for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()}
        todo = [it for it in items if it["id"] not in done]
        print(f"[{slug}] 전체 {len(items)} / 완료 {len(done)} / 남음 {len(todo)}", flush=True)
        t0 = time.monotonic()
        with out_path.open("a", encoding="utf-8") as f:
            for n, it in enumerate(todo, 1):
                r = label_one(chat, it)
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                if n <= 2:
                    print(f"  [{slug}] 샘플 {it['id']} correct={r.get('correct')} status={r.get('status')}", flush=True)
                if n % 100 == 0:
                    el = time.monotonic() - t0
                    print(f"  [{slug}] {n}/{len(todo)} ({el/n:.1f}s/문항, {el/60:.0f}분)", flush=True)
        rows = [json.loads(l) for l in out_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        ok = sum(1 for r in rows if r.get("correct"))
        print(f"[{slug}] 완료 — 정답 {ok}/{len(rows)} ({ok/len(rows)*100:.0f}%)", flush=True)


def merge() -> None:
    items = {json.loads(l)["id"]: json.loads(l)
             for l in QFILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    labels: dict[str, dict] = {}
    for m in ALL:
        p = DATA / f"labels2_{m}.jsonl"
        if not p.exists():
            print(f"  [merge] labels2_{m}.jsonl 없음")
            if m in WORKERS:
                return
            continue
        labels[m] = {json.loads(l)["id"]: json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()}
    common = [i for i in items if all(i in labels[m] for m in WORKERS)]
    rows = []
    for i in common:
        it = items[i]
        vec = {f"correct_{m}": bool(labels[m][i]["correct"]) for m in WORKERS}
        if "baseline" in labels and i in labels["baseline"]:
            vec["correct_baseline"] = bool(labels["baseline"][i]["correct"])
        rows.append({"id": i, "q": it["q"], "tag": it.get("tag"), "hard": it.get("hard"),
                     "spec": it.get("spec"), "gold": it.get("gold"), **vec})
    (DATA / "labeled2.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    n = len(rows)
    dis = [r for r in rows if len({r[f"correct_{m}"] for m in WORKERS}) > 1]
    aw = sum(1 for r in rows if not any(r[f"correct_{m}"] for m in WORKERS))
    ar = sum(1 for r in rows if all(r[f"correct_{m}"] for m in WORKERS))
    print(f"[merge] 공통 {n}문항 → labeled2.jsonl")
    print("  정답률: " + "  ".join(f"{m} {sum(r[f'correct_{m}'] for r in rows)/n*100:.0f}%" for m in WORKERS)
          + ("  | baseline %.0f%%" % (sum(r.get('correct_baseline', False) for r in rows)/n*100)
             if 'correct_baseline' in (rows[0] if rows else {}) else ""))
    print(f"  전원정답 {ar} / 전원오답 {aw} / **불일치 {len(dis)} ({len(dis)/n*100:.0f}%)**")
    print(f"  → G1 게이트(≥15% & ≥500건): {'PASS' if len(dis)/n>=0.15 and len(dis)>=500 else 'CHECK'} (불일치 {len(dis)})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="solar,exaone")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--merge", action="store_true")
    a = ap.parse_args()
    if a.merge:
        merge()
    else:
        run_models(a.models.split(","), a.limit)


if __name__ == "__main__":
    main()
