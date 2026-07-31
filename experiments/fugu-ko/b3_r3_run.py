"""B3 · R3 — delegation false-dispatch runner.

Writes the raw production delegation-extractor output for every item in
`golden/b3_r3_delegation.json` (230: 110 genuine / 120 traps). One LLM call per
(model, item) — the SAME `delegation_extract` call both arms consume. All arm
mapping + the deterministic policy gate live in the scorer (`b3_score.py`), so
mapping can be revised without re-hitting the models.

Production fidelity: the system prompt and excerpt cap are IMPORTED from
`orthus.agentwork.delegation` (`_EXTRACT_SYSTEM_PROMPT`, `_EXTRACT_EXCERPT_MAX`) —
identical to the mail-path extraction call (`orthus.mail.delegation`). No audit
wrapper is invoked (we call the model directly), so nothing is written to the DB.

arm-B derives {dispatch|no_op|request_more_data} from this raw extract alone.
arm-G additionally runs the deterministic prefilter (`non_delegation_reason`) and
the production policy gate (`decide_policy`, agent_task family, llm_inferred=True)
— both applied in the scorer over this same raw output.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import b2_run  # load_dotenv(.env) + prod-wired endpoints  # noqa: E402

from orthus.agentwork.delegation import (  # noqa: E402
    _EXTRACT_EXCERPT_MAX,
    _EXTRACT_SYSTEM_PROMPT,
)

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden" / "b3_r3_delegation.json"
RAW_DIR = HERE / "raw"


def run_item(ep, item: dict) -> dict:
    excerpt = (item["text"] or "").strip()[:_EXTRACT_EXCERPT_MAX]
    raw = ep.complete(_EXTRACT_SYSTEM_PROMPT, excerpt, json_only=True)
    return {"id": item["id"], "raw": raw}


def run_model(ep, items, workers):  # noqa: ANN001
    results: dict[str, dict] = {}
    errors = 0
    err_lock = threading.Lock()

    def work(item):  # noqa: ANN001
        nonlocal errors
        try:
            rec = run_item(ep, item)
        except Exception as exc:  # noqa: BLE001 — never silently drop
            with err_lock:
                errors += 1
            rec = {"id": item["id"], "raw": "", "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
        results[item["id"]] = rec

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, it) for it in items]
        for _ in as_completed(futs):
            pass
    rows = [results[it["id"]] for it in items]
    print(f"  [{ep.slug}] {len(rows)} items errors={errors} {time.monotonic()-t0:.1f}s", flush=True)
    return rows, errors


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default="solar,exaone,gpt-4o-mini")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    items = json.loads(GOLDEN.read_text("utf-8"))["items"]
    if args.limit:
        items = items[: args.limit]
    slugs = [s.strip() for s in args.models.split(",") if s.strip()]

    summary = {}
    for slug in slugs:
        try:
            ep = b2_run.build_endpoint(slug)
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] {slug}: build failed {exc}", flush=True)
            continue
        pf = b2_run.preflight(ep)
        if pf is not None:
            print(f"[skip] {slug}: preflight {pf}", flush=True)
            continue
        print(f"[run] {slug}: {len(items)} items", flush=True)
        rows, errors = run_model(ep, items, args.workers)
        out = RAW_DIR / f"b3_r3_{slug}.jsonl"
        write_jsonl(out, rows)
        summary[slug] = {"n": len(rows), "errors": errors, "path": str(out)}
        print(f"  -> {out}", flush=True)

    print("\n== R3 run summary ==")
    for k, v in summary.items():
        print(f"  {k:<16} n={v['n']:<4} errors={v['errors']}")


if __name__ == "__main__":
    main()
