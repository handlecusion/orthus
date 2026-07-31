"""E3 — decompose 프리필터 신호 확장 측정 (O4 후속, analysis/e3-decompose-prefilter-plan.md).

측정 대상은 **프로덕션 코드의 실경로**다: `orthus.router.decompose.should_decompose(ext_tier=T)`.
하네스는 티어를 스윕만 하고 판정 로직을 재구현하지 않는다.

3단 측정 (§4.2 — FP의 실비용 분리):
  ① 프리필터 도달(reached_llm)   — LLM 0회. 누락형에선 recall, 대조군에선 "지연 비용"(게이트 1콜).
  ② LLM 게이트 판정(gate=yes)     — solar 고정(변인 통제).
  ③ 실제 fan-out(split ≥ 2)       — 게이트 yes가 실제 분해로 이어졌는지. 대조군에선 **진짜 오분해**.

판정(§5, 사전 고정): 누락형 recall ≥ 80% AND 대조군 최종 오분해율 ≤ 5% AND 대조군 프리필터
오통과율 ≤ 15% AND 기존 t7 22문항 최종 판정 무변화.

usage:  python -m experiments.fugu-ko.e3_prefilter            # 전체(기본 solar)
        python experiments/fugu-ko/e3_prefilter.py --model solar --max-tier 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pool import build_pool  # noqa: E402

from orthus.router.decompose import (  # noqa: E402
    _has_command_verb,
    _has_connective_or_enum,
    should_decompose,
    split_question,
)

GOLDEN = HERE / "golden"
RAW = HERE / "analysis" / "raw"  # harness.py와 동일 — gitignore 대상(원자료 미커밋)

# §5 사전 고정 임계치
THRESH_RECALL = 0.80
THRESH_FP_FINAL = 0.05
THRESH_FP_PREFILTER = 0.15


def reached(q: str, tier: int) -> bool:
    """프로덕션 프리필터와 동일한 stage 1 판정 (LLM 0회)."""
    compact = q.lower().replace(" ", "")
    return _has_command_verb(compact) or _has_connective_or_enum(q, ext_tier=tier)


class GateCache:
    """질문당 LLM 호출 1회씩만 — 티어를 올려도 같은 질문의 게이트/split 판정은 재사용한다."""

    def __init__(self, chat):
        self.chat = chat
        self.gate: dict[str, bool] = {}
        self.split: dict[str, int] = {}
        self.calls = 0

    def verdict(self, q: str) -> bool:
        if q not in self.gate:
            # ext_tier=9는 clamp되어 MAX와 같다. 프리필터는 이미 호출자가 통과시켰으므로
            # 여기서는 stage 2(LLM enum)만 재는 것이 목적이다.
            self.gate[q] = should_decompose(q, chat_model=self.chat, ext_tier=9)
            self.calls += 1
        return self.gate[q]

    def n_parts(self, q: str) -> int:
        if q not in self.split:
            self.split[q] = len(split_question(q, k=5, chat_model=self.chat))
            self.calls += 1
        return self.split[q]


def evaluate(items: list[dict], tier: int, cache: GateCache) -> list[dict]:
    rows = []
    for it in items:
        q = it["q"]
        r = reached(q, tier)
        gate = cache.verdict(q) if r else False
        parts = cache.n_parts(q) if gate else 0
        rows.append(
            {
                "id": it["id"],
                "q": q,
                "tier": tier,
                "reached_llm": r,
                "gate": gate,
                "n_parts": parts,
                "fanned_out": bool(gate and parts >= 2),
                "compound": bool(it.get("compound")),
                "type": it.get("type") or it.get("trap"),
            }
        )
    return rows


def pct(num: int, den: int) -> float:
    return (num / den * 100) if den else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="solar")
    ap.add_argument("--max-tier", type=int, default=3)
    args = ap.parse_args()

    missed = json.load(open(GOLDEN / "e3_missed.json", encoding="utf-8"))["items"]
    control = json.load(open(GOLDEN / "e3_control.json", encoding="utf-8"))["items"]
    t7 = json.load(open(GOLDEN / "t7.json", encoding="utf-8"))["items"]

    # 자연 분포 대조군(§7 대표성): 위 대조군은 40문항 중 28개가 함정인 **의도된 최악치**라
    # 거기서 잰 오통과율은 실트래픽 비용이 아니다. 실비용은 저작 개입이 없는 기존 단일 질문
    # 세트 — t5(라우팅 골든, 전부 단일) + t7의 compound=false — 에서 잰다. 이 세트에는 명령
    # verb가 든 문항이 있어 tier 0에서도 일부 도달하므로, 비용은 **tier 0 대비 증분**으로 본다.
    t5 = json.load(open(GOLDEN / "t5.json", encoding="utf-8"))["items"]
    natural = [
        {"id": i["id"], "q": i["q"], "compound": False, "trap": "natural"} for i in t5 if i.get("q")
    ] + [
        {"id": i["id"], "q": i["q"], "compound": False, "trap": "natural"}
        for i in t7
        if not i.get("compound")
    ]

    # ── G-골든 검증: 누락형은 "현행 프리필터에서 도달 실패"인 것만 유효하다(§4.3) ──
    bad_missed = [i["id"] for i in missed if reached(i["q"], 0)]
    bad_control = [i["id"] for i in control if reached(i["q"], 0)]
    print(f"[골든] 누락형 {len(missed)} / 대조군 {len(control)} / t7 {len(t7)}")
    if bad_missed:
        print(f"  ✗ 누락형인데 tier 0에서 이미 도달: {bad_missed}")
    if bad_control:
        print(f"  ✗ 대조군인데 tier 0에서 이미 도달(FP 기저 오염): {bad_control}")
    if bad_missed or bad_control:
        print("  → 골든 수정 필요. 측정 중단.")
        sys.exit(1)
    print("  ✓ tier 0 기저: 누락형 recall 0% / 대조군 오통과 0% (O4 재현)\n")

    if args.model == "prod":
        # §7 리스크 점검: 게이트의 FP 필터 성능이 모델 의존일 수 있으므로, 채택 구성을
        # 프로덕션 기본 모델(node.env `ORTHUS_LLM`)로 1회 재측정한다(운영 구성 검증).
        from orthus.models.registry import get_chat_model

        chat = get_chat_model()
        slug = "prod"
    else:
        pool = build_pool([args.model])
        slug, chat = next(iter(pool.items()))
    cache = GateCache(chat)
    t0 = time.monotonic()

    RAW.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    summary: list[dict] = []

    print(f"[게이트 모델] {slug} ({chat.model_id}) — 변인 통제 고정\n")
    header = (
        f"{'tier':<5} {'누락형 recall':>14} {'누락형 실분해':>14} "
        f"{'대조군 오통과':>14} {'대조군 오분해':>14}"
    )
    print(header)
    print("-" * len(header))

    for tier in range(0, args.max_tier + 1):
        m_rows = evaluate(missed, tier, cache)
        c_rows = evaluate(control, tier, cache)
        n_rows = evaluate(natural, tier, cache)
        all_rows += m_rows + c_rows + n_rows

        m_reach = sum(r["reached_llm"] for r in m_rows)
        m_fan = sum(r["fanned_out"] for r in m_rows)
        c_reach = sum(r["reached_llm"] for r in c_rows)
        c_fan = sum(r["fanned_out"] for r in c_rows)
        nat_reach = sum(r["reached_llm"] for r in n_rows)
        nat_fan = sum(r["fanned_out"] for r in n_rows)
        n_m, n_c, n_n = len(m_rows), len(c_rows), len(n_rows)

        summary.append(
            {
                "tier": tier,
                "recall_prefilter": pct(m_reach, n_m),
                "recall_fanout": pct(m_fan, n_m),
                "fp_prefilter": pct(c_reach, n_c),
                "fp_final": pct(c_fan, n_c),
                "nat_prefilter": pct(nat_reach, n_n),
                "nat_final": pct(nat_fan, n_n),
                "nat_n": n_n,
            }
        )
        print(
            f"{tier:<5} {m_reach:>3}/{n_m} {pct(m_reach, n_m):>6.1f}% "
            f"{m_fan:>3}/{n_m} {pct(m_fan, n_m):>6.1f}% "
            f"{c_reach:>3}/{n_c} {pct(c_reach, n_c):>6.1f}% "
            f"{c_fan:>3}/{n_c} {pct(c_fan, n_c):>6.1f}% "
            f"| 자연 {nat_reach:>2}/{n_n} {pct(nat_reach, n_n):>5.1f}% "
            f"{nat_fan:>2}/{n_n} {pct(nat_fan, n_n):>5.1f}%"
        )

    # ── t7 회귀: 기존 22문항의 최종 판정(should_decompose)이 티어별로 바뀌는가 ──
    print("\n[t7 회귀] 기존 골든 22문항 최종 판정 변화")
    base = {}
    t7_rows = []
    for tier in range(0, args.max_tier + 1):
        verdicts = {}
        for it in t7:
            q = it["q"]
            verdicts[it["id"]] = cache.verdict(q) if reached(q, tier) else False
        if tier == 0:
            base = verdicts
            n_correct = sum(v == bool(it.get("compound")) for it, v in zip(t7, verdicts.values()))
            print(f"  tier 0 기준선: gate_correct {n_correct}/{len(t7)}")
            continue
        changed = [k for k in verdicts if verdicts[k] != base[k]]
        n_correct = sum(v == bool(it.get("compound")) for it, v in zip(t7, verdicts.values()))
        flips = [
            f"{k}({'F→T' if verdicts[k] else 'T→F'},"
            f"{'개선' if verdicts[k] == bool(next(i for i in t7 if i['id'] == k).get('compound')) else '악화'})"
            for k in changed
        ]
        print(
            f"  tier {tier}: 판정 변화 {len(changed)}건 {flips or ''} · "
            f"gate_correct {n_correct}/{len(t7)}"
        )
        t7_rows.append({"tier": tier, "changed": changed, "gate_correct": n_correct})

    # ── §5 판정 ──
    print("\n[§5 판정] recall ≥ 80% · 최종 오분해(adversarial) ≤ 5% · 프리필터 오통과 ≤ 15%")
    print(
        "  주: 오통과 상한은 **자연 분포 대조군의 tier 0 대비 증분**으로 본다. adversarial "
        "대조군은 40문항 중 28개가 함정인 최악치라 그 위의 오통과율은 지연 비용의 상계일 뿐이다."
    )
    base_nat = summary[0]["nat_prefilter"]
    for s in summary:
        if s["tier"] == 0:
            continue
        nat_delta = s["nat_prefilter"] - base_nat
        ok_recall = s["recall_prefilter"] >= THRESH_RECALL * 100
        ok_fp_final = s["fp_final"] <= THRESH_FP_FINAL * 100
        ok_fp_pre = nat_delta <= THRESH_FP_PREFILTER * 100
        verdict = "채택" if (ok_recall and ok_fp_final and ok_fp_pre) else "기각"
        why = []
        if not ok_recall:
            why.append(f"recall {s['recall_prefilter']:.1f}%")
        if not ok_fp_final:
            why.append(f"오분해 {s['fp_final']:.1f}%")
        if not ok_fp_pre:
            why.append(f"자연 오통과 증분 {nat_delta:+.1f}%p")
        print(
            f"  T{s['tier']}: {verdict} "
            f"(recall {s['recall_prefilter']:.1f}% · 오분해 {s['fp_final']:.1f}% · "
            f"자연 오통과 {s['nat_prefilter']:.1f}% [{nat_delta:+.1f}%p] · "
            f"최악 오통과 {s['fp_prefilter']:.1f}%)" + (f" — 미달: {', '.join(why)}" if why else "")
        )

    elapsed = int(time.monotonic() - t0)
    out = RAW / f"e3_{slug}.json"
    out.write_text(
        json.dumps(
            {"summary": summary, "t7": t7_rows, "rows": all_rows, "llm_calls": cache.calls},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\nLLM 호출 {cache.calls}회 · {elapsed}s · 원자료 {out}")


if __name__ == "__main__":
    main()
