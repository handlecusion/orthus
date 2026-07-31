"""B1 Layer-2 orchestration stitch: domestic composite vs frontier singles.

Reads per-model snapshot raw files under `analysis/raw/b1/layer2/raw_<slug>.jsonl`
(each line: {id, task, layer, kind, score:{status,...}, latency_ms, ...}).

- Per-task pass/n matrix (model x task), on model-independently scored items.
- Domestic composite: per task pick best domestic (solar/exaone/ax) by pass-rate,
  take that model's per-item correctness for the task's items -> stitched.
- Head-to-head totals (composite vs gpt-4o / sonnet / baseline / each domestic).
- Paired McNemar composite-vs-frontier on the intersection of scored ids.

No writes outside stdout. Run:
  ../../.venv/bin/python b1_layer2_stitch.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAP = HERE / "analysis" / "raw" / "b1" / "layer2"

TASKS = ["t3", "t5", "t6", "t7", "t9", "t10"]
DOMESTIC = ["solar", "exaone", "ax"]
FRONTIER = ["gpt4o", "gpt53", "sonnet"]

SLUG_LABEL = {
    "solar": "solar (Upstage Solar-pro)",
    "exaone": "exaone (LG EXAONE)",
    "ax": "ax (SKT A.X-K1)",
    "gpt4o": "gpt-4o (frontier)",
    "gpt53": "gpt-5.3-chat-latest (frontier, temp=1 vendor-forced)",
    "sonnet": "Claude Sonnet 4.6 (frontier)",
    "baseline": "gpt-4o-mini (baseline/prod)",
}


def load(slug: str) -> dict[str, dict] | None:
    p = SNAP / f"raw_{slug}.jsonl"
    if not p.exists():
        return None
    rows: dict[str, dict] = {}
    for line in p.open():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        rows[o["id"]] = o
    return rows


def correct_map(rows: dict[str, dict]) -> dict[str, bool]:
    """id -> bool for pass/fail scored items only (deferred/error excluded)."""
    out: dict[str, bool] = {}
    for _id, o in rows.items():
        st = o["score"]["status"]
        if st in ("pass", "fail"):
            out[_id] = st == "pass"
    return out


def task_of(rows: dict[str, dict], _id: str) -> str:
    return rows[_id]["task"]


def mcnemar(a: dict[str, bool], b: dict[str, bool]) -> dict:
    """Paired McNemar on intersection of scored ids. a,b: id->correct."""
    ids = sorted(set(a) & set(b))
    b01 = sum(1 for i in ids if (not a[i]) and b[i])   # a wrong, b right
    b10 = sum(1 for i in ids if a[i] and (not b[i]))   # a right, b wrong
    n_disc = b01 + b10
    # exact binomial two-sided p on discordant pairs (small-n honest)
    if n_disc == 0:
        p = 1.0
    else:
        k = min(b01, b10)
        p = 0.0
        for j in range(0, k + 1):
            p += math.comb(n_disc, j) * (0.5 ** n_disc)
        p = min(1.0, 2 * p)
    # continuity-corrected chi2 for reference
    chi2 = ((abs(b01 - b10) - 1) ** 2) / n_disc if n_disc else 0.0
    return {
        "n_paired": len(ids),
        "a_right_b_wrong": b10,
        "b_right_a_wrong": b01,
        "discordant": n_disc,
        "p_exact_binom": p,
        "chi2_cc": chi2,
    }


def main() -> None:
    models = ["solar", "exaone", "ax", "gpt4o", "gpt53", "sonnet", "baseline"]
    raw = {m: load(m) for m in models}
    present = [m for m in models if raw[m] is not None]
    missing = [m for m in models if raw[m] is None]
    print("# B1 Layer-2 orchestration stitch\n")
    print(f"present models: {present}")
    if missing:
        print(f"MISSING (not yet run): {missing}")
    print()

    cmap = {m: correct_map(raw[m]) for m in present}
    # scored id universe (should be identical across models); use intersection
    scored_sets = [set(cmap[m]) for m in present]
    common = set.intersection(*scored_sets) if scored_sets else set()
    union = set.union(*scored_sets) if scored_sets else set()
    print(f"scored ids: common={len(common)} union={len(union)} "
          f"(identical across models: {len(common) == len(union)})\n")

    # any model providing task lookup
    ref_rows = raw[present[0]]
    ids_by_task = {t: sorted(i for i in common if ref_rows.get(i, {}).get("task") == t)
                   for t in TASKS}

    # ---- per-task pass/n matrix ----
    print("## Per-task pass/n matrix (model x task, model-independently scored)\n")
    header = ["model"] + [f"{t}(n={len(ids_by_task[t])})" for t in TASKS] + ["TOTAL"]
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    task_pass = {}  # (model, task) -> (pass, n)
    for m in present:
        cells = []
        tot_p = tot_n = 0
        for t in TASKS:
            ids = ids_by_task[t]
            p = sum(1 for i in ids if cmap[m][i])
            n = len(ids)
            task_pass[(m, t)] = (p, n)
            tot_p += p
            tot_n += n
            cells.append(f"{p}/{n} ({p/n*100:.0f}%)" if n else "-")
        cells.append(f"**{tot_p}/{tot_n} ({tot_p/tot_n*100:.1f}%)**")
        print(f"| {SLUG_LABEL.get(m, m)} | " + " | ".join(cells) + " |")
    print()

    # ---- composite slot assignment ----
    print("## Domestic composite — slot assignment (best domestic per task)\n")
    dom_present = [m for m in DOMESTIC if m in present]
    slot = {}
    print("| task | " + " | ".join(dom_present) + " | winner |")
    print("|" + "---|" * (len(dom_present) + 2))
    for t in TASKS:
        rates = {}
        for m in dom_present:
            p, n = task_pass[(m, t)]
            rates[m] = (p / n) if n else 0.0
        # winner = highest pass-rate; tie-break by fewer-latency? keep first (solar,exaone,ax order)
        best = max(dom_present, key=lambda m: (rates[m], -DOMESTIC.index(m)))
        slot[t] = best
        cells = [f"{task_pass[(m,t)][0]}/{task_pass[(m,t)][1]}" for m in dom_present]
        print(f"| {t} | " + " | ".join(cells) + f" | **{best}** |")
    print()
    print(f"slot assignment: {slot}\n")

    # composite per-item correctness (over common scored ids)
    comp = {}
    for t in TASKS:
        m = slot[t]
        for i in ids_by_task[t]:
            comp[i] = cmap[m][i]
    comp_pass = sum(1 for v in comp.values() if v)
    comp_n = len(comp)
    print(f"COMPOSITE total: {comp_pass}/{comp_n} ({comp_pass/comp_n*100:.1f}%)\n")

    # ---- head-to-head totals ----
    print("## Head-to-head totals (over common scored set)\n")
    print("| model | pass/n | acc |")
    print("|---|---|---|")
    print(f"| **국내 composite (orchestration)** | {comp_pass}/{comp_n} | "
          f"**{comp_pass/comp_n*100:.1f}%** |")
    rows_tot = []
    for m in present:
        p = sum(1 for i in common if cmap[m][i])
        rows_tot.append((m, p, len(common)))
        print(f"| {SLUG_LABEL.get(m, m)} | {p}/{len(common)} | {p/len(common)*100:.1f}% |")
    print()

    # ---- McNemar composite vs each frontier + baseline ----
    print("## Paired McNemar — composite vs frontier / baseline\n")
    print("| comparison | n | comp_right_other_wrong | other_right_comp_wrong | "
          "discordant | p(exact) | verdict |")
    print("|---|---|---|---|---|---|---|")
    for m in [x for x in FRONTIER + ["baseline"] if x in present]:
        r = mcnemar(comp, cmap[m])
        verdict = "n.s." if r["p_exact_binom"] > 0.05 else (
            "composite better" if r["a_right_b_wrong"] > r["b_right_a_wrong"] else "other better")
        print(f"| composite vs {m} | {r['n_paired']} | {r['a_right_b_wrong']} | "
              f"{r['b_right_a_wrong']} | {r['discordant']} | {r['p_exact_binom']:.3f} | {verdict} |")
    print()

    # ---- pairwise composite vs each single (incl domestic singles) ----
    print("## McNemar — composite vs each single model\n")
    print("| comparison | comp_right_other_wrong | other_right_comp_wrong | disc | p |")
    print("|---|---|---|---|---|")
    for m in present:
        r = mcnemar(comp, cmap[m])
        print(f"| composite vs {m} | {r['a_right_b_wrong']} | {r['b_right_a_wrong']} | "
              f"{r['discordant']} | {r['p_exact_binom']:.3f} |")
    print()

    # ---- e3 aggregate note for t7 (deferred probe items) ----
    print("## t7 e3 aggregate (deferred probe items — deterministic prefilter)\n")
    for m in present:
        rows = raw[m]
        missed = [o for o in rows.values() if o["task"] == "t7"
                  and o["score"]["status"] == "deferred"]
        print(f"  {m}: {len(missed)} deferred t7 probe items (fold into "
              f"missed_recall/mis_split, reported 0.0/0.0 in run stdout)")
    print()

    # ---- latency p50 per model ----
    print("## Latency p50 (ms, scored items)\n")
    for m in present:
        lat = sorted(o.get("latency_ms", 0) for o in raw[m].values()
                     if o["score"]["status"] in ("pass", "fail"))
        p50 = lat[len(lat) // 2] if lat else 0
        print(f"  {m}: p50={p50}ms")


if __name__ == "__main__":
    main()
