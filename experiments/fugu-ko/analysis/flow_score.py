"""Score Metric-06 flow-bench raw rows into per-flow completion + stage attribution."""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

RAW = Path("experiments/fugu-ko/analysis/raw")
L2 = Path("experiments/fugu-ko/e2e/l2")

# D10: agent-authored items exist from two authoring waves; either tag counts
# as "agent_authored" in the provenance table (analysis/d10-authoring-note.md).
PROV_TAGS = (
    "provenance:agent_authored_2026-07-22",
    "provenance:agent_authored_2026-07-24",
)

meta = {}
for f in ["g1.jsonl", "g2.jsonl", "g3.jsonl", "g4.jsonl"]:
    for line in (L2 / f).read_text().splitlines():
        it = json.loads(line)
        meta[it["id"]] = it


def stage_of(task, score, result):
    if score["status"] == "error":
        return "exception"
    fails = score.get("failures") or []
    key = fails[0].split(":")[0] if fails else ""
    if task == "g4":
        return "delegation_gate"
    if task == "g2":
        if key == "mode":
            return "routing"
        if key.startswith("agent_work"):
            return "command_gate"
        if key.startswith("part_count"):
            return "decompose"
        return "routing"
    if task == "g3":
        if key.startswith("delegation"):
            return "delegation_extraction"
        if key in ("status", "ingested", "scope", "doc_id"):
            return "mail_ingest"
        if key.startswith(("log_", "response_", "reply_send")) or "recipient" in key:
            return "send_gate"
        return "candidate_gate"
    if task == "g1":
        if key == "status":
            return "publish_route"
        return "wiki_authoring"
    return "other"


def load(slug):
    p = RAW / f"e2e_flow_{slug}.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()]


def completed(score):
    """Completion: model-independent 'pass', or discriminating assert_ok True."""
    if score["status"] == "pass":
        return True
    if score["status"] == "fail":
        return False
    if score["status"] == "deferred" and "assert_ok" in score:
        return bool(score["assert_ok"])
    return None  # not scorable (judge/metric/projection gap/skip/error)


def main(slugs):
    per_model = {}
    for slug in slugs:
        rows = load(slug)
        flows = defaultdict(lambda: {"n": 0, "ok": 0})
        prov = defaultdict(lambda: {"n": 0, "ok": 0})
        stages = Counter()
        unscored = Counter()
        calls = 0
        item_ok = {}
        for r in rows:
            calls += r["result"].get("n_llm_calls") or 0
            if r["score"]["status"] == "error":
                stages[stage_of(r["task"], r["score"], r["result"])] += 1
                item_ok[r["id"]] = False
                flows[r["task"]]["n"] += 1
                continue
            c = completed(r["score"])
            if c is None:
                unscored[r["score"].get("reason") or r["score"]["status"]] += 1
                continue
            flows[r["task"]]["n"] += 1
            flows[r["task"]]["ok"] += int(c)
            tag = (
                "agent_authored"
                if any(t in meta[r["id"]]["tags"] for t in PROV_TAGS)
                else "prebuilt"
            )
            prov[tag]["n"] += 1
            prov[tag]["ok"] += int(c)
            item_ok[r["id"]] = c
            if not c:
                stages[stage_of(r["task"], r["score"], r["result"])] += 1
        per_model[slug] = {
            "flows": dict(flows),
            "prov": dict(prov),
            "stages": dict(stages),
            "unscored": dict(unscored),
            "llm_calls": calls,
            "item_ok": item_ok,
        }

    for slug, d in per_model.items():
        tot_n = sum(v["n"] for v in d["flows"].values())
        tot_ok = sum(v["ok"] for v in d["flows"].values())
        print(f"\n== {slug} ==  overall {tot_ok}/{tot_n} ({tot_ok / tot_n:.1%})  llm_calls={d['llm_calls']}")
        for fl in sorted(d["flows"]):
            v = d["flows"][fl]
            print(f"  {fl}: {v['ok']}/{v['n']} ({v['ok'] / v['n']:.1%})")
        for tag, v in sorted(d["prov"].items()):
            print(f"  provenance {tag}: {v['ok']}/{v['n']} ({v['ok'] / v['n']:.1%})")
        print(f"  failure stages: {dict(sorted(d['stages'].items()))}")
        print(f"  unscored: {dict(sorted(d['unscored'].items()))}")

    # cross-model regression-guard identity check + discriminating-only table
    disc = [i for i, m in meta.items() if "model_fallback_zero" in (m.get("invariants") or [])]
    print("\n== discriminating structural items (per model) ==")
    for slug, d in per_model.items():
        sub = {i: v for i, v in d["item_ok"].items() if i in disc}
        by_flow = defaultdict(lambda: [0, 0])
        for i, v in sub.items():
            fl = meta[i]["task"]
            by_flow[fl][0] += int(v)
            by_flow[fl][1] += 1
        line = "  ".join(f"{fl} {a}/{b}" for fl, (a, b) in sorted(by_flow.items()))
        tot = sum(v for v in sub.values())
        print(f"  {slug}: {tot}/{len(sub)}  |  {line}")

    guards = [i for i in meta if i not in disc]
    base = None
    for slug, d in per_model.items():
        g = {i: v for i, v in d["item_ok"].items() if i in guards}
        if base is None:
            base, base_slug = g, slug
        else:
            diff = {i for i in base if i in g and base[i] != g[i]}
            print(f"  guard-identity {base_slug} vs {slug}: {'IDENTICAL' if not diff else 'DIFFS ' + str(sorted(diff))}")

    out = RAW / "flowbench_scored.json"
    out.write_text(json.dumps(
        {s: {k: v for k, v in d.items() if k != "item_ok"} | {"item_ok": d["item_ok"]} for s, d in per_model.items()},
        ensure_ascii=False, indent=1))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
