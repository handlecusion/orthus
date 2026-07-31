"""D7 사전 진단 — 실행 시점(post-hoc) 신호로 solar 실패가 감지/회수 가능한가.

D6 결론("추론 시점에 어느 워커가 틀릴지 알 수 없음")은 *질문 텍스트만 본* 사전
라우팅에 대한 결론이다. 이 스크립트는 이미 캡처된 홀드아웃 워커 실행 결과
(`analysis/raw/holdout_worker_answers.json`)만으로 다음을 정량화한다:

1. solar 오답 중 실행 증거만으로 감지되는 비율 (status err / 빈 결과).
   gold가 빈 문항이 0이므로 "degenerate ⇒ 오답"은 **구조적 보장** — 위양성 0.
2. 결정론 degenerate-fallback 정책(solar degenerate → ax → exaone)의 정확도.
3. fallback으로도 못 잡는, 내용 검증(verifier)이 필요한 잔여 회수 대상.

실행: worktree root에서 `python train/diag_exec_signal.py`
"""

import json
from collections import Counter
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ans = json.load(open(ROOT / "analysis/raw/holdout_worker_answers.json"))
g = json.load(open(ROOT / "golden/t3_unseen_holdout.json"))
items = {it["id"]: it for it in g["items"]}
WORKERS = ["solar", "ax", "exaone"]


def ok(qid: str, w: str) -> bool:
    a = ans[qid][w]
    return a["status"] == "executed" and set(items[qid]["gold"]) <= set(a["nums"])


def degen(qid: str, w: str) -> bool:
    a = ans[qid][w]
    return a["status"] != "executed" or len(a["nums"]) == 0


def policy_fallback(qid: str) -> str:
    if not degen(qid, "solar"):
        return "solar"
    for w in ("ax", "exaone"):
        if not degen(qid, w):
            return w
    return "solar"


def main() -> None:
    qids = list(ans)
    n = len(qids)
    acc = {w: sum(ok(q, w) for q in qids) / n for w in WORKERS}
    print(f"n={n} acc={{k: round(v * 100, 1) for k, v in acc.items()}}", acc)

    assert sum(1 for q in qids if not items[q]["gold"]) == 0, "gold 빈 문항 존재 — 구조 보장 깨짐"

    solar_wrong = [q for q in qids if not ok(q, "solar")]
    sw_degen = [q for q in solar_wrong if degen(q, "solar")]
    fp = [q for q in qids if ok(q, "solar") and degen(q, "solar")]
    print(f"solar wrong {len(solar_wrong)} / degenerate {len(sw_degen)} / 위양성 {len(fp)}")

    pol = sum(ok(q, policy_fallback(q)) for q in qids) / n
    b = sum(1 for q in qids if ok(q, policy_fallback(q)) and not ok(q, "solar"))
    c = sum(1 for q in qids if not ok(q, policy_fallback(q)) and ok(q, "solar"))
    m = b + c
    p = min(1.0, sum(comb(m, k) for k in range(min(b, c) + 1)) / 2**m * 2) if m else 1.0
    print(f"[결정론 fallback] {pol * 100:.1f}% vs static {acc['solar'] * 100:.1f}% (득{b}/실{c}, McNemar p≈{p:.4g})")

    rem = [
        q
        for q in qids
        if not ok(q, "solar") and not degen(q, "solar") and (ok(q, "ax") or ok(q, "exaone"))
    ]
    det = sum(1 for q in qids if ok(q, policy_fallback(q)))
    orc = sum(1 for q in qids if any(ok(q, w) for w in WORKERS))
    print(f"잔여 회수 대상(내용 검증 필요) {len(rem)}문항, tag={Counter(items[q]['tag'] for q in rem)}")
    print(f"결정론 상한 {det / n * 100:.1f}% / oracle {orc / n * 100:.1f}% → 학습기 몫 +{(orc - det) / n * 100:.1f}%p")


if __name__ == "__main__":
    main()
