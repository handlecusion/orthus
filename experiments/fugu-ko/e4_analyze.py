"""E4 집계/진단 — 이종 judge 패널 다수결 (신규 LLM 콜 0회). 계획: analysis/e4-judge-panel-plan.md

입력(전부 기존 원자료):
- analysis/raw/t2_judge.jsonl        gpt-4o 단독 판정 (기준선 — 재사용)
- analysis/raw/t2_panel_judge.jsonl  국내 judge 2종 판정 (judge/panel.py 산출)

집계(§4.2 사전 고정):
- judge-level verdict = 양방향 일치 결과(win/loss/tie) — 각 judge가 이미 그 규칙으로 산출.
- panel verdict = 3 judge(gpt-4o + 국내 2종) 다수결. 3자 3색(win/loss/tie 각 1) → tie.
- 승률 = 승/(승+패) (기존 정의 동일), 패널 verdict 기준 재계산.

진단(§4.2):
① judge 쌍별 일치율 + Cohen's kappa(3-way, tie 포함)
② judge별 decisiveness(비-tie 비율) — A.X 짧은 출력 성향이 tie 남발로 나타나는지
③ gpt-4o 단독 대비 패널 verdict 플립 문항 (사람 스팟체크 대상)
④ judge별 방향 불일치율(위치 편향 프록시) + json 실패율(§5 무효 조건 >10%)

게이트(§5 사전 고정):
- H-E4a: 패널 승률에서도 Solar·EXAONE ≥ 40%(수용선) & 순위 Solar ≥ EXAONE > A.X 유지 → 결론 견고
- H-E4b: 국내 judge vs gpt-4o 일치율 ≥ 70% & kappa ≥ 0.4 → "국내 judge 실용 가능"

실행: PYTHONPATH=<repo> python experiments/fugu-ko/e4_analyze.py
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / "analysis" / "raw"
WORKERS = ["solar", "ax", "exaone"]
CATS = ["win", "loss", "tie"]
GPT = "gpt-4o"

# §4.1 패널 구성 — judge ∉ 판정 쌍
PANEL: dict[str, list[str]] = {
    "solar": [GPT, "exaone", "ax"],
    "exaone": [GPT, "solar", "ax"],
    "ax": [GPT, "solar", "exaone"],
}
ACCEPT = 40.0  # 수용선 승률(%)


def _rows(name: str) -> list[dict]:
    p = RAW / name
    if not p.exists():
        raise SystemExit(f"원자료 없음: {p} — judge/panel.py 를 먼저 실행")
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()]


def load_verdicts(panel_file: str = "t2_panel_judge.jsonl") -> dict[tuple[str, str, str], dict]:
    """(judge, worker, id) → row. gpt-4o는 기존 t2_judge.jsonl(judge 필드 없음)에서 승격.

    같은 키가 여러 번 나오면 **마지막 row가 이긴다**(실패 판정 재시도분이 원 실패 row를 대체).
    """
    v: dict[tuple[str, str, str], dict] = {}
    for r in _rows("t2_judge.jsonl"):
        v[(GPT, r["worker"], r["id"])] = {**r, "judge": GPT}
    for r in _rows(panel_file):
        v[(r["judge"], r["worker"], r["id"])] = r
    return v


def majority(votes: list[str]) -> str:
    """3 judge 다수결. 3자 3색 → tie (§4.2)."""
    c = {k: votes.count(k) for k in CATS}
    top = max(c.values())
    winners = [k for k in CATS if c[k] == top]
    return winners[0] if len(winners) == 1 else "tie"


def kappa(a: list[str], b: list[str]) -> float:
    """Cohen's kappa (3-category, tie 포함). 완전 일치=1, 우연 수준=0."""
    n = len(a)
    if n == 0:
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum((a.count(c) / n) * (b.count(c) / n) for c in CATS)
    return 1.0 if pe == 1.0 else (po - pe) / (1 - pe)


def winrate(tally: dict[str, int]) -> float:
    dec = tally["win"] + tally["loss"]
    return (tally["win"] / dec * 100) if dec else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--panel",
        default="t2_panel_judge.jsonl",
        help="패널 판정 원자료 파일명(analysis/raw 기준). 독립 재실행분 재현성 확인용.",
    )
    args = ap.parse_args()
    V = load_verdicts(args.panel)
    qids = sorted({k[2] for k in V})
    judges = [GPT, "solar", "exaone", "ax"]

    # ── 무효 조건(§5): json/호출 실패율 > 10% 인 judge는 패널에서 강등
    fail = defaultdict(lambda: [0, 0])  # judge → [실패콜, 총콜]
    for (j, _w, _q), r in V.items():
        if j == GPT:
            continue  # 기존 판정은 실패 텔레메트리 없음(원 스크립트가 예외를 tie로 흡수)
        for k in ("err_fwd", "err_rev"):
            fail[j][1] += 1
            if r.get(k):
                fail[j][0] += 1
    demoted = {j for j, (e, t) in fail.items() if t and e / t > 0.10}

    print("=" * 78)
    print("E4 — 이종 judge 패널(PoLL) 교차검증  ·  신규 LLM 콜 0회(원자료 재생)")
    print("=" * 78)
    print(f"\n판정 쌍 3(각 워커 vs baseline gpt-4o-mini) × 문항 {len(qids)} · judge ∉ 판정 쌍")

    # ── ④ judge 신뢰도 (위치 편향 / 실패율 = 무효 조건)
    # 위치 편향 = 양방향 모두 단정했는데 **같은 자리**를 골랐다(A,A 또는 B,B) → 내용이 아니라 위치를 봤다.
    # 부분 불일치 = 한 방향만 단정. 양방향 tie = 진짜 무승부(편향 아님).
    print("\n[④ judge 신뢰도] — 위치 편향(양방향 단정인데 같은 자리 선택) / 호출·파싱 실패율")
    print(f"  {'judge':8} {'판정수':>5} {'위치편향':>10} {'부분불일치':>10} {'양방향tie':>9} {'실패콜':>10}  비고")
    for j in judges:
        rs = [r for (jj, _w, _q), r in V.items() if jj == j]
        if not rs:
            continue
        pos = sum(1 for r in rs if r["v_fwd"] == r["v_rev"] != "tie")
        part = sum(1 for r in rs if (r["v_fwd"] == "tie") != (r["v_rev"] == "tie"))
        btie = sum(1 for r in rs if r["v_fwd"] == r["v_rev"] == "tie")
        e, t = fail[j] if j in fail else (0, 0)
        frate = f"{e}/{t} ({e / t * 100:.0f}%)" if t else "n/a"
        note = "패널 강등(>10%)" if j in demoted else ""
        print(
            f"  {j:8} {len(rs):>5} {pos:>5}/{len(rs)} ({pos / len(rs) * 100:>3.0f}%)"
            f" {part:>5}/{len(rs)} {btie:>7}/{len(rs)} {frate:>12}  {note}"
        )

    # ── ② decisiveness
    print("\n[② decisiveness] — 비-tie 비율 (낮으면 그 judge는 사실상 기권)")
    print(f"  {'judge':8} {'승':>3} {'패':>3} {'무':>3}  {'비-tie':>7}")
    for j in judges:
        rs = [r for (jj, _w, _q), r in V.items() if jj == j]
        if not rs:
            continue
        c = {k: sum(1 for r in rs if r["result"] == k) for k in CATS}
        dec = (c["win"] + c["loss"]) / len(rs) * 100
        flag = "  ← 기권 수준(<20%)" if dec < 20 else ""
        print(f"  {j:8} {c['win']:>3} {c['loss']:>3} {c['tie']:>3}  {dec:>6.0f}%{flag}")

    # ── ① judge 쌍별 일치율 + kappa
    print("\n[① judge 간 일치] — 겹치는 문항에서만 (judge∉쌍 규칙상 국내끼리는 30문항 겹침)")
    print(f"  {'judge A':8} {'judge B':8} {'n':>3} {'일치율':>7} {'kappa':>7}   해석")
    e4b: dict[str, tuple[float, float, int]] = {}
    for a, b in combinations(judges, 2):
        common = sorted({(w, q) for (j, w, q) in V if j == a} & {(w, q) for (j, w, q) in V if j == b})
        if not common:
            continue
        va = [V[(a, w, q)]["result"] for w, q in common]
        vb = [V[(b, w, q)]["result"] for w, q in common]
        agr = sum(x == y for x, y in zip(va, vb)) / len(common) * 100
        k = kappa(va, vb)
        interp = "높음" if k >= 0.6 else ("중등(≥0.4)" if k >= 0.4 else ("약함" if k >= 0.2 else "우연 수준"))
        print(f"  {a:8} {b:8} {len(common):>3} {agr:>6.0f}% {k:>7.2f}   {interp}")
        if a == GPT:
            e4b[b] = (agr, k, len(common))

    # ── ⑤ 판정 쌍 × judge 매트릭스 — "국내 judge가 국내 워커 편을 드는가"(패널이 새 편향을 들여왔는지)
    print("\n[⑤ 쌍별 judge 편향] — 같은 쌍을 본 judge끼리 비교해야 공정(judge마다 담당 쌍이 다름)")
    print(f"  {'판정 쌍':22} {'judge':8} {'승':>3} {'패':>3} {'무':>3}  {'워커 승률':>8}")
    for w in WORKERS:
        for j in PANEL[w]:
            rs = [V[(j, w, q)] for q in qids if (j, w, q) in V]
            if not rs:
                continue
            c = {k: sum(1 for r in rs if r["result"] == k) for k in CATS}
            mark = "  ← 기준(gpt-4o)" if j == GPT else ""
            print(
                f"  {f'{w} vs baseline':22} {j:8} {c['win']:>3} {c['loss']:>3} {c['tie']:>3}"
                f"  {winrate(c):>7.0f}%{mark}"
            )
        print()

    # ── ⑥ E2 F4 교차검증 — "헤징 선호"는 gpt-4o의 취향인가, 쌍대비교의 구조적 문제인가
    # E2(F4)는 judge가 내용이 아니라 헤징(유보 표현)에 점수를 준다고 보고했으나 **judge가 gpt-4o 하나뿐**이라
    # 벤더 취향인지 방법론 결함인지 가릴 수 없었다. 이종 패널이 그 구분을 가능하게 한다.
    probe = RAW / "e2_anchor_probe.jsonl"
    if probe.exists():
        refused = {  # 기준선이 거절문을 낸 문항 = "거절 vs 시도" 비교가 된 문항
            r["kid"] for r in (json.loads(x) for x in probe.read_text("utf-8").splitlines() if x.strip())
            if r["refusal"]["baseline"]["extended"]
        }
        print(f"\n[⑥ 헤징 선호 교차검증] — 기준선 거절 {len(refused)}문항 vs 실답변 {len(qids) - len(refused)}문항 (E2 F4)")
        print("  판정 쌍을 고정해 **같은 답변쌍을 본 judge끼리만** 비교한다(judge마다 담당 쌍이 달라 전체 집계는 교란).")
        print("  Δ = 실답변 문항 승률 − 기준선 거절 문항 승률. 양수 = 기준선의 거절문에 후하다(= 헤징 선호).")
        for w in WORKERS:
            print(f"\n  [{w} vs baseline]  {'judge':8} {'거절문':>16} {'실답변':>16}       Δ")
            for j in PANEL[w]:
                cells = []
                for subset in (refused, set(qids) - refused):
                    rs = [V[(j, w, q)] for q in subset if (j, w, q) in V]
                    c = {k: sum(1 for r in rs if r["result"] == k) for k in CATS}
                    dec = c["win"] + c["loss"]
                    cells.append((winrate(c) if dec else float("nan"), c["win"], c["loss"]))
                (wr_r, w_r, l_r), (wr_a, w_a, l_a) = cells
                gap = wr_a - wr_r
                print(
                    f"  {'':20} {j:8} {wr_r:>7.0f}% ({w_r}승{l_r}패) {wr_a:>7.0f}% ({w_a}승{l_a}패)"
                    f"  {gap:>+6.0f}p"
                )
        print("\n  → 같은 쌍에서 Δ가 gpt-4o에만 크면 '벤더 취향(self-preference)', 다른 벤더 judge에서도 재현되면")
        print("    E2 F4가 지목한 것은 벤더 취향이 아니라 **judge 공통의 근거-우선 규범**이다.")

    # ── 승률 재계산: gpt-4o 단독 vs 패널 다수결
    solo = {w: {k: 0 for k in CATS} for w in WORKERS}
    panel = {w: {k: 0 for k in CATS} for w in WORKERS}
    flips: list[tuple[str, str, str, str, list[str]]] = []
    missing = 0
    for w in WORKERS:
        for q in qids:
            g = V.get((GPT, w, q))
            if not g:
                continue
            solo[w][g["result"]] += 1
            members = [j for j in PANEL[w] if j not in demoted]
            votes = [V[(j, w, q)]["result"] for j in members if (j, w, q) in V]
            if len(votes) < len(members):
                missing += 1
                continue
            pv = majority(votes)
            panel[w][pv] += 1
            if pv != g["result"]:
                flips.append((w, q, g["result"], pv, votes))

    print("\n[승률 재계산] — 기존 정의(승/(승+패)) · 패널 = 3 judge 다수결, 3자 3색→tie")
    print(f"  {'worker':8} | {'gpt-4o 단독':>22} | {'이종 3-패널':>22} | {'Δ승률':>7}")
    print(f"  {'':8} | {'승':>3} {'패':>3} {'무':>3} {'승률':>8} | {'승':>3} {'패':>3} {'무':>3} {'승률':>8} |")
    wr_solo, wr_panel = {}, {}
    for w in WORKERS:
        s, p = solo[w], panel[w]
        wr_solo[w], wr_panel[w] = winrate(s), winrate(p)
        d = wr_panel[w] - wr_solo[w]
        print(
            f"  {w:8} | {s['win']:>3} {s['loss']:>3} {s['tie']:>3} {wr_solo[w]:>7.0f}% "
            f"| {p['win']:>3} {p['loss']:>3} {p['tie']:>3} {wr_panel[w]:>7.0f}% | {d:>+6.0f}p"
        )
    if missing:
        print(f"  (판정 누락으로 패널 집계 제외: {missing}문항 — panel.py 재실행 필요)")

    # ── ③ 플립 문항
    print(f"\n[③ 플립 문항] — gpt-4o 단독 → 패널에서 결과가 바뀐 문항 ({len(flips)}건, 사람 스팟체크 대상)")
    if not flips:
        print("  없음 — 패널이 gpt-4o 단독 판정을 전 문항에서 재확인")
    for w, q, before, after, votes in flips:
        vs = ", ".join(f"{j}={V[(j, w, q)]['result']}" for j in PANEL[w] if (j, w, q) in V)
        print(f"  {w:7} {q:7} {before:>4} → {after:<4}  ({vs})")

    # ── 게이트 판정 (§5)
    print("\n" + "=" * 78)
    print("게이트 판정 (§5 사전 고정)")
    print("=" * 78)

    acc = {w: wr_panel[w] >= ACCEPT for w in ("solar", "exaone")}
    rank_ok = wr_panel["solar"] >= wr_panel["exaone"] > wr_panel["ax"]
    e4a = all(acc.values()) and rank_ok
    print("\nH-E4a (결론 견고성):")
    for w in ("solar", "exaone"):
        print(f"  · {w} 패널 승률 {wr_panel[w]:.0f}% ≥ 40% 수용선 … {'PASS' if acc[w] else 'FAIL'}")
    print(
        f"  · 순위 Solar({wr_panel['solar']:.0f}%) ≥ EXAONE({wr_panel['exaone']:.0f}%) > "
        f"A.X({wr_panel['ax']:.0f}%) … {'PASS' if rank_ok else 'FAIL'}"
    )
    shift = max(abs(wr_panel[w] - wr_solo[w]) for w in WORKERS)
    if e4a and shift < 15:
        print("  ⇒ 지지 — 패널 교차검증 통과. 보고서 8.1에 'judge 패널 교차검증 통과' 기록.")
    elif e4a:
        print(f"  ⇒ 부분 뒤집힘 — 순위/수용선은 유지하나 승률 최대 이동 {shift:.0f}%p ≥ 15%p. 6.1/6.6에 패널 값 병기.")
    else:
        print("  ⇒ 뒤집힘 — 수용선/순위 붕괴. 플립 문항 전수 사람 스팟체크 후 8.1 전면 개정,")
        print("     배정표(wiki_qa→solar) 근거까지 재검토.")

    print("\nH-E4b (국내 judge 가능성): gpt-4o와 일치율 ≥70% & kappa ≥0.4")
    any_ok = False
    for j, (agr, k, n) in sorted(e4b.items()):
        ok = agr >= 70 and k >= 0.4
        any_ok |= ok
        print(f"  · {j:7} 일치율 {agr:>3.0f}% / kappa {k:>5.2f} (n={n}) … {'PASS — 실용 가능' if ok else 'FAIL'}")
    print(f"  ⇒ {'국내 judge 실용 가능(위 PASS 모델) — E2 §4.2 패널 규약으로 승계' if any_ok else '국내 judge 단독 대체는 아직 불가 — gpt-4o 앵커 유지'}")
    if demoted:
        print(f"\n무효 조건 발동: {sorted(demoted)} 실패율 >10% → 해당 쌍은 2-패널로 강등 집계했다.")


if __name__ == "__main__":
    main()
