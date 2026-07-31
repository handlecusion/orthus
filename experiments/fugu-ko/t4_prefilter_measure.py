"""T4 프리필터 recall/precision 측정 — Stage 1만, LLM 0회, DB 0회.

프로덕션 프리필터의 실경로(`_has_command_verb` + `_has_connective_or_enum(ext_tier=T)`)를
직접 호출해 tier를 스윕한다. e3_prefilter.py의 `reached()` 패턴을 그대로 쓰되 LLM 게이트/
split은 부르지 않는다(신호 도달률만 측정).

usage: python experiments/fugu-ko/t4_prefilter_measure.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from orthus.router.decompose import _has_command_verb, _has_connective_or_enum  # noqa: E402

# 골든/probe 데이터는 untracked라 worktree에 없을 수 있다. 있으면 worktree, 없으면 main
# 체크아웃에서 읽는다(read-only 입력; 테스트하는 프로덕션 코드는 위 import = worktree).
_MAIN = Path("<repo>")


def _data_dir(name: str) -> Path:
    # untracked 골든/probe가 worktree에 다 없을 수 있어, 완전한 main 체크아웃을 우선한다.
    main = _MAIN / "experiments/fugu-ko" / name
    return main if main.exists() else (HERE / name)


GOLDEN = _data_dir("golden")
ANALYSIS = _data_dir("analysis")

# 진단 문서(prefilter-gap-diagnosis.md §3)의 실패모드 분류.
MODE = {
    "A": {"h2-c07", "h2-c09", "h2-c13", "h2-c17", "h2-c24", "h2-c29", "h2-c31", "h2-c36", "h2-c39"},
    "C": {"h2-c56", "h2-c57", "h2-c58", "h2-c59", "h2-c60"},
    "B": {"h2-c05", "h2-c08"},
    "D": {"h2-c33"},
    "E": {"h2-c34"},
}


def reached(q: str, tier: int) -> bool:
    compact = q.lower().replace(" ", "")
    return _has_command_verb(compact) or _has_connective_or_enum(q, ext_tier=tier)


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({(n / d * 100 if d else 0):.1f}%)"


def load(path: Path):
    return json.load(open(path, encoding="utf-8"))


def main() -> None:
    probe = [
        json.loads(line)
        for line in (ANALYSIS / "prefilter_gap_probe.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    e3missed = load(GOLDEN / "e3_missed.json")["items"]
    control = load(GOLDEN / "e3_control.json")["items"]
    t7 = load(GOLDEN / "t7.json")["items"]
    h2 = load(GOLDEN / "t7_holdout2.json")
    h2items = h2["items"] if isinstance(h2, dict) else h2
    man = load(GOLDEN / "t7_holdout2_manifest.json")["id_to_arm"]
    h2trap = [it for it in h2items if man.get(it["id"]) == "trap"]

    print("=" * 70)
    print("RECALL — 게이트 도달률 (reached_llm), q_original 기준 (LLM 0회)")
    print("=" * 70)

    print("\n[26-probe q_original] tier별 도달")
    print(f"{'tier':<6}{'reached':>16}")
    for tier in (0, 1, 2, 3, 4):
        r = sum(reached(p["q_original"], tier) for p in probe)
        print(f"{tier:<6}{pct(r, len(probe)):>16}")

    print("\n[26-probe 모드별 tier3 vs tier4]")
    print(f"{'mode':<6}{'n':>4}{'tier3':>14}{'tier4':>14}")
    for m, ids in MODE.items():
        sub = [p for p in probe if p["id"] in ids]
        r3 = sum(reached(p["q_original"], 3) for p in sub)
        r4 = sum(reached(p["q_original"], 4) for p in sub)
        print(f"{m:<6}{len(sub):>4}{pct(r3, len(sub)):>14}{pct(r4, len(sub)):>14}")
    r3 = sum(reached(p["q_original"], 3) for p in probe)
    r4 = sum(reached(p["q_original"], 4) for p in probe)
    print(f"{'ALL':<6}{len(probe):>4}{pct(r3, len(probe)):>14}{pct(r4, len(probe)):>14}")
    miss4 = [p["id"] for p in probe if not reached(p["q_original"], 4)]
    print(f"  tier4 잔여 미도달: {miss4}")

    print("\n[e3_missed.json (n=40)] tier3 vs tier4 (tier3에서 이미 100% — 회귀 없음 확인)")
    r3 = sum(reached(it["q"], 3) for it in e3missed)
    r4 = sum(reached(it["q"], 4) for it in e3missed)
    print(f"  tier3 {pct(r3, len(e3missed))}   tier4 {pct(r4, len(e3missed))}")

    print("\n" + "=" * 70)
    print("PRECISION — 오발화(프리필터 오통과) 비율. Stage1 통과=LLM에 위임이라")
    print("최종 오답이 아니라 LLM 콜 낭비(지연). 낮을수록 좋음.")
    print("=" * 70)

    print("\n[e3_control.json (n=40, 전부 단일질문)] tier3 vs tier4")
    c3 = sum(reached(it["q"], 3) for it in control)
    c4 = sum(reached(it["q"], 4) for it in control)
    print(f"  tier3 {pct(c3, len(control))}   tier4 {pct(c4, len(control))}")
    fired3 = {it["id"] for it in control if reached(it["q"], 3)}
    fired4 = {it["id"] for it in control if reached(it["q"], 4)}
    print(f"  tier4 신규 오발화(증분): {sorted(fired4 - fired3)}")

    print("\n[t7.json (n=22)] tier3 vs tier4 — compound=false 만 오발화로 집계")
    t7f = [it for it in t7 if not it.get("compound")]
    print(f"  (compound=false subset n={len(t7f)})")
    c3 = sum(reached(it["q"], 3) for it in t7f)
    c4 = sum(reached(it["q"], 4) for it in t7f)
    print(f"  tier3 {pct(c3, len(t7f))}   tier4 {pct(c4, len(t7f))}")

    print("\n" + "=" * 70)
    print("HOLDOUT 확인 (1회, 튜닝 미사용) — H2 trap arm 60 (전부 단일질문)")
    print("=" * 70)
    h3 = sum(reached(it["q"], 3) for it in h2trap)
    h4 = sum(reached(it["q"], 4) for it in h2trap)
    print(f"  tier3 오발화 {pct(h3, len(h2trap))}   tier4 오발화 {pct(h4, len(h2trap))}")
    fired4 = [it["id"] for it in h2trap if reached(it["q"], 4) and not reached(it["q"], 3)]
    print(f"  tier4 신규 오발화(증분): {fired4}")


if __name__ == "__main__":
    main()
