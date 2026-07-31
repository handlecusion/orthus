"""T8 — 오케스트레이터 synthesize 품질 (D4 후속, judge 기반).

/agent-work/orchestrate의 마지막 결정 ④: 복합질문의 grounded 조각 답들을 하나로
통합한다(`router.decompose.synthesize`). 이 통합을 국내 모델로 구동해 품질을 잰다.

설계(입력 통제):
  1. 각 복합질문을 **고정 모델(solar)**로 split → 조각별 leaf answer() 실행 → grounded
     sub_answers를 만들어 **freeze**. (입력은 전 워커 공유 — synthesize 모델만 변수)
  2. 워커별 synthesize(q, frozen_subs, frozen_subqs, chat_model=worker) → 통합 답.
  3. 워커-통합 vs baseline-통합을 **양방향 쌍대 judge**(pairwise.judge_once 재사용).

grounded 조각 ≥2 여야 LLM 합성이 발화한다(1개 wiki/graph는 결정론 passthrough).
런타임에 ≥2 grounded 문항만 judge 대상으로 필터한다.

원자료 → analysis/raw/t8_<worker>.jsonl, t8_judge.jsonl (gitignore).
실행: (company env 로드 후)
  PYTHONPATH=$PWD:$PWD/experiments/fugu-ko python experiments/fugu-ko/t8_synth.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "judge"))

from pool import build_pool  # noqa: E402
from pairwise import judge_once  # noqa: E402
from orthus.models.adapters.openai_compat import OpenAIChat  # noqa: E402
from orthus.router import answer as router_answer  # noqa: E402
from orthus.router.decompose import split_question, synthesize, _extract_body  # noqa: E402
from orthus.schemas.canonical import SubAnswer, SubQuestion  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
RAW = HERE / "analysis" / "raw"
GOLDEN = HERE / "golden"
WORKERS = ["solar", "ax", "exaone"]
BASELINE = "baseline"
ALL = WORKERS + [BASELINE]
FIXED = "solar"  # 입력(sub_answers) 생성 고정 모델

_DENIALS = ("근거 없", "제공되지 않", "찾을 수 없", "정보가 없", "확인되지 않")


def _grounded_body(routed) -> str | None:
    """leaf RoutedAnswer에서 grounded 본문 추출. denial/빈 본문은 None."""
    if routed is None:
        return None
    body = (_extract_body(routed) or "").strip()
    if not body or any(d in body for d in _DENIALS):
        return None
    return body


def build_inputs(q: str, fixed_chat):
    """고정 모델로 split → leaf 실행 → grounded sub_answers freeze.
    returns (subqs, subas, n_grounded) or None(분해 불가)."""
    subtexts = split_question(q, k=3, chat_model=fixed_chat)
    if len(subtexts) < 2:
        return None
    subqs: list[SubQuestion] = []
    subas: list[SubAnswer] = []
    n_grounded = 0
    for t in subtexts:
        sid = uuid4()
        subqs.append(SubQuestion(id=sid, text=t, scope="company"))
        try:
            routed = router_answer(UID, t, scope="company", chat_model=fixed_chat,
                                   learn=False, record_gaps=False)
        except Exception:  # noqa: BLE001
            routed = None
        body = _grounded_body(routed)
        grounded = body is not None
        n_grounded += int(grounded)
        subas.append(SubAnswer(sub_question_id=sid, routed=routed, grounded=grounded, text=t))
    return subqs, subas, n_grounded


def main() -> None:
    items = json.load(open(GOLDEN / "t8.json", encoding="utf-8"))["items"]
    pool = build_pool(ALL)
    fixed_chat = pool[FIXED]
    RAW.mkdir(parents=True, exist_ok=True)

    # 1. 입력 freeze (고정 solar) — 전 워커 공유
    print("[T8] 입력 생성(고정 solar): split→leaf→grounded freeze")
    frozen: dict[str, tuple] = {}
    for it in items:
        got = build_inputs(it["q"], fixed_chat)
        if got is None:
            print(f"  {it['id']}  분해 불가(<2) → 제외")
            continue
        subqs, subas, ng = got
        frozen[it["id"]] = (it["q"], subqs, subas, ng)
        print(f"  {it['id']}  parts={len(subqs)} grounded={ng}"
              + ("  → judge 대상" if ng >= 2 else "  → 제외(grounded<2)"))

    eligible = {k: v for k, v in frozen.items() if v[3] >= 2}
    print(f"\n  judge 대상 {len(eligible)}/{len(items)}문항 (grounded≥2)\n")
    if not eligible:
        print("  grounded≥2 문항 없음 — 합성 LLM 미발화. 종료.")
        return

    # 2. 워커별 synthesize (동일 freeze 입력)
    for slug in ALL:
        chat = pool[slug]
        rows = []
        for qid, (q, subqs, subas, ng) in eligible.items():
            t0 = time.monotonic()
            try:
                res = synthesize(q, subas, subqs, chat_model=chat)
                ans = res.answer if res else ""
            except Exception as e:  # noqa: BLE001
                ans = ""
                rows.append({"id": qid, "answer": "", "error": f"{type(e).__name__}: {str(e)[:100]}",
                             "ms": int((time.monotonic() - t0) * 1000)})
                continue
            rows.append({"id": qid, "answer": ans, "n_grounded": ng,
                         "ms": int((time.monotonic() - t0) * 1000)})
        with open(RAW / f"t8_{slug}.jsonl", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        answered = sum(1 for r in rows if r.get("answer"))
        print(f"  {slug:8} synthesized {answered}/{len(eligible)}")

    # 3. 양방향 쌍대 judge (worker vs baseline synthesizer)
    judge = OpenAIChat(os.environ["ORTHUS_LLM_BASE_URL"], os.environ["ORTHUS_LLM_API_KEY"], "gpt-4o")

    def load(m):
        return {r["id"]: r for r in map(json.loads,
                (RAW / f"t8_{m}.jsonl").read_text("utf-8").splitlines()) if r}

    data = {m: load(m) for m in ALL}
    verdicts = []
    tally = {w: {"win": 0, "loss": 0, "tie": 0} for w in WORKERS}
    for w in WORKERS:
        for qid, (q, *_rest) in eligible.items():
            aw = data[w].get(qid, {}).get("answer") or ""
            ab = data[BASELINE].get(qid, {}).get("answer") or ""
            v1 = judge_once(judge, q, aw, ab)  # worker=A
            v2 = judge_once(judge, q, ab, aw)  # worker=B
            if v1 == "A" and v2 == "B":
                res = "win"
            elif v1 == "B" and v2 == "A":
                res = "loss"
            else:
                res = "tie"
            tally[w][res] += 1
            verdicts.append({"worker": w, "id": qid, "v_fwd": v1, "v_rev": v2, "result": res})

    with open(RAW / "t8_judge.jsonl", "w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")

    print("\n== T8 synthesize 쌍대비교 (워커 통합 vs baseline 통합, 입력 고정) ==")
    print(f"  judge=gpt-4o, 양방향 일치만 승패, n={len(eligible)}문항\n")
    print(f"  {'worker':8} {'승':>3} {'패':>3} {'무':>3}  {'승률':>7}  {'수용(≥40%)':>10}")
    for w in WORKERS:
        t = tally[w]
        dec = t["win"] + t["loss"]
        wr = (t["win"] / dec * 100) if dec else 0
        ok = "PASS" if wr >= 40 else "FAIL"
        print(f"  {w:8} {t['win']:>3} {t['loss']:>3} {t['tie']:>3}  {wr:>6.0f}%  {ok:>10}")


if __name__ == "__main__":
    main()
