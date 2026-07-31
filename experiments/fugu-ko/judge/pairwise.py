"""T2 쌍대비교 judge (D3). 국내 워커 각 vs 현행 baseline(gpt-4o-mini).

judge = OpenAI **gpt-4o(풀버전)** — 워커/기준선보다 강함. 기준선이 gpt-4o-mini(동일 계열)라
self-preference는 **기준선에 유리(=국내에 보수적)** 방향. 아래 3중으로 완화:
  1. 익명화: 모델 정체 비노출, "답변 A/B"만.
  2. 위치 편향 방어: 각 쌍을 **양방향(A↔B 스왑) 2회** 판정. 두 방향이 일치할 때만 승패 인정,
     불일치(위치에 흔들림)는 tie 처리.
  3. baseline 승 문항은 사람 스팟체크 대상으로 표시(출력).
판정 지표 = 절대점수 아님, **쌍대 승률**. 수용 기준(§4.4): 국내 vs 기준선 승률 ≥ 40%.

원자료(판정 이유 포함) → analysis/raw/t2_judge.jsonl (gitignore). 비용: 30문항 × 3워커 ×
2방향 = 180 gpt-4o 호출(≈ 수천원).

실행: (company env 로드 후) PYTHONPATH=... python experiments/fugu-ko/judge/pairwise.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT.parent.parent))  # orthus repo root

from orthus.models.adapters.openai_compat import OpenAIChat  # noqa: E402

RAW = ROOT / "analysis" / "raw"
GOLDEN = ROOT / "golden"
WORKERS = ["solar", "ax", "exaone"]
BASELINE = "baseline"

_SYS = (
    "너는 사내 지식 비서의 답변 품질을 평가하는 엄정한 심사관이다. "
    "같은 질문에 대한 두 답변(A, B)을 정확성·구체성·질문 적합성·근거성 기준으로 비교한다. "
    "장황함이 곧 우수함이 아니며, 틀린 단정은 강하게 감점한다. "
    '출력은 JSON 하나: {"winner": "A"|"B"|"tie", "reason": "<한 문장>"}.'
)


def _prompt(q: str, a: str, b: str) -> str:
    return f"[질문]\n{q}\n\n[답변 A]\n{a}\n\n[답변 B]\n{b}\n\n어느 답변이 더 나은가?"


def load(model: str) -> dict[str, dict]:
    return {r["id"]: r for r in map(json.loads, (RAW / f"t2_{model}.jsonl").read_text("utf-8").splitlines()) if r}


def judge_once(judge, q: str, a: str, b: str) -> str:
    try:
        out = judge.complete(_SYS, _prompt(q, a, b), json_only=True)
        w = str(json.loads(out).get("winner", "tie")).strip().lower()
        return {"a": "A", "b": "B", "tie": "tie"}.get(w, "tie")
    except Exception:  # noqa: BLE001
        return "tie"


def main() -> None:
    judge = OpenAIChat(os.environ["ORTHUS_LLM_BASE_URL"], os.environ["ORTHUS_LLM_API_KEY"], "gpt-4o")
    items = json.load(open(GOLDEN / "t2.json", encoding="utf-8"))["items"]
    qmap = {it["id"]: it["q"] for it in items}
    data = {m: load(m) for m in WORKERS + [BASELINE]}
    verdicts = []

    tally = {w: {"win": 0, "loss": 0, "tie": 0} for w in WORKERS}
    for w in WORKERS:
        for qid, q in qmap.items():
            aw = data[w].get(qid, {}).get("answer") or ""
            ab = data[BASELINE].get(qid, {}).get("answer") or ""
            # 방향1: A=worker, B=baseline / 방향2: 스왑
            v1 = judge_once(judge, q, aw, ab)   # worker=A
            v2 = judge_once(judge, q, ab, aw)   # worker=B
            if v1 == "A" and v2 == "B":
                res = "win"
            elif v1 == "B" and v2 == "A":
                res = "loss"
            else:
                res = "tie"  # 위치에 흔들림 → 신뢰불가
            tally[w][res] += 1
            verdicts.append({"worker": w, "id": qid, "v_fwd": v1, "v_rev": v2, "result": res})

    RAW.mkdir(parents=True, exist_ok=True)
    with open(RAW / "t2_judge.jsonl", "w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")

    print("== T2 쌍대비교 (각 국내 워커 vs 현행 baseline gpt-4o-mini) ==")
    print("  judge=gpt-4o, 양방향 일치만 승패, n=%d문항\n" % len(qmap))
    print(f"  {'worker':8} {'승':>3} {'패':>3} {'무':>3}  {'승률(승/(승+패))':>16}  {'수용(≥40%)':>10}")
    for w in WORKERS:
        t = tally[w]
        dec = t["win"] + t["loss"]
        wr = (t["win"] / dec * 100) if dec else 0
        ok = "PASS" if wr >= 40 else "FAIL"
        print(f"  {w:8} {t['win']:>3} {t['loss']:>3} {t['tie']:>3}  {wr:>14.0f}%  {ok:>10}")

    print("\n  [baseline 승 문항 — 사람 스팟체크 대상]")
    for w in WORKERS:
        losses = [v["id"] for v in verdicts if v["worker"] == w and v["result"] == "loss"]
        if losses:
            print(f"    {w}: {losses}")


if __name__ == "__main__":
    main()
