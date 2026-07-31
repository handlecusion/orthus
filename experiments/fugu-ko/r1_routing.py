"""R1 라우팅 홀드아웃 — 측정 하네스 (`docs/routing-holdout-plan.md` §4/§5/§6).

세 후보를 **같은 홀드아웃**에서 잰다:

  C1 모델 스왑     routing 배정을 solar → ax / exaone (또는 현행 gpt-4o-mini)
  C2 규칙 확장     결정론 신호 추가 (LLM 0콜)
  C3 교차평면 회수  structured가 0행/게이트거부면 그 리프를 wiki로 재질의

지표는 **비대칭을 지우지 않는다**(§5) — M1(wiki→structured)은 답이 완전히 소실되고,
M2(structured→wiki)는 위키가 부분 답을 내기도 한다. 합산 금지.

`graph` 라우팅은 프로덕션에서 bind 실패 시 wiki로 안전 demote되므로(K4b) 오류 계산에서는
wiki로 접어서 본다. 다만 발생 횟수는 따로 보고한다(숨기지 않는다).

실행 (company node env 선행):
    python r1_routing.py --stage c1c2
    python r1_routing.py --stage c3
    python r1_routing.py --stage report
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

from pool import build_pool  # noqa: E402
from e2_e2e import anchor_hit  # noqa: E402
from orthus.router.route import _rule_based_route, classify  # noqa: E402
from orthus.structured.query import query_structured  # noqa: E402
from orthus.wiki.retrieve import retrieve  # noqa: E402
from orthus.wiki.qa import answer_from_hits  # noqa: E402

UID = UUID("11111111-1111-1111-1111-111111111111")
SCOPE = "company"  # I6
OUT = HERE / "analysis" / "r1"
GOLDEN = HERE / "golden" / "routing_holdout.json"

_lock = threading.RLock()


def log(m: str) -> None:
    with _lock:
        print(m, flush=True)


# ─────────────────────────────────────────────────────── C2 — 결정론 규칙 확장

# 현행 규칙이 못 잡는 **수량 표현**. '몇 개/개수/건수'는 이미 잡히지만 '얼마나 되나요'는 안 잡혀
# LLM으로 떨어진다. 이 어구들은 집계 의도가 명확하다(모호하지 않다).
_C2_COUNT_TERMS = (
    "얼마나되",
    "얼마나있",
    "얼마나많",
    "얼마나쌓",
    "총몇",
    "몇건",
    "몇명",
    "몇번",
    "수는얼마",
    "어느정도인가",
    "어느정도나",
)


def _c2_rule(question: str) -> str | None:
    """현행 규칙 → (미발화 시) 확장 수량 신호."""
    base = _rule_based_route(question)
    if base is not None:
        return base
    compact = question.lower().replace(" ", "")
    if any(t in compact for t in _C2_COUNT_TERMS):
        return "structured"
    return None


def route_c2_llm(question: str, chat) -> str:
    """C2-a — 확장 규칙이 안 걸리면 기존대로 LLM(solar)에 묻는다."""
    r = _c2_rule(question)
    return r if r is not None else classify(question, chat_model=chat)


def route_c2_wiki_default(question: str) -> str:
    """C2-c — 확장 규칙이 안 걸리면 **LLM을 부르지 않고 wiki**로 간다.

    사각지대의 정답 분포가 wiki 쪽으로 크게 기울어 있다면(실측), LLM 1콜을 태우는 것보다
    결정론 기본값이 더 정확하고 **0콜·0지연**일 수 있다. 이기면 라우팅에서 LLM이 사라진다.
    """
    r = _c2_rule(question)
    return r if r is not None else "wiki"


# ─────────────────────────────────────────────────────── C1 + C2 측정


def _fold(route: str) -> str:
    """graph는 프로덕션에서 bind 실패 시 wiki로 안전 demote된다(K4b) → 오류 계산상 wiki."""
    return "wiki" if route == "graph" else route


def stage_c1c2() -> None:
    g = json.load(open(GOLDEN, encoding="utf-8"))
    main, control = g["main"], g["control"]

    # I1 — 주 모집단은 전부 규칙 사각지대여야 한다. 아니면 그 문항은 모델과 무관하다.
    bad = [m["id"] for m in main if _rule_based_route(m["q"]) is not None]
    if bad:
        raise SystemExit(f"I1 위반 — 규칙이 결정하는 문항이 주 셋에 섞였다: {bad[:5]} ({len(bad)}건)")
    log(f"[I1] OK — 주 셋 {len(main)}문항 전부 규칙 사각지대")

    pool = build_pool(["baseline", "solar", "ax", "exaone"])
    rows: list[dict] = []
    t0 = time.monotonic()
    done = [0]

    def work(it: dict) -> dict:
        rec = {"id": it["id"], "q": it["q"], "expected": it["expected"], "generator": it["generator"]}
        for slug, chat in pool.items():  # C1
            try:
                rec[f"c1_{slug}"] = classify(it["q"], chat_model=chat)
            except Exception as e:  # noqa: BLE001
                rec[f"c1_{slug}"] = f"error:{type(e).__name__}"
        try:  # C2-a (확장 규칙 + solar LLM)
            rec["c2_llm"] = route_c2_llm(it["q"], pool["solar"])
        except Exception as e:  # noqa: BLE001
            rec["c2_llm"] = f"error:{type(e).__name__}"
        rec["c2_wiki_default"] = route_c2_wiki_default(it["q"])  # C2-c (LLM 0콜)
        done[0] += 1
        if done[0] % 25 == 0:
            el = time.monotonic() - t0
            log(f"    ... {done[0]}/{len(main)}  경과 {el/60:.0f}분  ETA {el/done[0]*(len(main)-done[0])/60:.0f}분")
        return rec

    log(f"[C1+C2] 주 셋 {len(main)}문항 × (4모델 + 규칙2종)")
    with ThreadPoolExecutor(max_workers=4) as ex:
        rows = list(ex.map(work, main))

    # 대조군 — 규칙이 결정하므로 **모델을 바꿔도 결과가 변하면 안 된다**(하네스 자체 검증)
    log(f"[대조군] {len(control)}문항 — 모델 무관이어야 함")
    ctl = []
    for it in control:
        r = {"id": it["id"], "rule_route": it["rule_route"]}
        for slug, chat in pool.items():
            r[f"c1_{slug}"] = classify(it["q"], chat_model=chat)
        ctl.append(r)
    drift = [c for c in ctl if len({c[f"c1_{s}"] for s in pool}) > 1]
    if drift:
        log(f"    ⚠️ 대조군 {len(drift)}건이 모델별로 갈렸다 — 하네스가 규칙을 안 태우고 있다(무효 신호)")
    else:
        log("    ✅ 대조군 전건 모델 무관 — 하네스가 규칙 경로를 정확히 태운다")

    OUT.mkdir(parents=True, exist_ok=True)
    json.dump({"main": rows, "control": ctl}, open(OUT / "c1c2.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log(f"[C1+C2] → {OUT/'c1c2.json'}")


# ─────────────────────────────────────────────────────── C3 — 교차평면 회수


def _run_structured(q: str, chat) -> tuple[str, int, bool]:
    """(본문, row_count, 게이트거부 여부)"""
    try:
        r = query_structured(UID, q, scope=SCOPE, chat_model=chat)
        rejected = not bool(r.validation.passed)
        if r.status != "executed" or not r.rows:
            return "", 0, rejected
        return json.dumps(r.rows, ensure_ascii=False, default=str), int(r.row_count or 0), rejected
    except Exception:  # noqa: BLE001
        return "", 0, True


def _run_wiki(q: str, chat) -> tuple[str, list[str], float]:
    try:
        hits = retrieve(UID, q, k=5, scope=SCOPE)
        if not hits:
            return "", [], 0.0
        top = max((getattr(h, "score", 0.0) or 0.0) for h in hits)
        a = answer_from_hits(UID, q, hits, chat_model=chat, learn=False, record_gaps=False)
        return (a.answer or ""), [s.page_slug for s in a.sources], float(top)
    except Exception:  # noqa: BLE001
        return "", [], 0.0


def stage_c3() -> None:
    """오라우팅된 리프가 **회수되는가**, 그리고 그 대가로 **TN을 환각하는가**.

    C3는 structured가 0행/게이트거부일 때 wiki로 재질의한다. 문제는 0행이 두 가지를 뜻한다는
    것이다 — (a) 평면이 틀렸다 (b) 진짜 0건이다. (b)에 wiki 폴백을 걸면 정답 '없음'이 환각으로
    바뀐다. 그래서 TN 버킷의 환각율(M4)이 0이 아니면 R4로 **즉시 기각**한다.
    """
    g = json.load(open(GOLDEN, encoding="utf-8"))
    main = g["main"]
    tn = json.load(open(OUT / "tn.json", encoding="utf-8")) if (OUT / "tn.json").exists() else []
    c1c2 = json.load(open(OUT / "c1c2.json", encoding="utf-8"))["main"]
    routed = {r["id"]: _fold(r["c1_solar"]) for r in c1c2}

    pool = build_pool(["solar"])
    chat = pool["solar"]
    out: list[dict] = []
    done = [0]
    t0 = time.monotonic()

    def work(it: dict) -> dict:
        r = routed.get(it["id"], "wiki")  # 프로덕션(solar)이 실제로 고른 평면
        rec = {"id": it["id"], "expected": it["expected"], "routed": r, "anchor": it["anchor"]}
        t_s = time.monotonic()
        if r == "structured":
            body, rows, rejected = _run_structured(it["q"], chat)
            rec["structured_rows"] = rows
            rec["gate_rejected"] = rejected
            rec["hit_before"] = anchor_hit(it["anchor"], body, [])
            # C3 트리거: 0행 또는 게이트거부
            rec["c3_fired"] = (rows == 0) or rejected
            if rec["c3_fired"]:
                w_t = time.monotonic()
                ans, srcs, top = _run_wiki(it["q"], chat)
                rec["c3_ms"] = int((time.monotonic() - w_t) * 1000)
                rec["wiki_top_score"] = round(top, 4)
                rec["hit_after"] = anchor_hit(it["anchor"], ans, srcs)
                rec["c3_answer_nonempty"] = bool(ans.strip())
            else:
                rec["hit_after"] = rec["hit_before"]
        else:
            ans, srcs, top = _run_wiki(it["q"], chat)
            rec["wiki_top_score"] = round(top, 4)
            rec["hit_before"] = rec["hit_after"] = anchor_hit(it["anchor"], ans, srcs)
            rec["c3_fired"] = False
        rec["ms"] = int((time.monotonic() - t_s) * 1000)
        done[0] += 1
        if done[0] % 25 == 0:
            el = time.monotonic() - t0
            log(f"    ... {done[0]}/{len(main)}  경과 {el/60:.0f}분")
        return rec

    log(f"[C3] 주 셋 {len(main)}문항 — 프로덕션 라우팅 후 0행/거부면 wiki 재질의")
    with ThreadPoolExecutor(max_workers=4) as ex:
        out = list(ex.map(work, main))

    # ── TN: 정답이 '없음'인 질문. C3가 여기서 그럴듯한 답을 지어내면 R4 즉시 기각.
    tn_out: list[dict] = []
    if tn:
        log(f"[C3-TN] {len(tn)}문항 — 정답='없음'. 환각하면 R4 실패")

        def tn_work(it: dict) -> dict:
            body, rows, rejected = _run_structured(it["q"], chat)
            rec = {"id": it["id"], "q": it["q"], "structured_rows": rows, "gate_rejected": rejected}
            rec["c3_fired"] = (rows == 0) or rejected
            if rec["c3_fired"]:
                ans, srcs, top = _run_wiki(it["q"], chat)
                rec["wiki_top_score"] = round(top, 4)
                rec["wiki_answer"] = ans[:300]
                # 환각 = 위키가 "없음/모름"이 아닌 **실질적 답**을 냈다
                rec["hallucinated"] = bool(ans.strip()) and not _is_refusal(ans)
            return rec

        with ThreadPoolExecutor(max_workers=4) as ex:
            tn_out = list(ex.map(tn_work, tn))

    json.dump({"main": out, "tn": tn_out}, open(OUT / "c3.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log(f"[C3] → {OUT/'c3.json'}")


_REFUSAL = ("찾지 못", "근거 없", "없습니다", "확인할 수 없", "정보가 없", "모르", "해당 없", "0건", "없음")


def _is_refusal(ans: str) -> bool:
    return any(t in ans for t in _REFUSAL)


# ─────────────────────────────────────────────────────── 판정


def _mcnemar(a_ok: list[bool], b_ok: list[bool]) -> tuple[int, int, float]:
    """b가 a보다 나은가. (b만 맞음, a만 맞음, p) — 정확검정(이항)."""
    from math import comb

    b_only = sum(1 for x, y in zip(a_ok, b_ok) if not x and y)
    a_only = sum(1 for x, y in zip(a_ok, b_ok) if x and not y)
    n = b_only + a_only
    if n == 0:
        return b_only, a_only, 1.0
    k = min(b_only, a_only)
    p = min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / (2**n))
    return b_only, a_only, p


def stage_report() -> None:
    c = json.load(open(OUT / "c1c2.json", encoding="utf-8"))
    main = c["main"]
    n = len(main)
    arms = [k[3:] for k in main[0] if k.startswith("c1_")] + ["c2_llm", "c2_wiki_default"]

    def route_of(r: dict, arm: str) -> str:
        return _fold(r[f"c1_{arm}"] if f"c1_{arm}" in r else r[arm])

    print("\n" + "=" * 78)
    print(f"R1 라우팅 홀드아웃 판정 — 주 셋 {n}문항 (규칙 사각지대 × 리프 분포)")
    print("=" * 78)
    exp = [r["expected"] for r in main]
    print(f"\n라벨: wiki {exp.count('wiki')} · structured {exp.count('structured')}")
    print("\n── 지표 (§5 — M1/M2 합산 금지)\n")
    print(f"   {'구성':24s} {'정확도':>8s}  {'M1 치명':>9s}  {'M2 경미':>9s}  {'graph':>6s}")
    print(f"   {'':24s} {'':>8s}  {'wiki→str':>9s}  {'str→wiki':>9s}")
    print("   " + "-" * 66)

    table: dict[str, list[bool]] = {}
    for arm in arms:
        ok, m1, m2, gr = [], 0, 0, 0
        for r in main:
            raw = r[f"c1_{arm}"] if f"c1_{arm}" in r else r[arm]
            got, want = _fold(raw), r["expected"]
            if raw == "graph":
                gr += 1
            ok.append(got == want)
            if want == "wiki" and got == "structured":
                m1 += 1
            if want == "structured" and got == "wiki":
                m2 += 1
        table[arm] = ok
        n_w = exp.count("wiki")
        n_s = exp.count("structured")
        label = {"baseline": "baseline (gpt-4o-mini)", "solar": "solar (현행 배정)"}.get(arm, arm)
        print(f"   {label:24s} {sum(ok)/n*100:7.1f}%  {m1:3d}/{n_w:<3d}{m1/max(1,n_w)*100:4.0f}%  "
              f"{m2:3d}/{n_s:<3d}{m2/max(1,n_s)*100:4.0f}%  {gr:6d}")

    print("\n── R1 게이트: 현행(solar) 대비 유의하게 나은가 (McNemar 정확검정)\n")
    base = table["solar"]
    for arm in arms:
        if arm == "solar":
            continue
        b_only, a_only, p = _mcnemar(base, table[arm])
        verdict = "유의" if p < 0.05 else "유의하지 않음 → 현행 유지"
        print(f"   {arm:20s} 개선 {b_only:3d} · 악화 {a_only:3d} · p={p:.3f}  → {verdict}")

    if (OUT / "c3.json").exists():
        c3 = json.load(open(OUT / "c3.json", encoding="utf-8"))
        m, tn = c3["main"], c3["tn"]
        fired = [r for r in m if r.get("c3_fired")]
        recovered = [r for r in fired if r.get("hit_after") and not r.get("hit_before")]
        print("\n── C3 교차평면 회수\n")
        print(f"   발화        {len(fired)}/{len(m)}  (structured 라우팅 후 0행/게이트거부)")
        print(f"   M3 회수율   {len(recovered)}/{len(fired)} "
              f"({len(recovered)/max(1,len(fired))*100:.0f}%)  ← 오라우팅된 답을 되찾음")
        if fired:
            lat = sorted(r.get("c3_ms", 0) for r in fired)
            print(f"   M5 지연증분 p50 {lat[len(lat)//2]}ms · p95 {lat[int(len(lat)*0.95)-1]}ms")
        if tn:
            hall = [r for r in tn if r.get("hallucinated")]
            tn_fired = [r for r in tn if r.get("c3_fired")]
            print(f"\n   M4 TN 환각  {len(hall)}/{len(tn_fired)} 발화 중 "
                  f"({len(hall)/max(1,len(tn_fired))*100:.0f}%)  ← **0이어야 한다**")
            print(f"   R4 판정     {'✅ PASS' if not hall else '❌ FAIL — 정답 \"없음\"을 지어냈다. 기각.'}")
            for r in hall[:5]:
                print(f"       환각: {r['q'][:52]}\n             → {r.get('wiki_answer','')[:70]}")
    print("\n" + "=" * 78)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["c1c2", "c3", "report"])
    a = ap.parse_args()
    {"c1c2": stage_c1c2, "c3": stage_c3, "report": stage_report}[a.stage]()
