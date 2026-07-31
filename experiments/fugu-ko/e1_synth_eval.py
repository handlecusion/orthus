"""E1 Phase B 평가 — 합성 test(hard 분포)에서 투표/캐스케이드 실측.

입력: analysis/raw/e1_synth_{m}.jsonl (Phase B 재실행, rows 보유) + train/data/labels_{m}.jsonl(routing).

★ 비교 가능성 경고: (c) 학습 선택기의 87.3%는 **구 라벨**(2026-07-10경) 기준이다. Phase B
재실행에서 exaone structured 정오가 32/63 → 50/63으로 크게 변했으므로(원인 §드리프트),
구 라벨 위의 (c) 수치와 신 라벨 위의 (d) 수치를 직접 비교할 수 없다. 따라서 본 평가는
**같은 신선 라벨 위에서 (a)/(b)/(d)/oracle을 내부 일관되게** 재계산하고, (c)는 참고로만 병기한다.

실행: PYTHONPATH=$PWD python experiments/fugu-ko/e1_synth_eval.py
"""
from __future__ import annotations

import json
from pathlib import Path

from e1_vote import TRIGGERS, WORKERS, model_numbers  # 동일 트리거/추출기 재사용

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"
DATA = HERE / "train" / "data"
STATIC = {"structured": "solar", "routing": "ax"}
PRIMARY, SECONDARY = "solar", "exaone"


def load(p: Path) -> dict[str, dict]:
    return {r["id"]: r for r in map(json.loads, p.read_text(encoding="utf-8").splitlines()) if r}


def main() -> None:
    new = {m: load(RAW / f"e1_synth_{m}.jsonl") for m in WORKERS}
    old = {m: load(DATA / f"labels_{m}.jsonl") for m in WORKERS}
    split = json.load(open(DATA / "split.json", encoding="utf-8"))
    labeled = load(DATA / "labeled.jsonl")

    st_ids = [i for i in split["test"] if i in new["solar"]]
    rt_ids = [i for i in split["test"] if i in labeled and labeled[i]["task"] == "routing"]
    n = len(st_ids) + len(rt_ids)

    def rec(m: str, i: str) -> dict:
        r = new[m][i]
        ex = r["status"] == "executed"
        return {
            "executed": ex,
            "gate_passed": bool(r.get("gate_passed")),
            "row_count": r.get("row_count") or 0,
            "nums": frozenset(model_numbers(r.get("rows")) if ex else set()),
            "correct": bool(r["correct"]),
        }

    print(f"{'=' * 78}\n[E1 Phase B] 합성 test {n}문항 (structured {len(st_ids)} / routing {len(rt_ids)})")
    print(f"신선 rows 실측 · 트리거 {list(TRIGGERS)}\n{'=' * 78}")

    # ── 단일 모델 (structured=신선, routing=기존 라벨) ──
    def single(m: str) -> int:
        return sum(rec(m, i)["correct"] for i in st_ids) + sum(
            bool(old[m][i]["correct"]) for i in rt_ids
        )

    print(f"\n  {'구성':38} {'정답':>9} {'정답률':>8} {'호출':>7}")
    for m in WORKERS:
        s = single(m)
        print(f"  {'(a) 단일 ' + m:38} {s:>4}/{n:<4} {s / n * 100:7.1f}% {1.0:>6.2f}×")

    # ── (b) static 규칙 ──
    b = sum(rec(STATIC["structured"], i)["correct"] for i in st_ids) + sum(
        bool(old[STATIC["routing"]][i]["correct"]) for i in rt_ids
    )
    print(f"  {'(b) static 규칙 (구조화→solar, 라우팅→ax)':38} {b:>4}/{n:<4} {b / n * 100:7.1f}% {1.0:>6.2f}×")

    # ── (d1) 투표 (structured만; routing은 static 유지) ──
    v_ok, v_gain, v_loss, wrong_conv = 0, [], [], []
    for i in st_ids:
        rs = {m: rec(m, i) for m in WORKERS}
        groups: dict[frozenset, list[str]] = {}
        for m in WORKERS:
            if rs[m]["executed"]:
                groups.setdefault(rs[m]["nums"], []).append(m)
        best = max(groups.items(), key=lambda kv: len(kv[1]), default=(frozenset(), []))
        if len(best[1]) >= 2:
            correct = rs[best[1][0]]["correct"]  # 같은 nums ⇒ 같은 정오
            if not correct:
                wrong_conv.append((i, "+".join(best[1])))
        else:
            correct = rs[PRIMARY]["correct"]
        v_ok += correct
        if correct and not rs[PRIMARY]["correct"]:
            v_gain.append(i)
        if not correct and rs[PRIMARY]["correct"]:
            v_loss.append(i)
    v_total = v_ok + sum(bool(old[STATIC["routing"]][i]["correct"]) for i in rt_ids)
    v_calls = (3 * len(st_ids) + len(rt_ids)) / n
    print(f"  {'(d1) 투표 2-of-3 (structured)':38} {v_total:>4}/{n:<4} {v_total / n * 100:7.1f}% {v_calls:>6.2f}×")

    # ── (d2) 결정론 캐스케이드 ──
    c_ok, fired, c_gain, c_loss = 0, [], [], []
    for i in st_ids:
        p, s = rec(PRIMARY, i), rec(SECONDARY, i)
        if any(f(p) for f in TRIGGERS.values()):
            fired.append(i)
            c_ok += s["correct"]
            if s["correct"] and not p["correct"]:
                c_gain.append(i)
            if not s["correct"] and p["correct"]:
                c_loss.append(i)
        else:
            c_ok += p["correct"]
    c_total = c_ok + sum(bool(old[STATIC["routing"]][i]["correct"]) for i in rt_ids)
    c_calls = (len(st_ids) + len(fired) + len(rt_ids)) / n
    print(
        f"  {'(d2) 캐스케이드 solar→exaone':38} {c_total:>4}/{n:<4} {c_total / n * 100:7.1f}% {c_calls:>6.2f}×"
    )

    # ── oracle ──
    o = sum(1 for i in st_ids if any(rec(m, i)["correct"] for m in WORKERS)) + sum(
        1 for i in rt_ids if any(bool(old[m][i]["correct"]) for m in WORKERS)
    )
    print(f"  {'oracle (이론 상한)':38} {o:>4}/{n:<4} {o / n * 100:7.1f}%")
    print(f"\n  {'[참고] (c) 학습 선택기 — 구 라벨 기준':38} {'':>4}     {87.3:7.1f}%   ← 직접 비교 불가(§드리프트)")

    print("\n  [투표 상세]  회수 %d %s / 손실 %d %s" % (len(v_gain), v_gain, len(v_loss), v_loss))
    print(f"  [★ 오답 수렴] {len(wrong_conv)}/{len(st_ids)}  {wrong_conv if wrong_conv else '없음'}")
    print(f"\n  [캐스케이드 상세] 발동 {len(fired)}/{len(st_ids)} = {len(fired) / len(st_ids) * 100:.1f}%")
    print(f"    회수(+) {len(c_gain)} {c_gain}")
    print(f"    손실(-) {len(c_loss)} {c_loss}")
    silent = [
        i
        for i in st_ids
        if not rec(PRIMARY, i)["correct"] and not any(f(rec(PRIMARY, i)) for f in TRIGGERS.values())
    ]
    print(f"    트리거 미발동 오답('확신에 찬 오답') {len(silent)}/{len(st_ids)} → 캐스케이드 구조적 한계")

    # 태그별 캐스케이드 발동/회수 (hard 유형 진단)
    print("\n  [태그별 — hard 유형에서 작동하는가]")
    tags: dict[str, list[str]] = {}
    for i in st_ids:
        tags.setdefault(new["solar"][i].get("tag") or "?", []).append(i)
    print(f"    {'tag':9} {'n':>3} {'solar':>7} {'투표':>7} {'캐스':>7} {'oracle':>7}")
    for t, ids in sorted(tags.items()):
        sp = sum(rec(PRIMARY, i)["correct"] for i in ids)
        so = sum(1 for i in ids if any(rec(m, i)["correct"] for m in WORKERS))
        vv = sum(1 for i in ids if i not in v_loss and (rec(PRIMARY, i)["correct"] or i in v_gain))
        cc = sum(1 for i in ids if i not in c_loss and (rec(PRIMARY, i)["correct"] or i in c_gain))
        print(f"    {t:9} {len(ids):>3} {sp:>7} {vv:>7} {cc:>7} {so:>7}")


if __name__ == "__main__":
    main()
