"""D7 — 평가용 프롬프트 생성 (SFT 워커 --gen 입력).

동결 홀드아웃 463문항을 배포와 동일한 프롬프트(전체 실카탈로그, 증강 없음)로 렌더한다.
카탈로그는 격리 DB 전체 db_name 포함 — 배포 시 새 DB가 카탈로그에 실리는 것과 동일 조건.

⚠️ 이 홀드아웃 채점은 prereg §5에 따라 **참고(cross-DB 전이 프리뷰) 전용**이다.
승리 선언은 fresh 판정셋(§2)에서만 한다.

실행: python train/build_eval_prompts.py   # → data/sft_holdout_prompts.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent.parent))

from build_sft_data import load_db_keys, render_user  # noqa: E402
from orthus.assistant.compile import _SYSTEM  # noqa: E402

DATA = HERE / "data"
GOLDEN = ROOT / "golden"


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--items", default="golden/t3_unseen_holdout.json")
    ap.add_argument("--out", default="data/sft_holdout_prompts.jsonl")
    a = ap.parse_args()
    db_keys = load_db_keys()  # 전체 카탈로그 (fresh/합성 DB 포함 — 배포 동일)
    items = json.loads((ROOT / a.items).read_text(encoding="utf-8"))["items"]
    system = _SYSTEM.format(dialect="postgres")
    out = [
        {
            "id": it["id"],
            "tag": it["tag"],
            "gold": it["gold"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": render_user(it["q"], db_keys)},
            ],
        }
        for it in items
    ]
    p = ROOT / "train" / a.out if not a.out.startswith("/") else Path(a.out)
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in out) + "\n", encoding="utf-8")
    print(f"[eval-prompts] {len(out)}건 → {p}")


if __name__ == "__main__":
    main()
