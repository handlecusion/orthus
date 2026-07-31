"""B3 · R2 — abstention ("모른다") runner (wiki_qa grounding).

Writes raw per-arm decisions over `golden/b3_r2_abstention.json` (150 items:
75 answerable / 75 unanswerable across 4 unanswerable axes). Scoring is separate
(`b3_r2_score.py`), so arm mapping / metrics can be revised without re-hitting
the models or the DB.

arm-G (gated) — the PRODUCTION grounded path: `orthus.wiki.qa.ask` with
                `scope="company", learn=False, record_gaps=False` (the contamination-
                safe call the golden + tier_a t2 use). Grounding is real: the query
                is embedded with the Solar `embedding-passage:1024` model and matched
                against the company wiki loaded into `orthus_r2`. The abstain signal is
                the deterministic `result.gap is not None` (gap present => the system
                said "모른다" / could not ground; gap None + a substantive answer =>
                it answered). The chat model is the model under test, passed in as
                `chat_model=` so the grounding wiring is identical across models.

arm-B (bare)  — the SAME model with NO grounding: a parametric-only answer, asked
                to answer from what it knows or say it does not know. Measures what
                grounding + the gap signal ADD over the model alone.

The DB / embedding slot are set in THIS PROCESS ONLY (os.environ before any orthus
import); `.env` is never modified and `orthus_company_0706` is never touched. All
`audit()` writes are neutralized so nothing is written to `orthus_r2` either (the
run is pure-read); `learn=False, record_gaps=False` guarantees no wiki/gap writes.
"""

from __future__ import annotations

import os

# --- process-local wiring: point at orthus_r2 + Solar embedding BEFORE any import --- #
# load_dotenv(.env) runs with override=False (b2_run import), so these os.environ
# values win over .env. orthus_company_0706 (B1) and the .env DSNs are untouched.
os.environ["ORTHUS_PG_DSN"] = "postgresql+psycopg://orthus:orthus@localhost:5433/orthus_r2"
os.environ["ORTHUS_PG_DSN_READONLY"] = "postgresql+psycopg://orthus_ro:orthus_ro@localhost:5433/orthus_r2"
os.environ["ORTHUS_EMBEDDING"] = "solar"  # embedding-passage:1024, single model in orthus_r2
os.environ["ORTHUS_NODE_KIND"] = "company"

import argparse  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from pathlib import Path  # noqa: E402
from uuid import UUID  # noqa: E402

import b2_run  # load_dotenv(.env) + prod-wired endpoints  # noqa: E402

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden" / "b3_r2_abstention.json"
RAW_DIR = HERE / "raw"

# Harness user (L1_HARNESS_UID) — company-scope reads don't require it, but ask()
# threads it through _scope_filter; a real row exists in orthus_r2.
USER_ID = UUID("11111111-1111-1111-1111-111111111111")
SCOPE = "company"


# --- neutralize audit() DB writes (keep orthus_r2 pure-read like the R1 runner) --- #
class _NullSpan:
    def set_output(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        pass

    def add_meta(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        pass


@contextmanager
def _null_audit(*_a, **_k):  # noqa: ANN002, ANN003
    yield _NullSpan()


import orthus.wiki.gap as _gap_mod  # noqa: E402
import orthus.wiki.qa as _qa_mod  # noqa: E402
import orthus.wiki.retrieve as _retrieve_mod  # noqa: E402

for _m in (_qa_mod, _retrieve_mod, _gap_mod):
    _m.audit = _null_audit

from orthus.wiki.qa import ask  # noqa: E402

# --------------------------------------------------------------------------- #
# arm-B: bare, ungrounded answer. arm-B is by definition the model with NO
# production scaffolding — this instruction is deliberately minimal (same stance
# as the R1 bare-arm refusal prompt).
# --------------------------------------------------------------------------- #
_BARE_SYSTEM = (
    "당신은 어떤 회사(아틀라스/영상 플랫폼)의 내부 지식에 대한 질문을 받는다. "
    "당신에게는 그 회사의 내부 문서·위키가 주어지지 않는다. 당신이 확실히 아는 사실만 "
    "답하고, 그 회사 내부 사정이라 근거 없이 지어내야 하거나 모르면 솔직히 모른다고 한다.\n\n"
    "출력은 JSON 객체 하나뿐이다. 스키마:\n"
    '{"decision": "answer"|"idk", '
    '"answer": "<decision이 answer일 때만, 질문에 대한 답 한두 문장>"}\n'
    "확실하지 않으면 idk를 고른다. 내부 수치·이름·일정을 추측으로 지어내지 않는다."
)


def run_arm_b(ep, item: dict) -> dict:
    raw = ep.complete(_BARE_SYSTEM, f"질문: {item['q']}", json_only=True)
    return {"id": item["id"], "raw": raw}


# --------------------------------------------------------------------------- #
# arm-G: production grounded ask() over orthus_r2, chat model = the model on test.
# --------------------------------------------------------------------------- #
def run_arm_g(ep, item: dict) -> dict:
    res = ask(
        USER_ID,
        item["q"],
        scope=SCOPE,
        learn=False,
        record_gaps=False,
        chat_model=ep.adapter,
    )
    gap = res.gap
    sources = res.sources or []
    top_score = max((s.score for s in sources), default=None)
    return {
        "id": item["id"],
        "abstained": gap is not None,
        "gap_reason": (gap.reason if gap is not None else None),
        "top_score": top_score,
        "n_sources": len(sources),
        "source_slugs": [s.page_slug for s in sources[:5]],
        "answer": (res.answer or "")[:600],
    }


# --------------------------------------------------------------------------- #
def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run_model_arm(ep, items, arm, workers):  # noqa: ANN001
    results: dict[str, dict] = {}
    errors = 0
    err_lock = threading.Lock()

    def work(item):  # noqa: ANN001
        nonlocal errors
        try:
            rec = run_arm_b(ep, item) if arm == "B" else run_arm_g(ep, item)
        except Exception as exc:  # noqa: BLE001 — never silently drop
            with err_lock:
                errors += 1
            rec = {"id": item["id"], "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        results[item["id"]] = rec

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, it) for it in items]
        for _ in as_completed(futs):
            pass
    rows = [results[it["id"]] for it in items]
    print(f"  [{ep.slug}/arm-{arm}] {len(rows)} items errors={errors} {time.monotonic()-t0:.1f}s",
          flush=True)
    return rows, errors


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default="solar,exaone")
    ap.add_argument("--arms", default="B,G")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    items = json.loads(GOLDEN.read_text("utf-8"))["items"]
    if args.limit:
        items = items[: args.limit]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    slugs = [s.strip() for s in args.models.split(",") if s.strip()]

    # Pre-warm the connector registry single-threaded. retrieve.py's lazy
    # `if not registered_connector_slugs(): register_default_connector_providers()`
    # is a check-then-act race under the thread pool (raises "already registered"
    # on the 2nd concurrent call). Registering once up front (replace=True is
    # idempotent) makes the lazy branch a no-op for every worker thread.
    from orthus.connectors.registry import register_default_connector_providers
    register_default_connector_providers(replace=True)

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
        for arm in arms:
            rows, errors = run_model_arm(ep, items, arm, args.workers)
            out = RAW_DIR / f"b3_r2_{slug}_{arm}.jsonl"
            write_jsonl(out, rows)
            summary[f"{slug}/{arm}"] = {"n": len(rows), "errors": errors, "path": str(out)}
            print(f"  -> {out}", flush=True)

    print("\n== R2 run summary ==")
    for k, v in summary.items():
        print(f"  {k:<20} n={v['n']:<4} errors={v['errors']}")


if __name__ == "__main__":
    main()
