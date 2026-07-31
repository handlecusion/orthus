"""distill 환각 판정 — **원문을 읽은 판정자**가 클레임 하나하나를 검증한다.

`t11_distill.py`의 토큰-중복 휴리스틱은 **실패한 지표**였다. 걸린 것들을 열어 보니 환각이 아니라
(1) 근거를 인용이 아니라 포인터로 쓴 경우("문서 첫 섹션에서 명시됨"), (2) 원문이 한국어인데
클레임을 영어로 쓴 경우였다. 둘 다 토큰이 안 겹칠 뿐 내용은 맞다. 그 지표는 "베꼈나"를 쟀지
"사실인가"를 재지 않았다 — 그대로 썼다면 번역체 모델에 부당한 벌점을 줬을 것이다.

그래서 여기서는 판정자에게 **원문 전체와 클레임을 함께** 주고 한 가지만 묻는다:

    이 클레임이 원문에서 **뒷받침되는가?**  supported / unsupported / contradicted

`contradicted`(원문과 반대로 말함)를 별도 라벨로 둔다 — 이게 위키 오염의 최악 형태다.
판정자는 gpt-4o. **GPT는 프로덕션 모델로는 못 쓰지만 측정 도구로는 쓸 수 있다**(시스템 의존성이
아니라 자[尺]다). 국내 판정자를 하나 더 붙여 2인 다수결로 만들 수도 있으나, 여기 판정은
"원문에 있나 없나"라는 사실 확인이라 wiki_qa 같은 취향 편향이 없다 → 단일 판정자 + 샘플링.

    python t11_judge.py                 # 모델당 40클레임 표집
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

RAW = HERE / "analysis" / "raw"
N_SAMPLE = 40
SEED = 20260714  # 표집 고정 — 모델 간 비교가 표본 운에 흔들리지 않게

_SYS = (
    "너는 사실 검증자다. [원문]과 그 원문에서 추출됐다고 주장하는 [클레임]이 주어진다. "
    "클레임이 원문에서 뒷받침되는지만 판단한다.\n"
    "- supported: 원문이 그 내용을 (그대로든 의역이든, 어떤 언어로든) 담고 있다.\n"
    "- unsupported: 원문에 그런 내용이 없다. 지어냈다.\n"
    "- contradicted: 원문이 반대로 말한다.\n"
    "표현이 바뀌었거나 다른 언어로 번역된 것은 **supported다** — 내용만 본다.\n"
    '출력은 JSON 하나: {"verdict": "supported"|"unsupported"|"contradicted", "why": "<한 문장>"}'
)


def _docs() -> dict[str, str]:
    import psycopg

    dsn = os.environ.get("FUGU_DSN", "postgresql://orthus:orthus@localhost:5433/orthus_company")
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute(
            "SELECT doc_id, markdown FROM documents WHERE length(markdown) > 600 "
            "ORDER BY length(markdown) DESC LIMIT 25"
        )
        return {str(a): b for a, b in cur.fetchall()}


def _judge():
    from orthus.models.adapters.openai_compat import OpenAIChat
    from orthus.settings import get_settings

    s = get_settings()
    return OpenAIChat(s.llm_base_url, s.llm_api_key, "gpt-4o", 60.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="solar,exaone,baseline")
    ap.add_argument("--n", type=int, default=N_SAMPLE)
    a = ap.parse_args()

    docs = _docs()
    judge = _judge()
    out = (RAW / "t11_judge.jsonl").open("w", encoding="utf-8")
    print("\n  ══ distill 환각 판정 (원문을 읽은 판정자 · gpt-4o) ══")
    print("  ⚠ 'contradicted'가 최악이다 — 위키에 원문과 반대되는 사실이 박힌다.\n")

    for m in [x.strip() for x in a.models.split(",") if x.strip()]:
        p = RAW / f"t11_{m}.jsonl"
        if not p.exists():
            continue
        pool = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if not r.get("ok"):
                continue
            body = docs.get(r["doc_id"], "")
            for c in r["claims"]:
                pool.append((r["doc_id"], body, c["claim"]))
        rnd = random.Random(SEED)
        sample = rnd.sample(pool, min(a.n, len(pool)))
        tally: Counter = Counter()
        for doc_id, body, claim in sample:
            user = f"[원문]\n{body[:6000]}\n\n[클레임]\n{claim}"
            try:
                raw = judge.complete(_SYS, user, json_only=True)
                v = json.loads(raw).get("verdict", "?")
                why = json.loads(raw).get("why", "")
            except Exception as e:  # noqa: BLE001
                v, why = "?", f"{type(e).__name__}"
            tally[v] += 1
            out.write(
                json.dumps(
                    {"model": m, "doc_id": doc_id, "claim": claim, "verdict": v, "why": why},
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()
        n = sum(tally.values())
        sup = tally["supported"]
        bad = tally["unsupported"] + tally["contradicted"]
        print(
            f"  {m:9} 표본 {n:3} (전체 {len(pool)}) | 뒷받침됨 {sup:3} (**{sup / n * 100:5.1f}%**) "
            f"| 지어냄 {tally['unsupported']:2} | **원문과 모순 {tally['contradicted']:2}** "
            f"| 오염률 {bad / n * 100:4.1f}%"
        )
    out.close()


if __name__ == "__main__":
    main()
