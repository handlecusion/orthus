"""D6 후속(B) — 워커 투표 앙상블이 규칙(static=solar)을 이기나.

학습 선택기는 규칙을 못 이겼다(§d6). 마지막 질문: 예측이 아니라 **추론 시점 여러 워커를
돌려 답을 결합**하면 headroom을 회수하나? 주판정 463 holdout에 solar/ax/exaone를 다시
돌려 실제 반환 숫자셋을 캡처하고 전략별 채점 + McNemar.

메트릭 = gold ⊆ 반환숫자셋(subset/recall). 함의: union은 구조적으로 oracle(더 반환할수록
유리) = 실 답이 아님 → 참고 상한으로만. 공정한 앙상블 = 하나의 일관된 답을 내는 다수결.

전략:
  static      solar 단독 (규칙 baseline)
  vote2of3    ≥2 워커가 '똑같은 숫자셋' 반환하면 그걸, 아니면 solar (일관된 단일 답)
  best_pair   solar+ax 둘 중 더 큰 셋(=recall↑) — 2-워커 저비용 결합
  union3      셋 합집합 (=oracle 상한, 실 답 아님 — 참고)
  oracle      아무 워커나 정답
McNemar는 vote2of3 / best_pair 각각 vs static.
실행(로컬): orthus_company_0706 + FUGU_KEYS. 재실행 방지 위해 raw 캡처 저장.
"""
from __future__ import annotations

import json
import sys
import time
from math import comb
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent.parent))

from pool import build_pool  # noqa: E402
from t3_gold import model_numbers  # noqa: E402
from orthus.structured.query import query_structured  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
DATA = HERE / "data"
GOLDEN = HERE.parent / "golden"
RAW = HERE.parent / "analysis" / "raw"
WORKERS = ["solar", "ax", "exaone"]
STATIC = "solar"


def mcnemar(a, b):
    ids = set(a) & set(b)
    b10 = sum(1 for i in ids if a[i] and not b[i])
    b01 = sum(1 for i in ids if not a[i] and b[i])
    n = b10 + b01
    if n == 0:
        return b10, b01, 1.0
    k = min(b10, b01)
    return b10, b01, min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def _query_retry(chat, q, tries=5):
    """OperationalError(연결 풀 고갈/stale)에 재시도 + 풀 dispose. status='executed' 될 때까지."""
    from orthus.db import get_engine, get_ro_engine
    last = None
    for a in range(tries):
        try:
            r = query_structured(UID, q, scope="company", chat_model=chat)
            if r.status == "executed" or "Operational" not in str(getattr(r, "error", "") or ""):
                return r, None
            last = f"status:{r.status}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}"
            if "Operational" not in type(e).__name__ and "Operational" not in str(e):
                return None, last  # DB 무관 오류는 재시도 안 함
        # 연결 문제 → 풀 리셋 후 백오프
        try:
            get_ro_engine().dispose(); get_engine().dispose()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.0 * (a + 1))
    return None, last


def capture(items):
    """워커별 (status, 반환숫자셋) 캡처. raw 저장 → 재실행 방지. OperationalError 재시도 내장."""
    raw_path = RAW / "holdout_worker_answers.json"
    if raw_path.exists():
        print(f"[cache] {raw_path.name} 재사용")
        return json.loads(raw_path.read_text(encoding="utf-8"))
    pool = build_pool(WORKERS)
    ans: dict[str, dict] = {it["id"]: {} for it in items}
    for slug, chat in pool.items():
        t0 = time.monotonic()
        errs = 0
        for n, it in enumerate(items, 1):
            r, err = _query_retry(chat, it["q"])
            if r is not None:
                nums = sorted(model_numbers(r.rows)) if r.status == "executed" else []
                ans[it["id"]][slug] = {"status": r.status, "nums": nums}
            else:
                ans[it["id"]][slug] = {"status": f"err:{err}", "nums": []}
                errs += 1
            time.sleep(0.05)  # 풀 압력 완화
            if n % 100 == 0:
                print(f"  [{slug}] {n}/{len(items)} err={errs} ({(time.monotonic()-t0)/n:.1f}s/문항)", flush=True)
        print(f"[{slug}] 완료 err={errs}/{len(items)} ({(time.monotonic()-t0)/60:.1f}분)", flush=True)
    raw_path.write_text(json.dumps(ans, ensure_ascii=False), encoding="utf-8")
    print(f"[save] {raw_path}")
    return ans


def main():
    items = json.loads((GOLDEN / "t3_unseen_holdout.json").read_text(encoding="utf-8"))["items"]
    gold = {it["id"]: set(it["gold"]) for it in items}
    ids = [it["id"] for it in items]
    ans = capture(items)

    def numset(i, m):
        return set(ans[i].get(m, {}).get("nums", []))

    def covers(s, i):
        g = gold[i]
        return bool(g) and g <= s

    strat = {i: {} for i in ids}
    for i in ids:
        sets = {m: numset(i, m) for m in WORKERS}
        strat[i]["static"] = sets[STATIC]
        # vote2of3: 정확히 같은 셋을 2개 이상이 반환하면 그것, 아니면 solar
        from collections import Counter
        key = Counter(tuple(sorted(sets[m])) for m in WORKERS)
        top, cnt = key.most_common(1)[0]
        strat[i]["vote2of3"] = set(top) if cnt >= 2 else sets[STATIC]
        strat[i]["best_pair"] = sets["solar"] | sets["ax"]     # 2-워커 recall 결합
        strat[i]["union3"] = sets["solar"] | sets["ax"] | sets["exaone"]

    names = ["static", "vote2of3", "best_pair", "union3"]
    correct = {nm: {i: covers(strat[i][nm], i) for i in ids} for nm in names}
    o = {i: any(covers(numset(i, m), i) for m in WORKERS) for i in ids}
    print(f"\n══ 워커 투표 앙상블 (주판정 holdout n={len(ids)}) ══")
    print(f"  oracle(참고 상한) {sum(o.values())/len(ids)*100:.1f}%")
    b_correct = correct["static"]
    b_rate = sum(b_correct.values()) / len(ids) * 100
    print(f"\n{'전략':10s} {'정답률':>7s} {'vs static':>9s} {'McNemar':>10s}  비고")
    for nm in names:
        cr = sum(correct[nm].values()) / len(ids) * 100
        if nm == "static":
            print(f"{nm:10s} {cr:6.1f}% {'—':>9s} {'—':>10s}  (규칙 baseline)")
            continue
        w, lo, pv = mcnemar(correct[nm], b_correct)
        sig = "★유의승" if pv < 0.05 and cr > b_rate else ("유의패" if pv < 0.05 and cr < b_rate else "무의")
        note = "실 답 아님(recall 상한)" if nm in ("union3",) else "일관된 단일 답"
        print(f"{nm:10s} {cr:6.1f}% {cr-b_rate:+8.1f}p {'p='+format(pv,'.4f'):>10s}  {sig} · {note}  [c만맞{w} b만맞{lo}]")
    # 불일치 부분집합
    dis = [i for i in ids if len({tuple(sorted(numset(i, m))) for m in WORKERS}) > 1]
    print(f"\n[워커 답 불일치 {len(dis)}/{len(ids)}]")
    for nm in ["static", "vote2of3", "best_pair"]:
        cd = sum(1 for i in dis if correct[nm][i]) / len(dis) * 100
        print(f"  {nm:10s} {cd:.1f}%")
    print("\n판정: vote2of3/best_pair이 p<0.05 & >static 이면 → 앙상블이 규칙을 이김(선택기와 달리).")


if __name__ == "__main__":
    main()
