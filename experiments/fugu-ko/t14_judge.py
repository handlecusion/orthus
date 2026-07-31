"""T14 정밀도 판정 — **원문 전체를 읽은 판정자**가 클레임 하나하나를 검증한다.

`t11_judge.py`를 그대로 쓰지 않는 이유는 그 스크립트가 T13에서 **조용히 틀린 답을 냈기**
때문이다. 두 결함:

1. `body = docs.get(doc_id, "")` — 원문을 못 찾으면 **빈 문자열로 판정을 진행**했다. 판정자는
   빈 원문을 받고 전부 "지어냄"이라 답한다. 실패가 **에러가 아니라 그럴듯한 숫자**로 나온다.
2. `body[:6000]` — 원문을 6천 자로 자른 뒤 "이 클레임이 원문에 있나"를 물었다. 문서가 그보다
   길면 뒤에서 뽑힌 클레임은 **구조적으로 "지어냄"** 이 된다.

여기서 둘 다 구조적으로 막는다:
- 원문은 **얼린 docset**(`t14_docset.json`)에서 읽는다. 없으면 **죽는다**(빈 문자열로 넘어가지
  않는다).
- 창은 16,000자. company 문서 최대가 7,473자라 **자를 일이 없다** — 그리고 자르게 되면 죽는다.

판정 프로토콜(질문·라벨·모델)은 `t11_judge.py`와 **동일**하게 유지한다 — 바꾸는 건 원문을
제대로 주는 것뿐이다(변인 1개).

판정자는 gpt-4o. **GPT는 프로덕션 모델로는 금지지만 측정 도구로는 쓸 수 있다** — 시스템
의존성이 아니라 자[尺]다.

    python t14_judge.py                      # arm별 40클레임 표집
    python t14_judge.py --n 60
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
DOCSET = RAW / "t14_docset.json"
WINDOW = 16_000
N_SAMPLE = 40
SEED = 20260714  # 표집 고정 — arm 간 비교가 표본 운에 흔들리지 않게

ARMS_MODELS = [("P0", "solar"), ("P0", "baseline"), ("P1", "solar"), ("P1", "baseline")]

_SYS = (
    "너는 사실 검증자다. [원문]과 그 원문에서 추출됐다고 주장하는 [클레임]이 주어진다. "
    "클레임이 원문에서 뒷받침되는지만 판단한다.\n"
    "- supported: 원문이 그 내용을 뒷받침한다. **표현이 바뀌었거나 다른 언어로 번역된 것도 "
    "supported다 — 내용만 본다.**\n"
    "- unsupported: 원문에 근거가 없다(지어냈다).\n"
    "- contradicted: 원문과 반대로 말한다.\n"
    '출력은 JSON 하나: {"verdict": "supported"|"unsupported"|"contradicted", "why": "<한 문장>"}'
)


def _docs() -> dict[str, str]:
    assert DOCSET.exists(), f"{DOCSET} 없음 — t14_distill_cap.py를 먼저 돌려 docset을 얼려라"
    docs = json.loads(DOCSET.read_text(encoding="utf-8"))
    return {d["doc_id"]: d["markdown"] for d in docs}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=N_SAMPLE)
    a = ap.parse_args()

    from orthus.models.adapters.openai_compat import OpenAIChat

    judge = OpenAIChat(os.environ["ORTHUS_LLM_BASE_URL"], os.environ["ORTHUS_LLM_API_KEY"], "gpt-4o")
    docs = _docs()
    out = (RAW / "t14_judge.jsonl").open("w", encoding="utf-8")

    print("\n  ══ T14 distill 정밀도 (원문 **전체**를 읽은 판정자 · gpt-4o) ══")
    print("  ⚠ 'contradicted'가 최악이다 — 위키에 원문과 반대되는 사실이 박힌다.\n")

    rows_out = []
    for arm, model in ARMS_MODELS:
        p = RAW / f"t14_{arm}_{model}.jsonl"
        if not p.exists():
            continue
        pool = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if not r.get("ok"):
                continue
            body = docs.get(r["doc_id"])
            # T13의 결함 1: 없으면 빈 문자열로 넘어갔다. 여기서는 죽는다.
            assert body, f"원문 없음 — docset과 결과 파일이 어긋났다: {r['doc_id']}"
            assert len(body) <= WINDOW, f"원문이 판정 창을 넘는다({len(body)}자) — 창을 올려라"
            for c in r["claims"]:
                pool.append((body, c["claim"]))

        rnd = random.Random(SEED)
        sample = rnd.sample(pool, min(a.n, len(pool)))
        cnt: Counter = Counter()
        for body, claim in sample:
            user = f"[원문]\n{body}\n\n[클레임]\n{claim}"
            try:
                v = json.loads(judge.complete(_SYS, user, json_only=True)).get("verdict", "?")
            except Exception:  # noqa: BLE001
                v = "?"
            cnt[v] += 1
            out.write(
                json.dumps({"arm": arm, "model": model, "claim": claim, "verdict": v}, ensure_ascii=False) + "\n"
            )
            out.flush()

        n = sum(cnt.values())
        sup = cnt["supported"] / n * 100 if n else 0
        rows_out.append((arm, model, n, len(pool), cnt, sup))
        print(
            f"  {arm} {model:9} 표본 {n:3} (전체 {len(pool):3}) | "
            f"뒷받침됨 {cnt['supported']:3} (**{sup:5.1f}%**) | "
            f"지어냄 {cnt['unsupported']:3} | **원문과 모순 {cnt['contradicted']:2}**"
        )

    # 게이트 ② — P1 solar의 정밀도가 P0 solar 대비 5%p 초과로 떨어지면 채택 불가
    got = {(x[0], x[1]): x[5] for x in rows_out}
    if ("P0", "solar") in got and ("P1", "solar") in got:
        d = got[("P1", "solar")] - got[("P0", "solar")]
        print(f"\n  ── 게이트 ② 정밀도: solar {got[('P0', 'solar')]:.1f}% → {got[('P1', 'solar')]:.1f}% ({d:+.1f}%p)")
        print(f"     {'PASS — 커버리지를 오염으로 사지 않았다' if d > -5 else 'FAIL — 커버리지의 대가가 오염이다'}")
    out.close()


if __name__ == "__main__":
    main()
