"""M7 — Agentic Loop Bench scorer (prereg: analysis/m7-prereg.md §5–§6).

Consumes raw/m7_<slug>.jsonl + golden/m7_tasks.json, applies the frozen
deterministic checks (m7_env.check_task) and the prereg failure-attribution
rules, and writes the per-model table + paired McNemar tests to stdout (the
results doc is authored from this output).

  .venv/bin/python experiments/fugu-ko/m7_score.py --models solar,exaone,claude-sonnet-4-6,ax
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from m7_env import check_task  # noqa: E402

GOLDEN = HERE / "golden" / "m7_tasks.json"
RAW_DIR = HERE / "raw"

_NEGATION = ["없", "찾지 못", "확인되지 않", "확인할 수 없", "모른", "조회되지 않", "할 수 없"]


def attribute(task: dict, rec: dict, completed: bool) -> str:
    """Deterministic failure attribution (prereg §6 priority order)."""
    if completed:
        return "-"
    if rec["status"] == "api_error":
        return "api_error"
    if rec["n_ok_tool_calls"] == 0 and rec["n_tool_calls"] == 0:
        return "no_tool_use"
    if rec["status"] == "max_turns":
        return "max_turns"
    if rec["n_tool_calls"] > 0 and rec["n_format_failures"] * 2 >= rec["n_tool_calls"]:
        return "tool_format"
    if task["id"] != "V3" and any(n in (rec["final_text"] or "") for n in _NEGATION):
        return "gave_up"
    return "wrong_path"


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar over discordant pairs (b, c)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def score_model(slug: str, tasks_by_id: dict) -> tuple[dict, dict]:
    path = RAW_DIR / f"m7_{slug}.jsonl"
    if not path.exists():
        return {}, {}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    per_task: dict[str, dict] = {}
    for rec in rows:
        task = tasks_by_id[rec["task_id"]]
        completed, failed_checks = check_task(
            task, rec.get("final_text") or "", rec.get("end_state") or {}, rec["n_ok_tool_calls"]
        )
        if (
            rec["status"] != "final"
            and rec["status"] != "max_turns"
            and rec["status"] != "api_error"
        ):
            raise ValueError(f"unknown status {rec['status']}")
        if rec["status"] == "max_turns":
            completed = False  # prereg §4: turn-cap exhaustion is a failure
        if rec["status"] == "api_error":
            completed = False
        per_task[rec["task_id"]] = {
            "completed": completed,
            "failed_checks": failed_checks,
            "attr": attribute(task, rec, completed),
            "n_llm_calls": rec["n_llm_calls"],
            "n_tool_calls": rec["n_tool_calls"],
            "n_format_failures": rec["n_format_failures"],
            "kind": task["kind"],
        }
    n = len(per_task)
    done = [t for t in per_task.values() if t["completed"]]
    summary = {
        "n": n,
        "completed": len(done),
        "rate": len(done) / n if n else 0.0,
        "avg_turns_completed": (sum(t["n_llm_calls"] for t in done) / len(done) if done else None),
        "total_llm_calls": sum(t["n_llm_calls"] for t in per_task.values()),
        "total_tool_calls": sum(t["n_tool_calls"] for t in per_task.values()),
        "format_failures": sum(t["n_format_failures"] for t in per_task.values()),
        "by_kind": {
            k: (
                sum(1 for t in per_task.values() if t["kind"] == k and t["completed"]),
                sum(1 for t in per_task.values() if t["kind"] == k),
            )
            for k in ("read", "write", "recovery")
        },
        "attrs": {},
    }
    for t in per_task.values():
        if t["attr"] != "-":
            summary["attrs"][t["attr"]] = summary["attrs"].get(t["attr"], 0) + 1
    return summary, per_task


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="solar,exaone,claude-sonnet-4-6,ax")
    args = ap.parse_args()
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    tasks_by_id = {t["id"]: t for t in golden["tasks"]}

    slugs = [m.strip() for m in args.models.split(",") if m.strip()]
    all_summ: dict[str, dict] = {}
    all_pt: dict[str, dict] = {}
    for slug in slugs:
        summ, pt = score_model(slug, tasks_by_id)
        if not summ:
            print(f"[{slug}] no raw file — skipped")
            continue
        all_summ[slug], all_pt[slug] = summ, pt
        print(f"\n== {slug} ==")
        print(
            f"completed {summ['completed']}/{summ['n']} ({summ['rate']:.0%})  "
            f"avg_turns(completed)={summ['avg_turns_completed'] and round(summ['avg_turns_completed'], 2)}  "
            f"llm_calls={summ['total_llm_calls']}  tool_calls={summ['total_tool_calls']}  "
            f"fmt_failures={summ['format_failures']}"
        )
        print(f"by_kind: {summ['by_kind']}  attrs: {summ['attrs']}")
        for tid in sorted(all_pt[slug], key=lambda x: (x[0], int(re.sub(r"\D", "", x) or 0))):
            t = all_pt[slug][tid]
            mark = (
                "PASS" if t["completed"] else f"FAIL({t['attr']}; {','.join(t['failed_checks'])})"
            )
            print(f"  {tid}: {mark}  turns={t['n_llm_calls']}")

    # paired McNemar: frontier vs each domestic, over the task ids both ran
    if "claude-sonnet-4-6" in all_pt:
        f = all_pt["claude-sonnet-4-6"]
        for slug in slugs:
            if slug in ("claude-sonnet-4-6", "ax") or slug not in all_pt:
                continue
            d = all_pt[slug]
            common = sorted(set(f) & set(d))
            b = sum(1 for t in common if f[t]["completed"] and not d[t]["completed"])
            c = sum(1 for t in common if not f[t]["completed"] and d[t]["completed"])
            p = mcnemar_exact(b, c)
            print(
                f"\nMcNemar claude-sonnet-4-6 vs {slug} (n={len(common)}): "
                f"frontier-only-wins b={b}, domestic-only-wins c={c}, two-sided exact p={p:.4f}"
            )


if __name__ == "__main__":
    main()
