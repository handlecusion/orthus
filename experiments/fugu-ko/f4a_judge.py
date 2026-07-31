"""F4a email-prompt A/B judge — reuses the validated pairwise protocol verbatim.

Battles (position-swap 2-way, gpt-4o validated judge, error/parse=tie):
  1) solar_f4a vs claude-sonnet-4-6   (주판정 — prereg §3.2)
  2) solar_f4a vs solar(구프롬프트)    (부판정 A/B — prereg §3.3)
Question rendering matches arena_judge email battles: to + inst (+ctx).
"""

from __future__ import annotations

import json
import sys
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))
RAW = HERE / "raw"

from judge.pairwise import _SYS, _prompt  # noqa: E402


def load(name: str) -> dict[str, dict]:
    p = RAW / f"arena_email_{name}.jsonl"
    return {r["id"]: r for r in map(json.loads, p.read_text().splitlines())}


def render_q(it: dict) -> str:
    ctx = f"\n[참고 자료]\n{it['ctx']}" if it.get("ctx") else ""
    return f"받는 사람: {it['to']}\n요청: {it['inst']}{ctx}"


def render_a(it: dict) -> str:
    return f"제목: {it['subject']}\n\n{it['body']}"


def judge_once(judge, q: str, a: str, b: str) -> str:
    try:
        out = judge.complete(_SYS, _prompt(q, a, b), json_only=True)
        w = str(json.loads(out).get("winner", "tie")).strip().lower()
        return {"a": "A", "b": "B", "tie": "tie"}.get(w, "tie")
    except Exception:  # noqa: BLE001 — 판정 오류는 tie (프로토콜 그대로)
        return "tie"


def battle(judge, ours: dict, other: dict, label: str) -> None:
    w = t = losses = 0
    for iid in sorted(ours):
        q = render_q(ours[iid])
        a, b = render_a(ours[iid]), render_a(other[iid])
        v1 = judge_once(judge, q, a, b)  # ours=A
        v2 = judge_once(judge, q, b, a)  # swap: ours=B
        v2 = {"A": "B", "B": "A", "tie": "tie"}[v2]
        if v1 == v2 == "A":
            w += 1
        elif v1 == v2 == "B":
            losses += 1
        else:
            t += 1
    dec = w + losses
    p = min(1.0, sum(comb(dec, k) for k in range(min(w, losses) + 1)) / 2**dec * 2) if dec else 1.0
    rate = 100 * w / dec if dec else 0.0
    print(f"{label}: W{w} T{t} L{losses}  decided승률 {rate:.0f}% ({w}/{dec})  p={p:.4f}")


def main() -> None:
    from arena_judge import _build_gpt4o_judge  # noqa: E402 — 검증 판정자 빌더 재사용

    judge = _build_gpt4o_judge()
    f4a, old, sonnet = load("solar_f4a"), load("solar"), load("claude-sonnet-4-6")
    import sys as _s

    which = _s.argv[1] if len(_s.argv) > 1 else "all"
    if which in ("all", "main"):
        battle(judge, f4a, sonnet, "solar-F4a vs Sonnet ")
        battle(judge, f4a, old, "solar-F4a vs solar-구")
    if which in ("all", "control"):
        battle(judge, old, sonnet, "대조: solar-구 vs Sonnet")
    if which == "frontiers":
        for name, label in (
            ("claude-opus-4-5", "solar-F4a vs Opus 4.5"),
            ("claude-opus-4-6", "solar-F4a vs Opus 4.6"),
            ("claude-opus-4-8", "solar-F4a vs Opus 4.8"),
            ("gpt-5.3", "solar-F4a vs gpt-5.3 "),
        ):
            battle(judge, f4a, load(name), label)


def battle_counts(judge, a_gen: dict, b_gen: dict) -> tuple[int, int, int]:
    """(a_wins, ties, b_wins) — battle()과 동일 프로토콜, 집계만 반환."""
    w = t = losses = 0
    for iid in sorted(a_gen):
        q = render_q(a_gen[iid])
        a, b = render_a(a_gen[iid]), render_a(b_gen[iid])
        v1 = judge_once(judge, q, a, b)
        v2 = judge_once(judge, q, b, a)
        v2 = {"A": "B", "B": "A", "tie": "tie"}[v2]
        if v1 == v2 == "A":
            w += 1
        elif v1 == v2 == "B":
            losses += 1
        else:
            t += 1
    return w, t, losses


def round_robin() -> None:
    """8모델 풀 라운드로빈 → 평균 승점(승+0.5무). 결과는 pair별 JSONL 캐시에 append
    (재실행 시 기측정 쌍 스킵 — 판정 비용 절약 + 재현성)."""
    from arena_judge import _build_gpt4o_judge  # noqa: E402

    roster = [
        ("solar_f4a", "F4a(현행 조립)"),
        ("solar", "solar 구프롬프트"),
        ("exaone", "exaone 구배정"),
        ("claude-sonnet-4-6", "Sonnet 4.6"),
        ("claude-opus-4-5", "Opus 4.5"),
        ("claude-opus-4-6", "Opus 4.6"),
        ("claude-opus-4-8", "Opus 4.8"),
        ("gpt-5.3", "gpt-5.3"),
        ("gpt-5.6-sol", "gpt-5.6-sol†(codex)"),
    ]
    cache = RAW / "f4a_roundrobin.jsonl"
    done: dict[tuple[str, str], tuple[int, int, int]] = {}
    if cache.exists():
        for line in cache.read_text().splitlines():
            r = json.loads(line)
            done[(r["a"], r["b"])] = (r["w"], r["t"], r["l"])
    judge = _build_gpt4o_judge()
    gens = {slug: load(slug) for slug, _ in roster}
    for i, (a, _) in enumerate(roster):
        for b, _ in roster[i + 1 :]:
            if (a, b) in done or (b, a) in done:
                continue
            w, t, losses = battle_counts(judge, gens[a], gens[b])
            done[(a, b)] = (w, t, losses)
            with cache.open("a") as fh:
                fh.write(json.dumps({"a": a, "b": b, "w": w, "t": t, "l": losses}) + "\n")
            print(f"  {a} vs {b}: W{w} T{t} L{losses}")
    score: dict[str, list[float]] = {slug: [] for slug, _ in roster}
    for (a, b), (w, t, losses) in done.items():
        n = w + t + losses
        score[a].append((w + 0.5 * t) / n)
        score[b].append((losses + 0.5 * t) / n)
    label = dict(roster)
    print("\n== 라운드로빈 평균 승점 (0-100) ==")
    for slug, vals in sorted(score.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        print(f"  {label[slug]:20s} {100 * sum(vals) / len(vals):.1f}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "roundrobin":
        round_robin()
    else:
        main()
