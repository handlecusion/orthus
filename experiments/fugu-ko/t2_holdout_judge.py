"""wiki_qa 홀드아웃 판정 — **근거를 본 판정자**의 국내-국내 쌍대 비교.

기존 `judge/pairwise.py`의 치명적 결함을 고친다: 판정자에게 **근거 문서를 주지 않았다.**
근거 없이 두 답변만 보면 판정자는 "단정적이고 직접적인 답"을 선호하고, 그건 **환각을 보상한다.**
실제로 이 홀드아웃에서:

    질문: "서버 인증서 갱신은 어떻게 자동화돼 있어?"
    위키에 있는 것: "예상 소요 시간: 2시간"  ← 그것뿐이다
    solar : "*어떻게* 자동화되는지는 컨텍스트에 없고, 예상 소요 시간(2시간)만 있다"  ← 정확
    exaone: "서버 인증서 갱신은 **자동화되어 있으며**, 예상 소요 시간은 2시간이다"     ← 없는 주장

근거를 보지 않는 판정자는 exaone을 고를 것이다. 그래서 이 판정자는 **검색된 wiki 페이지 원문을
함께 받고**, 근거로 뒷받침되지 않는 단정을 감점하도록 지시받는다. 동시에 "근거에 답이 있는데도
회피"하는 것 역시 실패로 명시해 — 무조건 거절하는 모델이 반사이익을 얻지 못하게 한다.

설계는 E4 PoLL을 그대로 따른다:
- **judge ∉ 판정 쌍** — 자기 출력을 판정하면 self-preference가 역방향으로 들어온다.
- **양방향(A↔B 스왑) 2회**, 두 방향이 일치할 때만 승패 인정. 불일치 = 위치에 흔들린 것 → tie.
- 국내 판정자 1 + gpt-4o 1. (GPT는 프로덕션 모델로는 못 쓰지만 **측정 도구로는 쓸 수 있다** —
  시스템 의존성이 아니다.)

    python t2_holdout_judge.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

RAW = HERE / "analysis" / "raw"
WIKI = Path(os.environ.get("FUGU_WIKI_STORE", str(Path.home() / ".orthus/nodes/company/wiki-store/company")))

# 1차(solar) 대 나머지. judge는 그 쌍에 참여하지 않는 모델 + gpt-4o.
PAIRS = [("solar", "exaone"), ("solar", "ax")]
JUDGES = {("solar", "exaone"): ["ax", "gpt4o"], ("solar", "ax"): ["exaone", "gpt4o"]}

_SYS = (
    "너는 사내 지식 비서의 답변을 평가하는 엄정한 심사관이다. "
    "[근거]에 실린 위키 원문만이 사실이다. 같은 질문에 대한 두 답변(A, B)을 비교하되, "
    "다음 순서로 판단한다.\n"
    "1. **근거로 뒷받침되지 않는 단정은 강하게 감점한다.** 근거에 없는 사실을 지어내거나, "
    "근거가 '예정/과제'인 것을 '이미 완료됨'처럼 단정하면 그 답변은 진다.\n"
    "2. **근거에 답이 있는데도 회피하면 감점한다.** 무조건 '정보 없음'이라 하는 것은 정답이 아니다.\n"
    "3. 근거에 질문의 답이 **없을 때**는, 없다고 밝히면서 근거에 실제로 있는 관련 내용을 "
    "짚어 주는 답변이 우수하다. 질문과 무관한 사실만 던지는 답변은 열등하다.\n"
    "4. 장황함은 우수함이 아니다.\n"
    '출력은 JSON 하나: {"winner": "A"|"B"|"tie", "reason": "<한 문장>"}.'
)


def _prompt(q: str, srcs: str, a: str, b: str) -> str:
    return f"[질문]\n{q}\n\n[근거 — 위키 원문]\n{srcs}\n\n[답변 A]\n{a}\n\n[답변 B]\n{b}\n\n어느 답변이 더 나은가?"


def _page_body(slug: str) -> str:
    for f in WIKI.rglob(f"{slug}.md"):
        t = f.read_text(encoding="utf-8", errors="ignore")
        parts = t.split("\n---\n", 1)
        body = parts[1] if len(parts) > 1 else t
        return " ".join(body.split())[:400]
    return ""


def _load(m: str) -> dict[str, dict]:
    p = RAW / f"t2h_{m}.jsonl"
    return {json.loads(x)["id"]: json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()}


def _judge_model(name: str):
    from pool import build_pool

    if name == "gpt4o":
        from orthus.models.adapters.openai_compat import OpenAIChat
        from orthus.settings import get_settings

        s = get_settings()
        return OpenAIChat(s.llm_base_url, s.llm_api_key, "gpt-4o")
    return build_pool([name])[name]


def _vote(judge, q, srcs, a, b) -> str:
    try:
        raw = judge.complete(_SYS, _prompt(q, srcs, a, b), json_only=True)
        w = json.loads(raw).get("winner")
        return w if w in ("A", "B", "tie") else "tie"
    except Exception:  # noqa: BLE001
        return "tie"


def main() -> None:
    items = {i["id"]: i["q"] for i in json.loads((HERE / "golden" / "t2_holdout.json").read_text(encoding="utf-8"))["items"]}
    runs = {m: _load(m) for m in ("solar", "ax", "exaone")}
    judges = {n: _judge_model(n) for n in {"ax", "exaone", "gpt4o"}}
    out = (RAW / "t2h_judge.jsonl").open("w", encoding="utf-8")

    for left, right in PAIRS:
        tally: Counter = Counter()
        for qid, q in items.items():
            srcs = "\n".join(
                f"[{n}] {_page_body(s)}" for n, s in enumerate(runs[left][qid].get("sources", [])[:3], 1)
            )
            a, b = runs[left][qid]["answer"] or "", runs[right][qid]["answer"] or ""
            verdicts = []
            for jn in JUDGES[(left, right)]:
                j = judges[jn]
                fwd = _vote(j, q, srcs, a, b)  # A=left
                rev = _vote(j, q, srcs, b, a)  # A=right  → left 승 = "B"
                if fwd == "A" and rev == "B":
                    v = left
                elif fwd == "B" and rev == "A":
                    v = right
                else:
                    v = "tie"  # 방향에 흔들림 → 무효
                verdicts.append(v)
                out.write(json.dumps({"pair": f"{left}v{right}", "judge": jn, "id": qid,
                                      "fwd": fwd, "rev": rev, "verdict": v}, ensure_ascii=False) + "\n")
            out.flush()
            c = Counter(verdicts)
            win = c.most_common(1)[0]
            tally[win[0] if win[1] >= 2 else "tie"] += 1
            print(f"  {left} vs {right}  {qid}  판정 {verdicts}", flush=True)

        n = sum(tally.values())
        print(f"\n  ══ {left} vs {right} (근거를 본 판정자 2인 다수결, 양방향) ══")
        for k in (left, right, "tie"):
            print(f"    {k:8} {tally[k]:2}/{n}  ({tally[k] / n * 100:.0f}%)")
    out.close()


if __name__ == "__main__":
    main()
