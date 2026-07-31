"""B3 · R1 — gate-refusal runner (structured / SQL feasibility).

Writes raw model decisions for two arms over `golden/b3_r1_gate.json` (200 items:
100 feasible / 100 infeasible across 6 axes). Scoring is separate (`b3_score.py`).

arm-B (bare)  — the model's OWN judgment. Given the same live catalog the
                production compiler sees, it is asked "answerable against this
                schema? produce SQL, or REFUSE with a reason". No gate, no
                execution, no cascade. REFUSE => the model decided infeasible.

arm-G (gated) — the production wiring, reassembled from imported production
                functions (no query_runs writes, DB read-only):
                  build_notion_catalog -> compile_query (LLM) -> validate (gate)
                  -> _inject_scope_filter -> execute_readonly -> retry_signal
                  -> SVC cascade (2nd model on eligible triggers).
                Per prereg §3.3 the gate cannot structurally catch the
                nonexistent-column / nonexistent-table axes (JSONB `properties`
                keys are data, not schema); those surface as a silent 0-row / 0
                result. Every item records the mechanics so the scorer can assign
                layer attribution {gate_reject | cascade_rescue | abstained |
                undetected}.

All prompts/functions are IMPORTED from production — nothing is re-authored
except the deliberately-bare arm-B refusal instruction (arm-B is by definition
the model without production scaffolding).

Prod cascade note: current .env has structured_fallback_mode=on but
llm_fallback_model="" (cascade dormant in live prod). Per prereg arm-G is defined
to INCLUDE the SVC cascade, so this runner enables it with a fixed 2nd model
(--fallback, default solar) and records cascade participation explicitly.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import UUID

# b2_run already load_dotenv(.env) at import and builds prod-wired endpoints.
import b2_run  # noqa: E402

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden" / "b3_r1_gate.json"
RAW_DIR = HERE / "raw"

# Company user with company-scope visibility in orthus_company_0706 (read-only).
USER_ID = UUID("11111111-1111-1111-1111-111111111111")
SCOPE = "company"


# --- neutralize audit() DB writes (DB is read-only; audit INSERTs audit_log) --- #
class _NullSpan:
    def set_output(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        pass

    def add_meta(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        pass


@contextmanager
def _null_audit(*_a, **_k):  # noqa: ANN002, ANN003
    yield _NullSpan()


import orthus.assistant.compile as _compile_mod  # noqa: E402
import orthus.assistant.execute as _execute_mod  # noqa: E402
import orthus.assistant.pipeline as _pipeline_mod  # noqa: E402
import orthus.assistant.validate as _validate_mod  # noqa: E402
import orthus.structured.query as _query_mod  # noqa: E402

# Every audit() call reaches audit_log via the READ-WRITE engine (session()).
# The DB is read-only for this harness, so neutralize audit in EVERY module whose
# functions this runner invokes: compile_query, validate, execute_readonly
# (audit "assistant.execute"), and the structured.query / pipeline helpers. Missing
# execute.audit would silently INSERT telemetry rows into orthus_company_0706.
for _m in (_compile_mod, _validate_mod, _execute_mod, _pipeline_mod, _query_mod):
    _m.audit = _null_audit

from orthus.assistant.compile import compile_query  # noqa: E402
from orthus.assistant.execute import execute_readonly  # noqa: E402
from orthus.assistant.validate import validate  # noqa: E402
from orthus.settings import get_settings  # noqa: E402
from orthus.structured.query import (  # noqa: E402
    _DIALECT,
    _inject_scope_filter,
    build_notion_catalog,
)

_DIALECT_NAME = _DIALECT  # "postgres"


# --------------------------------------------------------------------------- #
# arm-B: bare feasibility judgment. Deliberately NOT a production prompt — arm-B
# is the model alone. It gets the same catalog rendering the compiler gets.
# --------------------------------------------------------------------------- #
_BARE_SYSTEM = (
    "당신은 사내 데이터에 대한 읽기 전용 SQL 질의를 만드는 분석가다. 아래 스키마"
    "(테이블·컬럼·설명)만 근거로, 사용자의 질문이 이 스키마로 **실제로 답할 수 있는지** "
    "스스로 판단한다.\n"
    "- 답할 수 있으면: 그 질문에 답하는 단일 read-only SELECT SQL을 출력한다.\n"
    "- 답할 수 없으면(존재하지 않는 컬럼/테이블 요구, 예측·주관·추천처럼 SQL 범위 밖, "
    "어느 컬럼인지 모호, 또는 데이터를 바꾸는 쓰기 요청): SQL을 만들지 말고 거절한다.\n\n"
    "출력은 JSON 객체 하나뿐이다. 스키마:\n"
    '{"decision": "sql"|"refuse", '
    '"sql": "<decision이 sql일 때만, 단일 SELECT>", '
    '"reason": "<decision이 refuse일 때 한국어 한 문장 사유>"}\n'
    "확실하지 않으면 refuse한다. 없는 컬럼/테이블을 지어내지 않는다."
)


def _render_catalog(catalog: dict) -> str:
    return _compile_mod._render_catalog(catalog)


def _bare_messages(question: str, catalog: dict) -> tuple[str, str]:
    user = f"Question: {question}\n\nSchema:\n{_render_catalog(catalog)}"
    return _BARE_SYSTEM, user


def run_arm_b(ep, item: dict, catalog: dict) -> dict:
    system, user = _bare_messages(item["q"], catalog)
    raw = ep.complete(system, user, json_only=True)
    return {"id": item["id"], "raw": raw}


# --------------------------------------------------------------------------- #
# arm-G: production wiring, reassembled. Returns a dict of mechanics per item.
# --------------------------------------------------------------------------- #
def _numeric_cells(rows: list[list]) -> set[float]:
    nums: set[float] = set()
    for row in rows:
        for cell in row:
            if isinstance(cell, bool):
                continue
            if isinstance(cell, (int, float, Decimal)):
                nums.add(float(cell))
    return nums


def _signal(passed: bool, executed: bool, row_count: int, rows: list[list]) -> str | None:
    """Replica of orthus.structured.query._retry_signal over raw mechanics."""
    if not passed:
        return "gate_fail"
    if not executed:
        return "not_executed"
    if row_count == 0 or not rows:
        return "empty_rows"
    nums = _numeric_cells(rows)
    if nums and nums == {0.0}:
        return "zero_answer"
    return None


_ADOPT_ELIGIBLE = frozenset({"gate_fail", "not_executed", "empty_rows"})


def _one_pass(ep, question: str, catalog: dict, dsn: str, timeout_ms: int) -> dict:
    """Single compile+validate+execute pass. Returns mechanics dict."""
    out = {
        "compiled_sql": None,
        "rejected_reason": None,
        "passed": False,
        "executed": False,
        "row_count": 0,
        "rows": [],
        "error": None,
    }
    try:
        compiled = compile_query(question, None, _DIALECT_NAME, catalog, [], ep)
    except Exception as exc:  # noqa: BLE001 — compile failure = cannot execute
        out["rejected_reason"] = f"compile_failed:{type(exc).__name__}"
        out["error"] = str(exc)[:160]
        return out
    out["compiled_sql"] = compiled.sql
    settings = get_settings()
    validation, final_sql = validate(
        compiled.sql, _DIALECT_NAME, catalog,
        default_limit=settings.assistant_default_limit, dsn_secret_key=None,
    )
    out["passed"] = bool(validation.passed)
    if not validation.passed:
        out["rejected_reason"] = validation.rejected_reason
        return out
    try:
        final_sql = _inject_scope_filter(final_sql, USER_ID, SCOPE, None)
        cols, rows, row_count = execute_readonly(final_sql, dsn, timeout_ms)
        out["executed"] = True
        # keep only a small numeric/shape footprint of rows (avoid huge raw files)
        out["row_count"] = int(row_count)
        out["rows"] = [[_cell(c) for c in r] for r in rows[:20]]
    except Exception as exc:  # noqa: BLE001 — execution failure = not_executed
        out["rejected_reason"] = f"execute_failed:{type(exc).__name__}"
        out["error"] = str(exc)[:160]
    return out


def _cell(c):  # noqa: ANN001, ANN201
    if isinstance(c, bool):
        return c
    if isinstance(c, Decimal):
        return float(c)
    if isinstance(c, (int, float, str)) or c is None:
        return c
    return str(c)[:80]


def run_arm_g(ep, item: dict, catalog: dict, fallback_ep, dsn: str, timeout_ms: int) -> dict:
    primary = _one_pass(ep, item["q"], catalog, dsn, timeout_ms)
    p_sig = _signal(primary["passed"], primary["executed"], primary["row_count"], primary["rows"])

    cascade_ran = False
    adopted = False
    secondary = None
    sec_sig = None
    if fallback_ep is not None and p_sig in _ADOPT_ELIGIBLE:
        cascade_ran = True
        secondary = _one_pass(fallback_ep, item["q"], catalog, dsn, timeout_ms)
        sec_sig = _signal(
            secondary["passed"], secondary["executed"], secondary["row_count"], secondary["rows"]
        )
        adopted = sec_sig is None  # 2nd is clean -> adopt

    final = secondary if adopted else primary
    f_sig = sec_sig if adopted else p_sig

    return {
        "id": item["id"],
        "primary_signal": p_sig,
        "cascade_ran": cascade_ran,
        "adopted": adopted,
        "secondary_signal": sec_sig,
        "final_signal": f_sig,
        "final_passed": final["passed"],
        "final_executed": final["executed"],
        "final_rejected_reason": final["rejected_reason"],
        "final_row_count": final["row_count"],
        "final_zero_answer": f_sig == "zero_answer",
        "primary_sql": primary["compiled_sql"],
        "primary_reject": primary["rejected_reason"],
    }


# --------------------------------------------------------------------------- #
def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run_model_arm(ep, items, arm, catalog, fallback_ep, dsn, timeout_ms, workers):  # noqa: ANN001
    results: dict[str, dict] = {}
    errors = 0
    err_lock = threading.Lock()

    def work(item):  # noqa: ANN001
        nonlocal errors
        try:
            if arm == "B":
                rec = run_arm_b(ep, item, catalog)
            else:
                rec = run_arm_g(ep, item, catalog, fallback_ep, dsn, timeout_ms)
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
    print(f"  [{ep.slug}/arm-{arm}] {len(rows)} items errors={errors} {time.monotonic()-t0:.1f}s",
          flush=True)
    return rows, errors


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default="solar,exaone,gpt-4o-mini")
    ap.add_argument("--arms", default="B,G")
    ap.add_argument("--fallback", default="solar", help="SVC cascade 2nd model for arm-G")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    items = json.loads(GOLDEN.read_text("utf-8"))["items"]
    if args.limit:
        items = items[: args.limit]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    slugs = [s.strip() for s in args.models.split(",") if s.strip()]

    settings = get_settings()
    dsn = settings.pg_dsn_readonly
    timeout_ms = settings.assistant_query_timeout_ms

    # Build the live catalog ONCE (identical for every item; scope=company).
    print("[catalog] building company catalog from orthus_company_0706 ...", flush=True)
    catalog = build_notion_catalog(USER_ID, scope=SCOPE)
    tbls = catalog.get("tables", {})
    print(f"  tables={list(tbls)}  notion_rows desc len={len(tbls.get('notion_rows',{}).get('description',''))}",
          flush=True)

    fallback_ep = None
    if "G" in arms:
        fallback_ep = b2_run.build_endpoint(args.fallback)
        print(f"[cascade] fallback 2nd model = {args.fallback}", flush=True)

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
            rows, errors = run_model_arm(
                ep, items, arm, catalog, fallback_ep, dsn, timeout_ms, args.workers
            )
            out = RAW_DIR / f"b3_r1_{slug}_{arm}.jsonl"
            write_jsonl(out, rows)
            summary[f"{slug}/{arm}"] = {"n": len(rows), "errors": errors, "path": str(out)}
            print(f"  -> {out}", flush=True)

    print("\n== R1 run summary ==")
    for k, v in summary.items():
        print(f"  {k:<24} n={v['n']:<4} errors={v['errors']}")


if __name__ == "__main__":
    main()
