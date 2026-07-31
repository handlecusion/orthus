#!/usr/bin/env python3
"""Slot-swap composite experiment: domestic_best (best domestic-model-per-slot)
vs current_production (the REAL, current main `orthus/models/orchestration.py`
ASSIGNMENTS as of PR #4, merged 2026-07-21 — the "diversified" table).

**2026-07-21 correction:** this script previously compared domestic_best
against a STALE `true_baseline` (all-solar-except-t10, pre-PR#4). PR #4
changed the actual production ASSIGNMENTS to the diversified table
(t3=solar, t5=exaone, t6=solar, t7=exaone, t9=ax, t10=exaone) — which is
*identical* to what this script already called `KNOWN_DIVERSIFIED_ASSIGNMENT`
(previously used only as a sanity-gate fixture, never compared against
domestic_best). The comparison that now matters is domestic_best (121/145)
vs current_production (118/145) — i.e. candidate vs the real, current,
already-diversified production. The old true_baseline/domestic_best
comparison is retained in the output JSON under a clearly-labeled stale key
for history, not deleted.

Reuses `e2e/combine_stats.py` (source loading, per-id correctness, t10
honorific rescoring) and `e2e/runner_lib.py` (`mcnemar_from_correct`,
`bootstrap_paired_diff_ci`) verbatim — no stats reimplementation.

Step 1 is a sanity gate: reproduce the already-published diversified-table
composite (118/145, vs-baseline mcnemar_p=0.5488, CI=[-0.0207, 0.069] from
`analysis/raw/orchestration_composite_9model.json`) using this script's own
data-loading path. Because `KNOWN_DIVERSIFIED_ASSIGNMENT` IS
`current_production`, this sanity gate directly validates the
current_production composite before the new domestic_best vs
current_production comparison is trusted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # experiments/fugu-ko/analysis
FUGU = HERE.parent                                # experiments/fugu-ko
REPO = FUGU.parent.parent                         # repo root
E2E = FUGU / "e2e"
RAW = FUGU / "analysis" / "raw"

for p in (str(REPO), str(FUGU), str(E2E)):
    if p not in sys.path:
        sys.path.insert(0, p)

from e2e import combine_stats as cs  # noqa: E402
from e2e import runner_lib as rl  # noqa: E402

# All 9 models measured in the shared 145-question E2E benchmark.
SLUGS_9 = [
    "solar",
    "exaone",
    "ax",
    "baseline",
    "openai:gpt-4o",
    "glm:glm-5.2",
    "deepseek",
    "openai:gpt-5.3-chat-latest",
    "deepseek:deepseek-v4-pro",
]
KEYS_9 = [cs.display_key(s) for s in SLUGS_9]

KNOWN_DIVERSIFIED_ASSIGNMENT = {
    "t3": "solar",
    "t5": "exaone",
    "t6": "solar",
    "t7": "exaone",
    "t9": "ax",
    "t10": "exaone",
}
# golden-expand re-measurement: n=145 -> n=324 (2026-07-21) -> n=717 (2026-07-22,
# round 2) -> n=1750 (2026-07-22, round 3: full aug golden expansion, tier_a
# 851->1884, 11-model rerun + t3 counts-only rescore in combine_stats). Prior
# fixtures were n=145 (118, 0.5488, (-0.0207, 0.069)), n=324 (264, 0.1338,
# (-0.00309, 0.05247)), and n=717 (623, 0.0001168, (0.01674, 0.04881)); updated
# to the reproduced n=1750 numbers below (seeded bootstrap, deterministic).
# NB the 9-model slot_swap common set now equals the 11-model common set (1750)
# — every model scored the identical id set.
KNOWN_DIVERSIFIED_COMPOSITE_PASS = 1449
KNOWN_DIVERSIFIED_VS_BASELINE_MCNEMAR_P = 2.9809838675814455e-16
KNOWN_DIVERSIFIED_VS_BASELINE_CI = (0.044, 0.072)

# STALE (pre-PR#4) main production assignment, per `orthus/models/orchestration.py`
# on `main` as it existed BEFORE PR #4 merged (2026-07-21): ALL solar except
# t10=exaone. PR #4 replaced this with the diversified table below. Kept only
# for the stale historical comparison recorded in the output JSON.
STALE_PRE_PR4_BASELINE_ASSIGNMENT = {
    "t3": "solar",
    "t5": "solar",
    "t6": "solar",
    "t7": "solar",
    "t9": "solar",
    "t10": "exaone",
}

# CURRENT REAL production assignment, per `orthus/models/orchestration.py` on
# `main` AFTER PR #4 merged (2026-07-21, confirmed via
# `git show origin/main:orthus/models/orchestration.py`). This is identical to
# `KNOWN_DIVERSIFIED_ASSIGNMENT` above (same table, same sanity-gate fixture) —
# named separately here for clarity in the new comparison this script exists
# to run.
CURRENT_PRODUCTION_ASSIGNMENT = {
    "t3": "solar",
    "t5": "exaone",
    "t6": "solar",
    "t7": "exaone",
    "t9": "ax",
    "t10": "exaone",
}
assert CURRENT_PRODUCTION_ASSIGNMENT == KNOWN_DIVERSIFIED_ASSIGNMENT

# Domestic best-per-slot, ties broken to current (true_baseline) assignment:
#   t3: exaone strictly best among domestic (solar 13 < exaone 15)
#   t5: ax strictly best among domestic (ax 19 > solar/exaone 18)
#   t6: 3-way tie ax/exaone/solar @ 19 -> tie-break to current (solar)
#   t7: exaone strictly best among domestic (exaone 15 > solar 14 > ax 11)
#   t9: tie ax/solar @ 32 -> tie-break to current (solar)
#   t10: exaone strictly best among domestic (exaone 21 > ax 18 > solar 16)
DOMESTIC_BEST_ASSIGNMENT = {
    "t3": "exaone",
    "t5": "ax",
    "t6": "solar",
    "t7": "exaone",
    "t9": "solar",
    "t10": "exaone",
}


def load_correctness_and_merged() -> tuple[dict[str, dict[str, bool]], dict[str, dict[str, dict]]]:
    """Per-model tier-A correctness dict + merged raw rows, mirroring
    `combine_stats.combine()` lines ~173-192 (t10 honorific rescoring included).
    """
    t10_golden = cs.load_t10_golden()
    t3_golden = cs.load_t3_golden()
    merged: dict[str, dict[str, dict]] = {}
    for slug, key in zip(SLUGS_9, KEYS_9):
        merged[key] = cs.merge_sources(cs.sources_for(slug, RAW))

    correctness: dict[str, dict[str, bool]] = {}
    for key, byid in merged.items():
        corr: dict[str, bool] = {}
        for rid, row in byid.items():
            if not rid.startswith("A-"):
                continue
            st = cs.rescored_status(row, t10_golden, t3_golden)
            if st in ("pass", "fail"):
                corr[rid] = st == "pass"
        correctness[key] = corr
    return correctness, merged


def task_lookup(common_ids: list[str], merged: dict[str, dict[str, dict]]) -> dict[str, str]:
    """id -> task, using whichever model's merged row is available for that id."""
    out: dict[str, str] = {}
    for rid in common_ids:
        row = None
        for byid in merged.values():
            if rid in byid:
                row = byid[rid]
                break
        out[rid] = cs.task_of(rid, row)
    return out


def build_composite(
    assignment: dict[str, str],
    common_ids: list[str],
    id_task: dict[str, str],
    correctness: dict[str, dict[str, bool]],
) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for rid in common_ids:
        task = id_task[rid]
        model_key = assignment[task]
        out[rid] = correctness[model_key][rid]
    return out


def per_task_pass(
    assignment: dict[str, str],
    common_ids: list[str],
    id_task: dict[str, str],
    correctness: dict[str, dict[str, bool]],
) -> dict[str, dict]:
    agg: dict[str, list[int]] = {t: [0, 0] for t in assignment}
    for rid in common_ids:
        t = id_task[rid]
        agg[t][1] += 1
        if correctness[assignment[t]][rid]:
            agg[t][0] += 1
    return {t: {"model": assignment[t], "pass": p, "n": n} for t, (p, n) in agg.items()}


def composite_summary(
    assignment: dict[str, str],
    common_ids: list[str],
    id_task: dict[str, str],
    correctness: dict[str, dict[str, bool]],
) -> dict:
    corr = build_composite(assignment, common_ids, id_task, correctness)
    passed = sum(1 for v in corr.values() if v)
    n = len(corr)
    return {
        "assignment": assignment,
        "passed": passed,
        "n": n,
        "accuracy": passed / n if n else 0.0,
        "per_task": per_task_pass(assignment, common_ids, id_task, correctness),
        "_correct": corr,  # stripped before saving to file
    }


def compare(a_corr: dict[str, bool], b_corr: dict[str, bool]) -> dict:
    mc = rl.mcnemar_from_correct(a_corr, b_corr)
    ci = rl.bootstrap_paired_diff_ci(a_corr, b_corr, n_resamples=10000, seed=1234)
    return {
        "n_paired": mc["n_paired"],
        "a_only": mc["a_only"],
        "b_only": mc["b_only"],
        "discordant": mc["discordant"],
        "mcnemar_p": mc["p_value"],
        "significant_p05": mc["significant"],
        "bootstrap_diff_ci95": [ci[0], ci[1]],
    }


def main() -> None:
    correctness, merged = load_correctness_and_merged()
    common = sorted(set.intersection(*[set(c) for c in correctness.values()]))
    print(f"n_common_scored = {len(common)}")
    id_task = task_lookup(common, merged)

    # --- Sanity gate: reproduce the known diversified-table composite -------
    known = composite_summary(KNOWN_DIVERSIFIED_ASSIGNMENT, common, id_task, correctness)
    known_corr = known.pop("_correct")
    baseline_corr = correctness["baseline"]
    known_vs_baseline = compare(known_corr, baseline_corr)

    sanity_pass_ok = known["passed"] == KNOWN_DIVERSIFIED_COMPOSITE_PASS
    sanity_mcnemar_ok = abs(known_vs_baseline["mcnemar_p"] - KNOWN_DIVERSIFIED_VS_BASELINE_MCNEMAR_P) < 1e-4
    sanity_ci_ok = (
        abs(known_vs_baseline["bootstrap_diff_ci95"][0] - KNOWN_DIVERSIFIED_VS_BASELINE_CI[0]) < 1e-4
        and abs(known_vs_baseline["bootstrap_diff_ci95"][1] - KNOWN_DIVERSIFIED_VS_BASELINE_CI[1]) < 1e-4
    )
    sanity_ok = sanity_pass_ok and sanity_mcnemar_ok and sanity_ci_ok

    print("=== SANITY GATE (reproduce known diversified-table numbers) ===")
    print(f"  known composite passed:   got={known['passed']}/{len(common)}  want={KNOWN_DIVERSIFIED_COMPOSITE_PASS}/{len(common)}  match={sanity_pass_ok}")
    print(f"  known vs-baseline mcnemar_p: got={known_vs_baseline['mcnemar_p']}  want={KNOWN_DIVERSIFIED_VS_BASELINE_MCNEMAR_P}  match={sanity_mcnemar_ok}")
    print(f"  known vs-baseline CI: got={known_vs_baseline['bootstrap_diff_ci95']}  want={list(KNOWN_DIVERSIFIED_VS_BASELINE_CI)}  match={sanity_ci_ok}")
    print(f"  SANITY GATE OVERALL: {'PASS' if sanity_ok else 'FAIL'}")

    if not sanity_ok:
        print(
            "\nSANITY GATE FAILED — cannot reproduce known published numbers with this "
            "script's data-loading path. Refusing to report new true_baseline / "
            "domestic_best numbers as reliable.",
            file=sys.stderr,
        )

    # --- STALE (pre-PR#4): domestic_best vs the old, no-longer-current --------
    # baseline. Recomputed here (not just copied from the prior run's JSON) so
    # the stale block in the output is generated by the same code path as the
    # corrected block, for an apples-to-apples record.
    stale_baseline = composite_summary(STALE_PRE_PR4_BASELINE_ASSIGNMENT, common, id_task, correctness)
    stale_baseline_corr = stale_baseline.pop("_correct")

    domestic_best = composite_summary(DOMESTIC_BEST_ASSIGNMENT, common, id_task, correctness)
    domestic_best_corr = domestic_best.pop("_correct")

    domestic_vs_stale = compare(domestic_best_corr, stale_baseline_corr)

    print("\n=== STALE (pre-PR#4): baseline = all-solar-except-t10=exaone ===")
    print(f"  passed = {stale_baseline['passed']}/{stale_baseline['n']}  ({stale_baseline['accuracy']:.4f})")
    for t, d in stale_baseline["per_task"].items():
        print(f"    {t}: {d['pass']}/{d['n']} ({d['model']})")
    print("\n  stale domestic_best vs stale pre-PR#4 baseline (McNemar + bootstrap CI):")
    print(f"  {json.dumps(domestic_vs_stale, ensure_ascii=False)}")

    # --- CORRECTED: domestic_best vs current_production (post-PR#4, real) ---
    # current_production IS KNOWN_DIVERSIFIED_ASSIGNMENT (same table), so its
    # composite/correctness is exactly `known`/`known_corr` computed above in
    # the sanity gate — reused directly rather than recomputed a second time.
    current_production = dict(known)
    current_production["assignment"] = CURRENT_PRODUCTION_ASSIGNMENT
    current_production_corr = known_corr

    domestic_best_vs_current = compare(domestic_best_corr, current_production_corr)

    print("\n=== NEW: domestic_best (domestic best-per-slot candidate) ===")
    print(f"  passed = {domestic_best['passed']}/{domestic_best['n']}  ({domestic_best['accuracy']:.4f})")
    for t, d in domestic_best["per_task"].items():
        print(f"    {t}: {d['pass']}/{d['n']} ({d['model']})")

    print("\n=== NEW: current_production (REAL main ASSIGNMENTS post-PR#4) ===")
    print(f"  passed = {current_production['passed']}/{current_production['n']}  ({current_production['accuracy']:.4f})")
    for t, d in current_production["per_task"].items():
        print(f"    {t}: {d['pass']}/{d['n']} ({d['model']})")

    print("\n=== *** THE comparison that matters now ***: domestic_best vs current_production ===")
    print(json.dumps(domestic_best_vs_current, indent=2, ensure_ascii=False))

    out = {
        "method": (
            "post-hoc synthetic composite (no LLM calls), same construction as "
            "orchestration_composite_9model.json. Reuses e2e/combine_stats.py + "
            "e2e/runner_lib.py (mcnemar_from_correct, bootstrap_paired_diff_ci) "
            "verbatim, no stats reimplementation."
        ),
        "correction_note": (
            "2026-07-21: PR #4 merged into main and replaced the production "
            "ASSIGNMENTS with the diversified table (t3=solar, t5=exaone, "
            "t6=solar, t7=exaone, t9=ax, t10=exaone). This file's prior run "
            "compared domestic_best against a STALE pre-PR#4 baseline "
            "(all-solar-except-t10). That comparison is preserved below under "
            "'stale_comparison_vs_pre_pr4_main' for history. The comparison "
            "that now matters — domestic_best vs the REAL, current, "
            "post-PR#4 production assignment — is under "
            "'corrected_comparison_vs_current_main'. Note current_production "
            "here is identical to this script's long-standing "
            "KNOWN_DIVERSIFIED_ASSIGNMENT sanity-gate fixture (118/145), so "
            "the sanity gate below directly validates the current_production "
            "composite used in the corrected comparison."
        ),
        "measured_on": "2026-07-21",
        "n_common_scored": len(common),
        "sanity_gate": {
            "known_diversified_assignment": KNOWN_DIVERSIFIED_ASSIGNMENT,
            "reproduced_composite_passed": known["passed"],
            "expected_composite_passed": KNOWN_DIVERSIFIED_COMPOSITE_PASS,
            "reproduced_vs_baseline_mcnemar_p": known_vs_baseline["mcnemar_p"],
            "expected_vs_baseline_mcnemar_p": KNOWN_DIVERSIFIED_VS_BASELINE_MCNEMAR_P,
            "reproduced_vs_baseline_ci95": known_vs_baseline["bootstrap_diff_ci95"],
            "expected_vs_baseline_ci95": list(KNOWN_DIVERSIFIED_VS_BASELINE_CI),
            "overall_pass": sanity_ok,
        },
        "domestic_best": domestic_best,
        "stale_comparison_vs_pre_pr4_main": {
            "description": (
                "STALE (superseded 2026-07-21 by PR #4): domestic_best vs the "
                "pre-PR#4 main production assignment (all-solar-except-t10). "
                "This baseline is no longer what main actually runs."
            ),
            "true_baseline": stale_baseline,
            "domestic_best_vs_true_baseline": domestic_vs_stale,
        },
        "corrected_comparison_vs_current_main": {
            "description": (
                "domestic_best (121/145) vs current_production (118/145) — "
                "the REAL, current main orthus/models/orchestration.py "
                "ASSIGNMENTS as of PR #4 (merged 2026-07-21). This is the "
                "comparison that actually matters now."
            ),
            "current_production": current_production,
            "domestic_best_vs_current_production": domestic_best_vs_current,
        },
        "new_numbers_trustworthy": sanity_ok,
    }

    out_path = FUGU / "analysis" / "orchestration_composite_slot_swap_exp.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), "utf-8")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
