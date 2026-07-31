"""B4-X2 (option b) — allganize 판정자-사람 코로보레이션 (standalone runner).

`analysis/b4-prereg.md` §3(X2) + §2(자백) + `x0-external-dataset-plan.md` §3.2.

feasibility 결론(리포트 §X2):
  option (a) 외부 grounded QA는 이 창에서 비현실적이다 — allganize 데이터셋은 **원문 문서를
  포함하지 않고** documents.csv의 외부 gov/finance URL만 준다. 우리 `ask()`는 compiled wiki
  page에만 그라운딩하므로(raw-chunk RAG 금지), (a)는 PDF ~60건 수집→파싱→corpus 인덱싱→
  embedding→distill/consolidate LLM wiki 저작을 전부 거쳐야 하고, 지금 OpenAI embedding/chat이
  429로 막혀 있으며 동시 B1 리소스 제약과 충돌한다. 그래서 프리레그가 명시한 **더 작고 깨끗한**
  option (b)를 한다: 동봉된 사람 O/X 채점을 재활용해 **판정자-사람 일치**를 잰다(G-JUDGE 외적 타당성).

무엇을 재나:
  allganize의 (question, target_answer(참조), system_answer, human O/X) 튜플에 대해, **포인트와이즈
  정오 판정자**(질문+참조답+후보답 → O/X)를 solar로 실행하고 사람 O/X와의 **일치율 + Cohen's κ +
  혼동행렬**을 낸다. allganize 라벨이 포인트와이즈 정오라서 프로덕션 pairwise 판정자 대신 포인트와이즈
  프롬프트를 쓴다(라벨 공간 일치). 판정 모델은 solar(OpenAI 429; ax·bedrock 금지).

주의(리포트에 병기):
  - 이건 **판정자**를 검증하지 우리 답변을 검증하지 않는다(프리레그 §5.3).
  - closed-book 오염통제는 (a) grounded QA용이다. (b) 판정자는 참조답을 보므로 해당 없음 — 리포트에 명시.
  - 데이터 행 커밋 금지(gitignored .cache). 라이선스: MIT.

실행:
  .venv/bin/python experiments/fugu-ko/x2_allganize.py --n 240 --model solar
  .venv/bin/python experiments/fugu-ko/x2_allganize.py --score-only
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CSV_PATH = HERE / "external" / ".cache" / "raw" / "allganize" / "rag_evaluation_result.csv"
RAW = HERE / "analysis" / "raw"
SEED = 1234

_SYS = (
    "너는 한국어 질의응답의 정오를 판정하는 엄정한 채점관이다. "
    "참조 정답을 기준으로, 후보 답변이 질문에 대해 사실적으로 옳고 충분한지 판단한다. "
    "장황함은 가점 요인이 아니며, 참조 정답과 어긋나거나 핵심을 못 짚으면 오답이다. "
    '출력은 JSON 하나: {"verdict": "O"|"X", "reason": "<한 문장>"}. '
    "O=정답, X=오답."
)


def _prompt(q: str, ref: str, cand: str) -> str:
    return (
        f"[질문]\n{q}\n\n[참조 정답]\n{ref}\n\n[후보 답변]\n{cand}\n\n"
        "후보 답변은 정답(O)인가 오답(X)인가?"
    )


def _systems(cols: list[str]) -> list[str]:
    return sorted(c[:-3] for c in cols if c.endswith("_ox"))


def select_items(n: int) -> list[dict]:
    """사람 O/X 균형(각 절반) + 도메인 분산, 결정론 seed."""
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    systems = _systems(list(rows[0].keys()))
    pool: list[dict] = []
    for qi, r in enumerate(rows):
        q, ref, dom = r["question"].strip(), r["target_answer"].strip(), r["domain"]
        if not q or not ref:
            continue
        for s in systems:
            ans = (r.get(s + "_answer") or "").strip()
            ox = (r.get(s + "_ox") or "").strip().upper()
            if ans and ox in ("O", "X"):
                pool.append(
                    {"q": q, "ref": ref, "cand": ans, "human": ox, "domain": dom,
                     "system": s, "qidx": qi}
                )
    rng = random.Random(SEED)
    rng.shuffle(pool)
    half = n // 2
    chosen: list[dict] = []
    for want in ("O", "X"):
        c = [p for p in pool if p["human"] == want][:half]
        chosen.extend(c)
    rng.shuffle(chosen)
    for i, it in enumerate(chosen, 1):
        it["id"] = f"x2-{i:03d}"
    return chosen


def raw_path() -> Path:
    return RAW / "x2_allganize_solar.jsonl"


def build_solar():
    sys.path.insert(0, str(HERE))
    from pool import build_pool

    return build_pool(["solar"])["solar"]


def judge_once(chat, q: str, ref: str, cand: str) -> tuple[str, str]:
    out = chat.complete(_SYS, _prompt(q, ref, cand), json_only=True)
    obj = json.loads(out)
    v = str(obj.get("verdict", "")).strip().upper()
    return (v if v in ("O", "X") else "?", str(obj.get("reason", ""))[:200])


def run(items: list[dict], chat) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    with raw_path().open("w", encoding="utf-8") as fh:
        for i, it in enumerate(items, 1):
            t0 = time.monotonic()
            try:
                verdict, reason = judge_once(chat, it["q"], it["ref"], it["cand"])
                rec = {"id": it["id"], "domain": it["domain"], "system": it["system"],
                       "human": it["human"], "judge": verdict, "reason": reason,
                       "ms": int((time.monotonic() - t0) * 1000)}
            except Exception as e:  # noqa: BLE001
                rec = {"id": it["id"], "domain": it["domain"], "system": it["system"],
                       "human": it["human"], "judge": "err",
                       "error": f"{type(e).__name__}: {str(e)[:120]}"}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            if i % 20 == 0 or i == len(items):
                print(f"  solar {i:3}/{len(items)}", flush=True)


def cohen_kappa(pairs: list[tuple[str, str]]) -> float:
    labels = ("O", "X")
    n = len(pairs)
    if not n:
        return 0.0
    po = sum(1 for a, b in pairs if a == b) / n
    pe = 0.0
    for lab in labels:
        pa = sum(1 for a, _ in pairs if a == lab) / n
        pb = sum(1 for _, b in pairs if b == lab) / n
        pe += pa * pb
    return (po - pe) / (1 - pe) if pe != 1 else 0.0


def score() -> None:
    p = raw_path()
    if not p.exists():
        print(f"  ! raw 없음 ({p.name})")
        return
    recs = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    valid = [r for r in recs if r.get("judge") in ("O", "X")]
    errs = len(recs) - len(valid)
    pairs = [(r["human"], r["judge"]) for r in valid]
    n = len(pairs)
    agree = sum(1 for a, b in pairs if a == b)
    # 혼동행렬: human×judge
    cm = {("O", "O"): 0, ("O", "X"): 0, ("X", "O"): 0, ("X", "X"): 0}
    for a, b in pairs:
        cm[(a, b)] += 1
    kappa = cohen_kappa(pairs)
    print("\n  ── X2(b) 판정자-사람 코로보레이션 (판정=solar 포인트와이즈 O/X) ──")
    print(f"  n={n}  (err/무효 {errs})  일치율 {agree}/{n} ({agree / n * 100:.1f}%)  Cohen's κ = {kappa:.3f}")
    print("  혼동행렬 (행=사람, 열=판정):")
    print("           judge O   judge X")
    print(f"   human O   {cm[('O', 'O')]:>6}    {cm[('O', 'X')]:>6}")
    print(f"   human X   {cm[('X', 'O')]:>6}    {cm[('X', 'X')]:>6}")
    # per-domain agreement
    print("  도메인별 일치율:")
    for dom in sorted(set(r["domain"] for r in valid)):
        dp = [(r["human"], r["judge"]) for r in valid if r["domain"] == dom]
        da = sum(1 for a, b in dp if a == b)
        print(f"    {dom:10} {da}/{len(dp)} ({da / len(dp) * 100:.1f}%)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=240)
    ap.add_argument("--model", default="solar")
    ap.add_argument("--score-only", action="store_true")
    args = ap.parse_args()
    sys.path.insert(0, str(REPO))
    if args.score_only:
        score()
        return
    if args.model != "solar":
        raise SystemExit("이 창에서는 solar만 (OpenAI 429·ax/bedrock 금지)")
    items = select_items(args.n)
    hist = {"O": sum(1 for it in items if it["human"] == "O"),
            "X": sum(1 for it in items if it["human"] == "X")}
    print(f"선택 {len(items)} items (seed={SEED}, 사람 O/X 균형 {hist})")
    if not os.environ.get("ORTHUS_LLM_SOLAR_API_KEY") and not Path(REPO / ".env").exists():
        raise SystemExit("solar 키 없음")
    chat = build_solar()
    run(items, chat)
    score()


if __name__ == "__main__":
    main()
