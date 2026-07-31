"""Layer-2 head-to-head stats for the DeepSeek V4 Pro / GLM-5.2 arms (2026-07-23).

Reads the fresh raws (`analysis/raw/e2e_l2_dsv4_deepseek:deepseek-v4-pro.jsonl`,
`analysis/raw/e2e_l2_glm52_glm:glm-5.2.jsonl`) plus the frozen B1 Layer-2
snapshots (`analysis/raw/b1/layer2/raw_*.jsonl`), reconstructs the domestic
orchestration composite (slots per analysis/b1-layer2-orchestration.md:
t3:solar t5:exaone t6:solar t7:solar t9:solar t10:exaone), and prints the
per-task pass/n matrix + exact McNemar vs composite / gpt-5.3 / sonnet.

Untracked analysis helper — no commits.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent  # analysis/
FUGU = HERE.parent
sys.path.insert(0, str(FUGU))

from e2e.runner_lib import bootstrap_paired_diff_ci, mcnemar_from_correct  # noqa: E402

RAW = HERE / "raw"
B1 = RAW / "b1" / "layer2"

SLOTS = {"t3": "solar", "t5": "exaone", "t6": "solar", "t7": "solar", "t9": "solar", "t10": "exaone"}
TASK_ORDER = ["t3", "t5", "t6", "t7", "t9", "t10"]


def load(path: Path) -> dict[str, dict]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        out[r["id"]] = r
    return out


def correct_map(rows: dict[str, dict]) -> dict[str, bool]:
    return {
        i: r["score"]["status"] == "pass"
        for i, r in rows.items()
        if r["score"]["status"] in ("pass", "fail")
    }


def per_task(cm: dict[str, bool], rows: dict[str, dict]) -> dict[str, tuple[int, int]]:
    agg: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for i, ok in cm.items():
        t = rows[i]["task"]
        agg[t][1] += 1
        agg[t][0] += 1 if ok else 0
    return {t: (v[0], v[1]) for t, v in agg.items()}


def latencies(rows: dict[str, dict], cm: dict[str, bool]) -> list[int]:
    return sorted(rows[i]["latency_ms"] for i in cm if rows[i].get("latency_ms") is not None)


def p50p95(lat: list[int]) -> tuple[int, int]:
    if not lat:
        return (0, 0)
    return (lat[len(lat) // 2], lat[min(len(lat) - 1, int(len(lat) * 0.95))])


def main() -> None:
    new_arms = {
        "deepseek-v4-pro": RAW / "e2e_l2_dsv4_deepseek:deepseek-v4-pro.jsonl",
        "glm-5.2": RAW / "e2e_l2_glm52_glm:glm-5.2.jsonl",
    }
    refs = {
        "solar": B1 / "raw_solar.jsonl",
        "exaone": B1 / "raw_exaone.jsonl",
        "gpt-5.3": B1 / "raw_gpt53.jsonl",
        "sonnet-4.6": B1 / "raw_sonnet.jsonl",
        "baseline": B1 / "raw_baseline.jsonl",
        "gpt-4o": B1 / "raw_gpt4o.jsonl",
    }

    rows_by = {}
    cms = {}
    for slug, path in {**refs, **new_arms}.items():
        rows = load(path)
        rows_by[slug] = rows
        cms[slug] = correct_map(rows)

    # Composite from frozen solar/exaone snapshots (per-task slots).
    comp: dict[str, bool] = {}
    for t, src in SLOTS.items():
        for i, ok in cms[src].items():
            if rows_by[src][i]["task"] == t:
                comp[i] = ok
    cms["composite"] = comp
    rows_by["composite"] = {i: rows_by["solar"][i] for i in comp}

    print("== per-task pass/n (scored set) ==")
    for slug in ["composite", "gpt-5.3", "sonnet-4.6", "deepseek-v4-pro", "glm-5.2"]:
        pt = per_task(cms[slug], rows_by[slug])
        cells = "  ".join(f"{t}:{pt.get(t, (0, 0))[0]}/{pt.get(t, (0, 0))[1]}" for t in TASK_ORDER)
        total = sum(v for v, _ in pt.values())
        n = sum(n_ for _, n_ in pt.values())
        lat = latencies(rows_by[slug], cms[slug])
        p50, p95 = p50p95(lat)
        print(f"{slug:<16} {cells}  TOTAL {total}/{n} ({total / n:.4f})  p50 {p50}ms p95 {p95}ms")

    print("\n== paired McNemar (exact) + bootstrap CI ==")
    for arm in new_arms:
        for ref in ["composite", "gpt-5.3", "sonnet-4.6", "baseline", "solar", "exaone"]:
            m = mcnemar_from_correct(cms[arm], cms[ref])
            lo, hi = bootstrap_paired_diff_ci(cms[arm], cms[ref])
            print(
                f"{arm} vs {ref:<12} n={m['n_paired']:<4} {arm}-only={m['a_only']:<3} "
                f"{ref}-only={m['b_only']:<3} p={m['p_value']:.4f} "
                f"sig={m['significant']}  CI[{lo:+.3f},{hi:+.3f}]"
            )
        print()

    # sanity: paired id-set sizes
    inter = set(cms["composite"])
    for arm in new_arms:
        d = set(cms[arm]) ^ inter
        print(f"[sanity] {arm}: scored={len(cms[arm])} composite∩Δ={len(d)}")


if __name__ == "__main__":
    main()
