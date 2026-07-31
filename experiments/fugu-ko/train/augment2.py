"""D6 Phase C — 패러프레이즈 증강 (base 1,361 → ~3,000). gpt-4o-mini(워커풀 밖=중립).

핵심 안전장치: DB명은 spec에서 가져와 `{db}` placeholder로 치환한 뒤 패러프레이즈 → 되돌린다.
이모지/후행공백 DB명(hard-case)이 패러프레이즈에서 훼손되면 gold(spec 기준)와 어긋나므로, DB명은
모델에게 절대 안 넘긴다. 라벨은 여전히 spec 결정론 → 패러프레이즈가 라벨을 훼손하지 않는다.

패러프레이즈 모델을 워커(solar 등)로 쓰면 그 워커가 쉬운 표현으로 바꿔 라벨을 편향시킬 수 있어
**중립 baseline(gpt-4o-mini, 국내 워커 아님)** 을 쓴다.

실행(node.env + FUGU_KEYS): PYTHONPATH=... python train/augment2.py --target 3000
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
DATA = HERE / "data"
GOLDEN = ROOT / "golden"

from gen2 import golden_norms, norm  # noqa: E402
from pool import build_pool  # noqa: E402

SYS = ("너는 한국어 질문을 자연스럽게 다르게 바꾸는 도구다. 규칙: (1) 의미(집계/개수/필터/그룹) "
       "완전 보존 (2) `{db}` 토큰은 글자 그대로 유지, 절대 바꾸거나 번역·삭제 금지 (3) 필드명·값·"
       "숫자 조건 유지 (4) 한 줄, 따옴표 없이. JSON 배열로만 답한다: [\"변형1\",\"변형2\"]")


def deslot(q: str, db: str) -> str | None:
    """q에서 db_name을 {db} placeholder로 치환. db가 q에 없으면 None(스킵)."""
    if db not in q:
        return None
    return q.replace(db, "{db}")


def reslot(tmpl: str, db: str) -> str:
    return tmpl.replace("{db}", db)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=3000)
    ap.add_argument("--per", type=int, default=2, help="문항당 요청 변형 수")
    a = ap.parse_args()

    base = [json.loads(l) for l in (DATA / "questions2.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    seen = golden_norms() | {norm(r["q"]) for r in base}
    out = list(base)
    chat = build_pool(["baseline"])["baseline"]
    need = a.target - len(base)
    print(f"[augment] base {len(base)} → target {a.target} (추가 {need}) · per={a.per}")

    made = 0
    for i, r in enumerate(base):
        if made >= need:
            break
        db = r["spec"][0]
        slotted = deslot(r["q"], db)
        if slotted is None:
            continue
        try:
            raw = chat.complete(SYS, f"질문: \"{slotted}\"\n{a.per}개 변형을 JSON 배열로:", json_only=False)
            variants = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
        except Exception:  # noqa: BLE001
            continue
        for tmpl in variants:
            if made >= need:
                break
            if "{db}" not in tmpl:
                continue
            q = reslot(tmpl.strip(), db)
            n = norm(q)
            if n in seen:
                continue
            seen.add(n)
            out.append({**r, "q": q, "id": f"g2p-{made:04d}", "src": "paraphrase"})
            made += 1
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(base)} 처리 · 생성 {made}", flush=True)

    # 재-id + 셔플 없이 저장(라벨 재개 안정성). 최종 셔플은 학습 split에서.
    for k, r in enumerate(out):
        r["id"] = f"g2-{k:04d}"
    (DATA / "questions2_aug.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n", encoding="utf-8")
    from collections import Counter
    print(f"[augment] 최종 {len(out)}문항 (base {len(base)} + 패러 {made}) → data/questions2_aug.jsonl")
    print(f"  tag: {dict(Counter(r['tag'] for r in out))} · src: {dict(Counter(r.get('src','base') for r in out))}")


if __name__ == "__main__":
    main()
