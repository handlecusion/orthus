"""MA.5 measurement harness for the /ask compound-question decompose slice.

docs/company-agent-orchestration.md §19 (MA.5). Phase 1 (단일-pass parallel
dispatch) is merged behind `ORTHUS_ASK_DECOMPOSE_ENABLED`. Before building Phase 2
(ReAct 루프 / `depends_on` 위상 정렬), this harness measures Phase 1 over a company
snapshot so the Phase 2 go/no-go is evidence-based, not a guess:

  (a) 복합 질문 빈도/형태       — compound-question frequency + prefilter signal form
  (b) 독립 병렬 복합 질문 비율   — share that split into ≥2 independent parts
  (c) decompose vs 단일 route 품질 — side-by-side bodies for human grading + grounded
                                    coverage proxy
  (d) 5초 인라인 예산 현실성     — decompose wall-clock latency
  (e) Phase 1 스트리밍 latency  — pre-stream blocking time (split) as a time-to-first
                                    -frame lower-bound proxy

Three modes, combinable:

  --from-audit   read historical `router.decompose` audit spans (LLM 0회, pure DB —
                 the real production signal for decompose rate / n_parts / latency).
  (default)      run should_decompose + split_question over a question corpus
                 (DEFAULT_CASES, --from-fixture, or --from-query-runs) → (a)(b).
  --live         additionally run single vs decomposed answers per case → (c)(d)(e).
                 Heavy: full answer() + answer_or_decompose() per question.

This is a read-only measurement tool. It does NOT enable the flag, write wiki/corpus,
or queue Agent Work. Leaf calls use learn=False / record_gaps=False / allow_agentic=
False so measurement never mutates T2 memory or gap rows (불변식 5).

Run:
  uv run python -m orthus.router.decompose_measure --from-audit
  uv run python -m orthus.router.decompose_measure --live --json /tmp/ma5.json
  make decompose-measure ARGS="--from-audit --live"
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from orthus.db import session
from orthus.models.base import ChatModel
from orthus.models.registry import get_chat_model
from orthus.router.decompose import (
    _has_command_verb,
    _has_connective_or_enum,
    answer_or_decompose,
    prefilter_ext_tier,
    should_decompose,
    split_question,
)
from orthus.settings import get_settings
from orthus.tables import audit_log, query_runs

log = logging.getLogger(__name__)

# Demo/owner user used for live runs; scope/owner are inherited strictly so this
# only ever sees company + this caller's own data (불변식 5, §11).
_MEASURE_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


# ── Representative corpus ─────────────────────────────────────────────────────
# Grounded in the actual company snapshot (orthus_company): every topic below maps
# to real wiki pages confirmed present at corpus-build time (개발자 온보딩 / 로그아웃
# 문제 해결 / 배포 프로세스 / 세액 계산 / 신규회사 등록 / 정산 데이터 / 주간 회고 Nova),
# across the 4 real projects (atlas · nova · orbit · company). This makes the
# --live grounded-coverage metric (c) meaningful — each compound part can actually
# be answered from compiled wiki, not a hallucination probe.
#
# Real production usage observed in audit_log: all 3 router.decompose runs split
# into exactly 2 parts, so the compound set centers on 2-part cross-topic questions.
# `expected_compound` is the analyst's label, printed next to should_decompose's
# verdict (precision/recall context). It is NOT ground truth fed to any model.
@dataclass(frozen=True)
class Case:
    question: str
    expected_compound: bool


DEFAULT_CASES: tuple[Case, ...] = (
    # --- expected compound (2 independent topics, each groundable) ---
    Case("개발자 온보딩 절차 알려주고 배포 프로세스도 알려줘", True),
    Case("로그아웃 문제는 어떻게 해결했고 그리고 세액 계산은 어떻게 해?", True),
    Case("신규회사 등록 방법 알려주고 정산 데이터값 불러오기도 알려줘", True),
    Case("nova 주간 회고 내용 알려주고 atlas 일정 업데이트도 알려줘", True),
    Case("1. 개발자 온보딩 2. 배포 프로세스 두 가지 다 정리해줘", True),
    # --- expected single (one topic, possibly multiple aspects → NOT compound) ---
    Case("로그아웃 문제 원인과 해결 과정이 뭐야?", False),
    Case("개발자 온보딩 워크플로우가 어떻게 돼?", False),
    Case("배포 프로세스가 어떻게 돼?", False),
    Case("세액 계산은 어떻게 해?", False),
    Case("프로젝트는 어떤 게 있어?", False),  # the one real query_runs question
)


# ── (a)(b) corpus offline metrics ────────────────────────────────────────────
@dataclass
class CorpusRow:
    question: str
    expected_compound: bool | None
    has_command_verb: bool
    has_connective_enum: bool
    prefilter_passed: bool  # at least one signal → would call the LLM enum
    decomposed: bool  # should_decompose final verdict
    n_split: int  # split_question() length (0 if not decomposed / split empty)


@dataclass
class CorpusReport:
    n: int
    prefilter_pass_rate: float
    decompose_rate: float
    independent_parallel_rate: float  # of decomposed: split ≥ 2
    n_parts_mean: float | None
    n_parts_distribution: dict[int, int]
    signal_breakdown: dict[str, int]
    # When expected labels are present (DEFAULT_CASES), confusion vs should_decompose.
    confusion: dict[str, int] | None
    rows: list[CorpusRow] = field(default_factory=list)


def measure_corpus(
    questions: list[Case], *, k: int, chat_model: ChatModel | None = None
) -> CorpusReport:
    chat = chat_model or get_chat_model()
    rows: list[CorpusRow] = []
    for case in questions:
        q = case.question
        compact = q.lower().replace(" ", "")
        has_verb = _has_command_verb(compact)
        # O4/E3: 이 리포트는 프로덕션 프리필터의 거울이므로 활성 확장 티어를 그대로 읽는다
        # (tier 0 default에서는 기존과 동일한 신호 집합).
        has_conn = _has_connective_or_enum(q, ext_tier=prefilter_ext_tier())
        prefilter_passed = bool(has_verb or has_conn)
        decomposed = should_decompose(q, chat_model=chat)
        n_split = 0
        if decomposed:
            n_split = len(split_question(q, k=k, chat_model=chat))
        rows.append(
            CorpusRow(
                question=q,
                expected_compound=case.expected_compound,
                has_command_verb=has_verb,
                has_connective_enum=has_conn,
                prefilter_passed=prefilter_passed,
                decomposed=decomposed,
                n_split=n_split,
            )
        )

    n = len(rows)
    decomposed_rows = [r for r in rows if r.decomposed]
    independent = [r for r in decomposed_rows if r.n_split >= 2]
    parts = [r.n_split for r in independent]
    dist: dict[int, int] = {}
    for p in parts:
        dist[p] = dist.get(p, 0) + 1

    confusion: dict[str, int] | None = None
    if all(r.expected_compound is not None for r in rows) and n:
        tp = sum(1 for r in rows if r.expected_compound and r.decomposed)
        fp = sum(1 for r in rows if not r.expected_compound and r.decomposed)
        fn = sum(1 for r in rows if r.expected_compound and not r.decomposed)
        tn = sum(1 for r in rows if not r.expected_compound and not r.decomposed)
        confusion = {"tp": tp, "fp": fp, "fn": fn, "tn": tn}

    return CorpusReport(
        n=n,
        prefilter_pass_rate=_safe_div(sum(r.prefilter_passed for r in rows), n),
        decompose_rate=_safe_div(len(decomposed_rows), n),
        independent_parallel_rate=_safe_div(len(independent), len(decomposed_rows)),
        n_parts_mean=(statistics.mean(parts) if parts else None),
        n_parts_distribution=dist,
        signal_breakdown={
            "command_verb": sum(r.has_command_verb for r in rows),
            "connective_enum": sum(r.has_connective_enum for r in rows),
        },
        confusion=confusion,
        rows=rows,
    )


# ── --from-audit: historical production signal (no LLM) ──────────────────────
@dataclass
class AuditReport:
    total_runs: int
    decomposed_runs: int
    decompose_rate: float
    n_parts_mean: float | None
    n_parts_distribution: dict[int, int]
    latency_p50_sec: float | None
    latency_p95_sec: float | None
    latency_over_5s: int  # (d) inline-budget violations
    reasons: dict[str, int]


def measure_from_audit(*, limit: int | None = None) -> AuditReport:
    """Read historical `router.decompose` spans straight from audit_log.

    enter/exit rows share node_run_id; latency = exit.occurred_at - enter.occurred_at.
    exit.meta carries {decomposed, n_parts, reason} written by answer_or_decompose().
    """
    with session() as s:
        rows = s.execute(
            select(
                audit_log.c.node_run_id,
                audit_log.c.phase,
                audit_log.c.meta,
                audit_log.c.occurred_at,
            )
            .where(audit_log.c.node == "router.decompose")
            .order_by(audit_log.c.occurred_at)
        ).all()

    runs: dict[UUID, dict[str, tuple]] = {}
    for node_run_id, phase, meta, occurred_at in rows:
        runs.setdefault(node_run_id, {})[phase] = (meta or {}, occurred_at)

    run_items = list(runs.items())
    if limit is not None:
        run_items = run_items[-limit:]

    total = 0
    decomposed = 0
    parts: list[int] = []
    latencies: list[float] = []
    reasons: dict[str, int] = {}
    for _node_run_id, phases in run_items:
        if "enter" not in phases:
            continue
        total += 1
        _enter_meta, enter_ts = phases["enter"]
        exit_phase = phases.get("exit")
        if exit_phase is None:
            continue
        exit_meta, exit_ts = exit_phase
        if exit_ts is not None and enter_ts is not None:
            latencies.append((exit_ts - enter_ts).total_seconds())
        if exit_meta.get("decomposed"):
            decomposed += 1
            np = exit_meta.get("n_parts")
            if isinstance(np, int):
                parts.append(np)
        reason = exit_meta.get("reason")
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1

    dist: dict[int, int] = {}
    for p in parts:
        dist[p] = dist.get(p, 0) + 1

    return AuditReport(
        total_runs=total,
        decomposed_runs=decomposed,
        decompose_rate=_safe_div(decomposed, total),
        n_parts_mean=(statistics.mean(parts) if parts else None),
        n_parts_distribution=dist,
        latency_p50_sec=(_percentile(latencies, 50) if latencies else None),
        latency_p95_sec=(_percentile(latencies, 95) if latencies else None),
        latency_over_5s=sum(1 for x in latencies if x > 5.0),
        reasons=reasons,
    )


# ── --live: (c)(d)(e) single vs decomposed comparison ────────────────────────
@dataclass
class LiveRow:
    question: str
    single_latency_sec: float
    single_mode: str
    decompose_latency_sec: float
    split_latency_sec: float  # (e) pre-stream blocking lower-bound for time-to-first-frame
    n_parts: int
    grounded_parts: int
    grounded_coverage: float  # grounded_parts / n_parts
    decompose_inline_ok: bool  # (d) total ≤ 5s
    # Bodies for human grading (c) — truncated, redaction already applied upstream.
    single_body: str
    synthesized_body: str | None
    # Per-part leaf bodies (c diagnosis): lets us see whether a missing topic was
    # lost at retrieval (leaf body already empty) or at synthesis (leaf had it, the
    # synthesized body dropped it). Each entry: "sub_q :: leaf_answer".
    part_bodies: list[str] = field(default_factory=list)


@dataclass
class LiveReport:
    n: int
    single_latency_p50: float | None
    decompose_latency_p50: float | None
    decompose_latency_p95: float | None
    split_latency_p50: float | None
    inline_budget_ok_rate: float  # (d) share of decompose runs ≤ 5s
    grounded_coverage_mean: float | None
    rows: list[LiveRow] = field(default_factory=list)


def measure_live(
    questions: list[Case],
    *,
    user_id: UUID,
    scope: str,
    project: str | None,
    k: int,
    chat_model: ChatModel | None = None,
    body_chars: int = 280,
) -> LiveReport:
    from orthus.router import answer as _answer

    chat = chat_model or get_chat_model()
    rows: list[LiveRow] = []
    for case in questions:
        q = case.question

        # Per-case isolation: a transient LLM/network blip on one question records an
        # error row and continues, instead of aborting the whole sweep (reproducibility).
        try:
            # Single baseline (legacy ladder, no decompose, no agentic).
            t0 = time.monotonic()
            single = _answer(
                user_id,
                q,
                scope=scope,
                project=project,
                chat_model=chat,
                allow_decompose=False,
                allow_agentic=False,
                learn=False,
                record_gaps=False,
            )
            single_latency = time.monotonic() - t0

            # Pre-stream blocking time = split (everything before any SSE frame).
            t1 = time.monotonic()
            _subs = split_question(q, k=k, chat_model=chat)
            split_latency = time.monotonic() - t1

            # Decomposed path (stream_id=None so no SSE side-effects in the harness).
            t2 = time.monotonic()
            dec = answer_or_decompose(
                user_id,
                q,
                scope=scope,
                project=project,
                chat_model=chat,
                context_wiki_slug=None,
                history=None,
                stream_id=None,
                actor_role="owner",
                allow_agentic_in_leaf=False,
                learn=False,
                record_gaps=False,
            )
            decompose_latency = time.monotonic() - t2

            n_parts = len(dec.parts)
            grounded_parts = sum(1 for p in dec.parts if p.grounded)
            synth = dec.synthesized_body.answer if dec.synthesized_body else None

            part_bodies: list[str] = []
            for p in dec.parts:
                leaf_body = ""
                if p.routed is not None and p.routed.wiki is not None:
                    leaf_body = p.routed.wiki.answer or ""
                elif p.routed is not None and p.routed.structured is not None:
                    leaf_body = getattr(p.routed.structured, "summary", "") or ""
                tag = "G" if p.grounded else ("E" if p.error else "-")
                part_bodies.append(f"[{tag}] {_truncate(leaf_body, body_chars) or '(empty)'}")

            rows.append(
                LiveRow(
                    question=q,
                    single_latency_sec=round(single_latency, 3),
                    single_mode=single.mode,
                    decompose_latency_sec=round(decompose_latency, 3),
                    split_latency_sec=round(split_latency, 3),
                    n_parts=n_parts,
                    grounded_parts=grounded_parts,
                    grounded_coverage=_safe_div(grounded_parts, n_parts),
                    decompose_inline_ok=decompose_latency <= 5.0,
                    single_body=_truncate(_body_of(single), body_chars),
                    synthesized_body=_truncate(synth, body_chars) if synth else None,
                    part_bodies=part_bodies,
                )
            )
        except Exception as exc:
            log.warning("measure_live case failed (%s): %s", type(exc).__name__, q[:40])
            rows.append(
                LiveRow(
                    question=q,
                    single_latency_sec=0.0,
                    single_mode="error",
                    decompose_latency_sec=0.0,
                    split_latency_sec=0.0,
                    n_parts=0,
                    grounded_parts=0,
                    grounded_coverage=0.0,
                    decompose_inline_ok=False,
                    single_body=f"(case error: {type(exc).__name__})",
                    synthesized_body=None,
                    part_bodies=[],
                )
            )

    decompose_lat = [r.decompose_latency_sec for r in rows]
    return LiveReport(
        n=len(rows),
        single_latency_p50=(
            _percentile([r.single_latency_sec for r in rows], 50) if rows else None
        ),
        decompose_latency_p50=(_percentile(decompose_lat, 50) if rows else None),
        decompose_latency_p95=(_percentile(decompose_lat, 95) if rows else None),
        split_latency_p50=(_percentile([r.split_latency_sec for r in rows], 50) if rows else None),
        inline_budget_ok_rate=_safe_div(sum(r.decompose_inline_ok for r in rows), len(rows)),
        grounded_coverage_mean=(
            statistics.mean([r.grounded_coverage for r in rows if r.n_parts]) if rows else None
        ),
        rows=rows,
    )


# ── corpus source loaders ────────────────────────────────────────────────────
def load_fixture(path: Path) -> list[Case]:
    """JSON list of {"question": str, "expected_compound"?: bool}."""
    data = json.loads(path.read_text())
    cases: list[Case] = []
    for item in data:
        if isinstance(item, str):
            cases.append(Case(item, None))  # type: ignore[arg-type]
        else:
            cases.append(Case(item["question"], item.get("expected_compound")))
    return cases


def load_query_runs(limit: int) -> list[Case]:
    """Most-recent distinct redacted nl_question from query_runs (structured branch)."""
    with session() as s:
        rows = s.execute(
            select(query_runs.c.nl_question, query_runs.c.created_at)
            .order_by(query_runs.c.created_at.desc())
            .limit(limit * 4)
        ).all()
    seen: set[str] = set()
    cases: list[Case] = []
    for nl_question, _created_at in rows:
        q = (nl_question or "").strip()
        if q and q not in seen:
            seen.add(q)
            cases.append(Case(q, None))  # type: ignore[arg-type]
        if len(cases) >= limit:
            break
    return cases


# ── helpers ──────────────────────────────────────────────────────────────────
def _safe_div(a: float, b: float) -> float:
    return round(a / b, 4) if b else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    frac = k - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 3)


def _truncate(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    return text if len(text) <= n else text[:n] + "…"


def _body_of(routed) -> str:
    if routed.mode in {"wiki", "graph"} and routed.wiki is not None:
        return routed.wiki.answer or ""
    if routed.mode == "structured" and routed.structured is not None:
        return getattr(routed.structured, "summary", "") or ""
    if routed.mode == "decomposed" and routed.synthesized_body is not None:
        return routed.synthesized_body.answer
    return ""


# ── CLI ──────────────────────────────────────────────────────────────────────
def _print_corpus(rep: CorpusReport) -> None:
    print("=== (a)(b) corpus offline ===")
    print(f"n={rep.n}  prefilter_pass_rate={rep.prefilter_pass_rate}")
    print(
        f"decompose_rate={rep.decompose_rate}  independent_parallel_rate={rep.independent_parallel_rate}"
    )
    print(f"n_parts mean={rep.n_parts_mean}  dist={rep.n_parts_distribution}")
    print(f"signal_breakdown={rep.signal_breakdown}")
    if rep.confusion:
        c = rep.confusion
        prec = _safe_div(c["tp"], c["tp"] + c["fp"])
        rec = _safe_div(c["tp"], c["tp"] + c["fn"])
        print(f"confusion={c}  precision={prec}  recall={rec}")
    for r in rep.rows:
        exp = "" if r.expected_compound is None else f" exp={r.expected_compound}"
        print(f"  [{'D' if r.decomposed else '-'}] n_split={r.n_split}{exp}  {r.question[:60]}")


def _print_audit(rep: AuditReport) -> None:
    print("=== --from-audit (production signal, no LLM) ===")
    print(
        f"total_runs={rep.total_runs}  decomposed_runs={rep.decomposed_runs}  rate={rep.decompose_rate}"
    )
    print(f"n_parts mean={rep.n_parts_mean}  dist={rep.n_parts_distribution}")
    print(
        f"latency p50={rep.latency_p50_sec}s p95={rep.latency_p95_sec}s  "
        f"over_5s={rep.latency_over_5s}"
    )
    print(f"reasons={rep.reasons}")


def _print_live(rep: LiveReport) -> None:
    print("=== (c)(d)(e) live single vs decompose ===")
    print(f"n={rep.n}")
    print(f"single_latency_p50={rep.single_latency_p50}s")
    print(f"decompose_latency p50={rep.decompose_latency_p50}s p95={rep.decompose_latency_p95}s")
    print(
        f"split(pre-stream) p50={rep.split_latency_p50}s  inline_budget_ok_rate={rep.inline_budget_ok_rate}"
    )
    print(f"grounded_coverage_mean={rep.grounded_coverage_mean}")
    for r in rep.rows:
        print(
            f"  parts={r.n_parts} grounded={r.grounded_parts} "
            f"dec={r.decompose_latency_sec}s single={r.single_latency_sec}s({r.single_mode})  "
            f"{r.question[:50]}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MA.5 decompose measurement (read-only).")
    ap.add_argument(
        "--from-audit", action="store_true", help="historical router.decompose spans (no LLM)"
    )
    ap.add_argument("--live", action="store_true", help="single vs decompose live answers (heavy)")
    ap.add_argument("--no-corpus", action="store_true", help="skip the offline corpus pass")
    ap.add_argument("--from-fixture", type=Path, help="JSON corpus file")
    ap.add_argument(
        "--from-query-runs", type=int, metavar="N", help="N recent distinct query_runs questions"
    )
    ap.add_argument("--scope", default="company", help="live answer scope (company|all|personal)")
    ap.add_argument("--project", default=None, help="live answer project bucket")
    ap.add_argument(
        "--user-id",
        type=UUID,
        default=_MEASURE_USER_ID,
        help="caller user_id for --live runs (must exist in users; scope/owner inherited)",
    )
    ap.add_argument("--audit-limit", type=int, default=None, help="last N audit runs only")
    ap.add_argument("--json", type=Path, help="dump full report to this JSON file")
    args = ap.parse_args(argv)

    settings = get_settings()
    k = settings.ask_decompose_max_parts
    report: dict[str, object] = {"flag_enabled": settings.ask_decompose_enabled, "k": k}

    if args.from_audit:
        audit_rep = measure_from_audit(limit=args.audit_limit)
        _print_audit(audit_rep)
        report["audit"] = asdict(audit_rep)

    cases: list[Case]
    if args.from_fixture:
        cases = load_fixture(args.from_fixture)
    elif args.from_query_runs:
        cases = load_query_runs(args.from_query_runs)
    else:
        cases = list(DEFAULT_CASES)

    if not args.no_corpus:
        corpus_rep = measure_corpus(cases, k=k)
        _print_corpus(corpus_rep)
        report["corpus"] = asdict(corpus_rep)

    if args.live:
        live_rep = measure_live(
            cases, user_id=args.user_id, scope=args.scope, project=args.project, k=k
        )
        _print_live(live_rep)
        report["live"] = asdict(live_rep)

    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
