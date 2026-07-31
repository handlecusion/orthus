#!/usr/bin/env python3
"""Combine per-model E2E raw jsonl into a `phase6_verified_stats.json`-shaped
comparison table (`NEW_MODEL_EVAL_HANDOFF.md` §8.1).

What it does, per §8.1:
  1. Build each model's per-id correctness dict from its raw jsonl(s), keeping
     only `score.status in ("pass","fail")` rows, restricted to the tier-A
     comparison universe (ids starting "A-"); the common set is the id
     intersection across all models.
  2. Accuracy = passed/n on `common` (full-scored per model reported too).
  3. Per-task breakdown on `common` (`per_task_on_common`).
  4. Pairwise McNemar + paired bootstrap CI — reusing
     `e2e/runner_lib.py::mcnemar_from_correct` and `bootstrap_paired_diff_ci`
     verbatim (never reimplemented).
  5. Empty-output contamination check on `common`.
  6. Latency p50/p95 on `common` (scored-common) and on `reached_llm=True`
     rows (numpy linear-interpolation percentiles, matching phase6).
  7. Writes the same JSON structure as `analysis/raw/phase6_verified_stats.json`.

t10 honorific re-scoring (§4): every model's t10 rows are re-scored through the
production harness scorer (`harness_e2e.score_l1_exact`, whose t10 branch strips
님/씨 via `_strip_honorific`) using the manifest golden. This is idempotent for
models already scored with the honorific-strip scorer (P6/new large models) and
is what reproduces the P5 domestic numbers whose original raw jsonl predates the
fix.

CLI:
  uv run python experiments/fugu-ko/e2e/combine_stats.py \
    --models solar,exaone,ax,baseline,openai:gpt-4o,glm:glm-5.2,deepseek \
    --out experiments/fugu-ko/analysis/raw/phase6_verified_stats.json

For the seven historical slugs the authoritative sources are not the (possibly
overwritten) `analysis/raw/e2e_{slug}.jsonl` — they are pinned in
`KNOWN_SOURCES` below (P5 domestic = full run in `phase5_full_orthus_db/` with
t3 overridden by the populated-DB rerun in `t3_rerun_orthus_company/`; P6 large =
their single full `analysis/raw/` file). Any slug NOT in `KNOWN_SOURCES`
defaults to `analysis/raw/e2e_{slug}.jsonl`, so a freshly-run new model works
without edits.
"""

from __future__ import annotations

import argparse
import collections
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent          # experiments/fugu-ko/e2e
FUGU = HERE.parent                                # experiments/fugu-ko
REPO = FUGU.parent.parent                         # repo root
RAW = FUGU / "analysis" / "raw"

for _p in (str(REPO), str(FUGU)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from e2e import runner_lib as rl  # noqa: E402  (verbatim stats reuse)
import harness_e2e as H  # noqa: E402  (verbatim t10 scorer reuse)

# --------------------------------------------------------------------------- #
# Source resolution
# --------------------------------------------------------------------------- #
# Historical seven-model sources (paths relative to the analysis/raw dir). P5
# domestic models: full run + t3 rerun override (the rerun is t3-only, keyed by
# id, so listing it second overrides only the t3 rows against populated
# orthus_company). P6 large models: single full file. Slugs absent here fall back
# to the plain `e2e_{slug}.jsonl` default.
KNOWN_SOURCES: dict[str, list[str]] = {
    "solar": ["phase5_full_orthus_db/e2e_solar.jsonl", "t3_rerun_orthus_company/e2e_solar.jsonl"],
    "exaone": ["phase5_full_orthus_db/e2e_exaone.jsonl", "t3_rerun_orthus_company/e2e_exaone.jsonl"],
    "ax": ["phase5_full_orthus_db/e2e_ax.jsonl", "t3_rerun_orthus_company/e2e_ax.jsonl"],
    "baseline": ["phase5_full_orthus_db/e2e_baseline.jsonl", "t3_rerun_orthus_company/e2e_baseline.jsonl"],
    "openai:gpt-4o": ["e2e_openai:gpt-4o.jsonl"],
    "glm:glm-5.2": ["e2e_glm:glm-5.2.jsonl"],
    "deepseek": ["e2e_deepseek.jsonl"],
}


def display_key(slug: str) -> str:
    """Short table key: the part after the first ':' (vendor prefix), else slug.

    `openai:gpt-4o` -> `gpt-4o`, `glm:glm-5.2` -> `glm-5.2`,
    `deepseek:deepseek-v4-pro` -> `deepseek-v4-pro`, `solar` -> `solar`.
    """
    return slug.split(":", 1)[1] if ":" in slug else slug


def sources_for(slug: str, raw_dir: Path) -> list[Path]:
    # Golden-expand (n=324): every slug now has a fresh full run written by the
    # harness to `raw/e2e_{slug}.jsonl`, including the previously-pinned P5/P6
    # slugs. Always resolve there so the merge doesn't fall back to old n=145
    # snapshots (`phase5_full_orthus_db/`, `t3_rerun_orthus_company/`), which
    # would collapse the id intersection back down to ~145. KNOWN_SOURCES is
    # kept above for historical reference only and is intentionally unused.
    return [raw_dir / f"e2e_{slug}.jsonl"]


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(path)
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Row model + scoring
# --------------------------------------------------------------------------- #
def load_t10_golden() -> dict[str, Any]:
    """id -> expected.value for tier-A t10 manifest items (for re-scoring)."""
    golden: dict[str, Any] = {}
    for rec in load_jsonl(rl.TIER_A_PATH):
        if rec.get("task") == "t10":
            golden[rec["id"]] = rec.get("expected", {}).get("value")
    return golden


def load_t3_golden() -> dict[str, Any]:
    """id -> expected.value for tier-A t3 manifest items (for re-scoring).

    Mirrors load_t10_golden. The t3 branch of `harness_e2e.score_l1_exact`
    reads `value.gate_pass` and `value.result` (int-set of counts) and compares
    counts-only against the raw `result.output.rows` — no API re-call needed.
    """
    golden: dict[str, Any] = {}
    for rec in load_jsonl(rl.TIER_A_PATH):
        if rec.get("task") == "t3":
            golden[rec["id"]] = rec.get("expected", {}).get("value")
    return golden


def merge_sources(paths: list[Path]) -> dict[str, dict]:
    """id -> row, later files overriding earlier ones (t3 rerun override)."""
    byid: dict[str, dict] = {}
    for p in paths:
        for row in load_jsonl(p):
            byid[row["id"]] = row
    return byid


def row_output(row: dict) -> Any:
    return (row.get("result") or {}).get("output")


def row_latency(row: dict) -> int:
    lat = row.get("latency_ms")
    if lat is None:
        lat = (row.get("result") or {}).get("latency_ms")
    return lat or 0


def row_reached_llm(row: dict) -> bool:
    return bool((row.get("result") or {}).get("reached_llm"))


def rescored_status(
    row: dict, t10_golden: dict[str, Any], t3_golden: dict[str, Any]
) -> str:
    """Raw status, with t10 (honorific-strip) and t3 (counts-only) re-scored
    through the production harness scorer against the manifest golden. Both use
    only the already-captured `result.output`, so no API re-call is needed and
    it is idempotent for rows already scored under the current scorer."""
    status = row.get("score", {}).get("status", "")
    task = row.get("task")
    if task == "t10":
        ok, _ = H.score_l1_exact("t10", row_output(row), t10_golden.get(row["id"]))
        return "pass" if ok else "fail"
    if task == "t3" and row["id"] in t3_golden:
        # Only re-score rows the harness actually scored (pass/fail); leave
        # error/skip rows untouched, matching the t10 branch's effective scope
        # (its callers gate on status in {"pass","fail"}).
        if status in ("pass", "fail"):
            ok, _ = H.score_l1_exact("t3", row_output(row), t3_golden.get(row["id"]))
            return "pass" if ok else "fail"
    return status


def tier_of(rid: str) -> str:
    return rid.split("-", 1)[0]


def task_of(rid: str, row: dict | None = None) -> str:
    if row and row.get("task"):
        return row["task"]
    parts = rid.split("-")
    return parts[1] if len(parts) > 1 else ""


def is_empty_output(out: Any) -> bool:
    return out is None or out == "" or out == [] or out == {}


def npctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(values, p))


# --------------------------------------------------------------------------- #
# Main combine
# --------------------------------------------------------------------------- #
def combine(slugs: list[str], raw_dir: Path) -> dict:
    t10_golden = load_t10_golden()
    t3_golden = load_t3_golden()
    keys = [display_key(s) for s in slugs]

    # per-model merged rows (id -> row)
    merged: dict[str, dict[str, dict]] = {}
    for slug, key in zip(slugs, keys):
        merged[key] = merge_sources(sources_for(slug, raw_dir))

    # per-model tier-A correctness dict (id -> bool)
    correctness: dict[str, dict[str, bool]] = {}
    for key, byid in merged.items():
        corr: dict[str, bool] = {}
        for rid, row in byid.items():
            if not rid.startswith("A-"):
                continue
            st = rescored_status(row, t10_golden, t3_golden)
            if st in ("pass", "fail"):
                corr[rid] = st == "pass"
        correctness[key] = corr

    common = set.intersection(*[set(c) for c in correctness.values()])
    common_sorted = sorted(common)
    n_common = len(common)

    # (2) accuracy on common + full-scored per model
    accuracy_on_common = {}
    accuracy_on_full = {}
    for key in keys:
        corr = correctness[key]
        passed = sum(1 for i in common if corr[i])
        accuracy_on_common[key] = {
            "passed": passed,
            "n": n_common,
            "accuracy": passed / n_common if n_common else 0.0,
        }
        full_passed = sum(1 for v in corr.values() if v)
        accuracy_on_full[key] = {
            "scored": len(corr),
            "passed": full_passed,
            "accuracy": full_passed / len(corr) if corr else 0.0,
        }

    # (3) per-task breakdown on common
    per_task_on_common = {}
    for key in keys:
        corr = correctness[key]
        agg: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
        for rid in common:
            t = task_of(rid, merged[key].get(rid))
            agg[t][1] += 1
            if corr[rid]:
                agg[t][0] += 1
        per_task_on_common[key] = {t: {"pass": p, "n": n} for t, (p, n) in sorted(agg.items())}

    # ranking
    ranking = sorted(
        ((k, accuracy_on_common[k]["accuracy"]) for k in keys),
        key=lambda kv: kv[1],
        reverse=True,
    )

    # (4) pairwise McNemar + bootstrap CI (verbatim runner_lib)
    pairwise = []
    pair_index = {}
    for a, b in itertools.combinations(keys, 2):
        entry = pair_stats(a, b, correctness)
        pairwise.append(entry)
        pair_index[(a, b)] = entry

    highlight_pairs = {}
    for label, (a, b) in {
        "deepseek_vs_exaone": ("exaone", "deepseek"),
        "deepseek_vs_baseline": ("baseline", "deepseek"),
        "gpt-4o_vs_baseline": ("baseline", "gpt-4o"),
    }.items():
        if a in correctness and b in correctness:
            highlight_pairs[label] = pair_index.get((a, b)) or pair_stats(a, b, correctness)

    # (6) latency
    latency = {}
    for key in keys:
        byid = merged[key]
        all_rows = list(byid.values())
        all_lat = [row_latency(r) for r in all_rows]
        reached_lat = [row_latency(r) for r in all_rows if row_reached_llm(r)]
        scored_common_lat = [row_latency(byid[i]) for i in common if i in byid]
        latency[key] = {
            "scored_common_p50": npctl(scored_common_lat, 50),
            "scored_common_p95": npctl(scored_common_lat, 95),
            "reached_llm_p50": npctl(reached_lat, 50),
            "reached_llm_p95": npctl(reached_lat, 95),
            "reached_llm_n": len(reached_lat),
            "all_rows_p50": npctl(all_lat, 50),
            "all_rows_n": len(all_lat),
            "all_rows_zero_latency": sum(1 for x in all_lat if not x),
        }

    # error triage (models with any error-status row)
    error_triage = {}
    for key in keys:
        rows = list(merged[key].values())
        errs = [r for r in rows if r.get("score", {}).get("status") == "error"]
        if not errs:
            continue
        details = [str(r.get("score", {}).get("detail", "")) for r in errs]
        endpoints = collections.Counter()
        for d in details:
            if "embeddings" in d:
                endpoints["embeddings"] += 1
            elif "chat/completions" in d:
                endpoints["chat_completions"] += 1
            else:
                endpoints["other"] += 1
        cause = "429_rate_limit" if all("429" in d for d in details) else "mixed"
        err_ids = {r["id"] for r in errs}
        error_triage[key] = {
            "n_error": len(errs),
            "by_task": dict(collections.Counter(r["task"] for r in errs)),
            "by_tier": dict(collections.Counter(tier_of(r["id"]) for r in errs)),
            "cause": cause,
            "endpoint": dict(endpoints),
            "error_x_scored_overlap": len(err_ids & common),
            "status_counts": dict(
                collections.Counter(r.get("score", {}).get("status", "") for r in rows)
            ),
        }

    # (5) empty-output contamination check on common
    empty_content_check = {}
    for key in keys:
        byid = merged[key]
        corr = correctness[key]
        empty_ids = sorted(i for i in common if is_empty_output(row_output(byid[i])))
        empty_content_check[key] = {
            "n_empty_output_in_common_scored": len(empty_ids),
            "empty_ids_sample": empty_ids,
            "empty_are_all_fail": bool(empty_ids) and all(not corr[i] for i in empty_ids),
        }

    # all-models-fail set. NB: phase6's stored `known_fail_8` was a hand-curated
    # highlight of the 8 GENUINE cross-model failures (t5/t6/t7) discussed in the
    # report; this derives the FULL cross-model all-fail set generically — a
    # strict superset that also contains the t3 env-artifact all-fails (empty-DB,
    # handoff §1) and any other all-fail ids. The phase6 8 are always a subset.
    all_fail_ids = sorted(i for i in common if all(not correctness[k][i] for k in keys))
    known_fail = {
        "ids": all_fail_ids,
        "per_model_status": {
            k: {i: ("pass" if correctness[k][i] else "fail") for i in all_fail_ids} for k in keys
        },
        "all_models_fail_all_8": bool(all_fail_ids)
        and all(not correctness[k][i] for k in keys for i in all_fail_ids),
    }

    # scored-set identity (domestic vs large split, mirrors phase6 p5/p6 keys).
    # golden-expand: the old fixed count in the key name (145) is stale — every
    # model now scores the identical id set of size `n_common_scored` (1750 at
    # the 851->1884 tier_a expansion). The key was renamed to carry the current
    # n; the boolean meaning ("all large models scored the identical id set")
    # is unchanged.
    domestic = [k for k, s in zip(keys, slugs) if not H._is_large_slug(s)]
    large = [k for k, s in zip(keys, slugs) if H._is_large_slug(s)]
    large_sets = [frozenset(correctness[k]) for k in large]
    scored_set_identity = {
        "p6_large_models_identical_scored_set": len(large_sets) > 0
        and len(set(large_sets)) == 1,
        "large_identical_scored_set_size": len(large_sets[0]) if large_sets else 0,
        "common_covers_all_p6": all(set(correctness[k]) <= common | set(correctness[k]) for k in large)
        and all(common == set(correctness[k]) for k in large),
        "p5_full_scored_n": {k: len(correctness[k]) for k in domestic},
        "p6_full_scored_n": {k: len(correctness[k]) for k in large},
    }

    return {
        "models": keys,
        "n_common_scored": n_common,
        "accuracy_on_common": accuracy_on_common,
        "per_task_on_common": per_task_on_common,
        "accuracy_on_full_scored_per_model": accuracy_on_full,
        "ranking_on_common": [[k, a] for k, a in ranking],
        "pairwise_mcnemar_on_common": pairwise,
        "highlight_pairs": highlight_pairs,
        "latency": latency,
        "error_triage": error_triage,
        "empty_content_check": empty_content_check,
        "known_fail_8": known_fail,
        "scored_set_identity": scored_set_identity,
        "_common_ids": common_sorted,
    }


def pair_stats(a: str, b: str, correctness: dict[str, dict[str, bool]]) -> dict:
    a_corr, b_corr = correctness[a], correctness[b]
    mc = rl.mcnemar_from_correct(a_corr, b_corr)
    ci = rl.bootstrap_paired_diff_ci(a_corr, b_corr, n_resamples=10000, seed=1234)
    ids = sorted(set(a_corr) & set(b_corr))
    n = len(ids)
    acc_a = sum(1 for i in ids if a_corr[i]) / n if n else 0.0
    acc_b = sum(1 for i in ids if b_corr[i]) / n if n else 0.0
    return {
        "a": a,
        "b": b,
        "n_paired": mc["n_paired"],
        "a_only_correct": mc["a_only"],
        "b_only_correct": mc["b_only"],
        "discordant": mc["discordant"],
        "mcnemar_p": mc["p_value"],
        "significant_p05": mc["significant"],
        "acc_diff_a_minus_b": acc_a - acc_b,
        "bootstrap_diff_ci95": [ci[0], ci[1]],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", required=True, help="comma-separated slugs")
    ap.add_argument("--out", required=True, help="output JSON path")
    ap.add_argument("--raw-dir", default=str(RAW), help="analysis/raw directory")
    ap.add_argument(
        "--keep-common-ids",
        action="store_true",
        help="keep the diagnostic _common_ids list in the output JSON",
    )
    args = ap.parse_args()

    slugs = [s.strip() for s in args.models.split(",") if s.strip()]
    raw_dir = Path(args.raw_dir)
    stats = combine(slugs, raw_dir)
    if not args.keep_common_ids:
        stats.pop("_common_ids", None)

    out = Path(args.out)
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=2), "utf-8")
    print(f"wrote {out}  (models={stats['models']}, n_common={stats['n_common_scored']})")
    for k, acc in stats["ranking_on_common"]:
        pn = stats["accuracy_on_common"][k]
        print(f"  {k:16s} {pn['passed']:3d}/{pn['n']}  {acc:.4f}")


if __name__ == "__main__":
    main()
