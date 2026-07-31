"""Phase 3-B MA.8a — event-triggered decompose orchestration.

An inbound company mail with a compound/action signal enqueues one `ask_event_jobs`
row (`enqueue_mail_event_orchestration`, called best-effort from `mail/ingest.py`).
A lifespan worker (`EventOrchestrationWorker`, modeled on K3 `KGOutboxWorker`) claims
the job offline (`FOR UPDATE SKIP LOCKED` + lease), runs `answer_or_decompose` over a
deterministic knowledge-framed seed, and persists the synthesized brief as a
`draft_for_review` `event_orchestration` AgentWork item (`persist_event_orchestration_brief`).

Design boundaries (docs/company-agent-orchestration.md §P3B):
- **Async, no /ask path change (E1):** the trigger fires from the mail ingest
  post-commit hook; the worker runs in the API lifespan, not the request thread.
  The result sinks to AgentWork, never an inline SSE answer.
- **One-shot, loop-impossible (E2/R16):** the only trigger source is "mail". The
  sink is an `event_orchestration` AgentWork item, which is NOT a trigger source, so
  the queue is acyclic-by-construction. Idempotency on `(source_kind, source_ref)`
  blocks duplicate enqueues; a per-pending storm cap blocks flooding.
- **Action stays P3-gated (E3):** the brief performs no external write and the gate
  fixes it to `draft_for_review`. The existing P7.1 reply-draft path is unchanged —
  MA.8a only adds a knowledge brief (sink = knowledge only, owner decision 2026-06-26).
- **Fail-closed (E6):** everything sits behind `ORTHUS_ASK_EVENT_ORCH_ENABLED`
  (default false). Off → mail ingest byte-identical (불변식 32).

This module's top-level imports avoid `orthus.router.decompose` /
`orthus.agentwork.service` / `orthus.models` — those are imported lazily inside the
worker to keep enqueue (called from mail ingest) cheap and cycle-free.
"""

from __future__ import annotations

import sys
import threading
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.audit import audit, redact_pii_text
from orthus.audit.logger import current_correlation_id
from orthus.db import session
from orthus.schemas.canonical import MailIngestRequest
from orthus.settings import Settings, get_settings
from orthus.tables import ask_event_jobs

MAX_ATTEMPTS = 5  # 5회 실패 → dead (kg_outbox 선례)
LEASE_SECONDS = 120  # orchestration은 LLM 여러 번이라 outbox(60s)보다 길게
BATCH_SIZE = 5
POLL_SECONDS = 5
RETENTION_DAYS = 30  # done/dead row 보존 기간 — trim이 그 이전 행 정리 (kg_outbox 선례)
_SEED_BODY_SNIPPET = 1200  # seed에 담는 메일 본문 상한
_BRIEF_TITLE_MAX = 200
# Fixed advisory-lock key for serializing enqueue (storm-cap count→insert). A stable
# arbitrary 63-bit int unique to this queue; pg_advisory_xact_lock is per-key.
_ENQUEUE_LOCK_KEY = 0x4153_4B45_5630_3179  # "ASKEV01y"


# ── 트리거 신호 (결정론, §P3B Q1 = 복합/실행 신호 메일만) ───────────────────────


def mail_has_orchestration_signal(text: str) -> bool:
    """should_decompose §4.1 prefilter 신호를 그대로 재사용한 결정론 트리거.

    명령 동사(`_COMMAND_VERBS`) 또는 접속·열거 힌트가 하나라도 있으면 True. 둘 다
    없으면(명백히 단일 정보 메일) False — LLM은 부르지 않는다(action_family 결정론
    전용, 불변식 14의 정신과 동일).
    """
    from orthus.router.decompose import _has_command_verb, _has_connective_or_enum

    compact = text.lower().replace(" ", "")
    if not compact:
        return False
    return _has_command_verb(compact) or _has_connective_or_enum(text)


def build_mail_seed_question(payload: MailIngestRequest) -> str:
    """메일을 '회사 지식 질문'으로 framing한 결정론 seed(redaction 통과, 불변식 6).

    답장 액션이 아니라 *지식 조회*로 framing한다 — sink이 지식 브리프만이기 때문
    (owner decision 2026-06-26). 본문은 상한 snippet으로 자른다.
    """
    subject = (payload.subject or "").strip()
    body = (payload.body_text or "").strip()[:_SEED_BODY_SNIPPET]
    raw = (
        "다음 수신 메일과 관련해 회사 위키에서 알 수 있는 내용을 항목별로 정리해 주세요.\n"
        f"제목: {subject}\n"
        f"내용: {body}"
    )
    return redact_pii_text(raw).strip()


def mail_source_ref(source_canonical_id: str) -> str:
    """idempotency 키 — 같은 메일은 한 번만 enqueue된다."""
    return f"mail:{source_canonical_id}"


def _mail_meta(payload: MailIngestRequest) -> dict[str, Any]:
    return {
        "backend": payload.backend,
        "subject": redact_pii_text((payload.subject or "").strip())[:_BRIEF_TITLE_MAX],
        "external_id": payload.external_id,
        "message_id": payload.message_id,
    }


# ── enqueue (mail ingest post-commit hook에서 best-effort 호출) ────────────────


def enqueue_mail_event_orchestration(
    payload: MailIngestRequest,
    owner_user_id: UUID,
    settings: Settings,
    *,
    scope: str,
    source_canonical_id: str,
    project: str | None,
    changed: bool,
    span=None,
) -> UUID | None:
    """복합/실행 신호가 있는 inbound company mail에 대해 orchestration job을 적재.

    모든 가드(flag/company/scope/inbound/signal)를 통과해야만 INSERT한다.
    `on_conflict_do_nothing`으로 멱등하고, pending 수가 storm cap을 넘으면 skip한다.
    이 함수는 절대 예외를 던지지 않는다(ingest를 깨지 않음 — 호출자도 try로 감싼다).
    Returns the new job_id, or None when skipped.
    """
    try:
        if not settings.ask_event_orch_enabled:
            return None
        if not changed:
            return None
        if settings.node_kind != "company":
            return None
        # MA.8a는 company-scope 전용 — 개인 업무함(scope=personal)은 제외(§P3B.0).
        if scope != "company":
            return None
        if getattr(payload, "direction", "inbound") != "inbound":
            return None
        if not mail_has_orchestration_signal(f"{payload.subject}\n{payload.body_text}"):
            return None
        seed = build_mail_seed_question(payload)
        if not seed:
            return None

        source_ref = mail_source_ref(source_canonical_id)
        correlation_id = current_correlation_id() or uuid4()
        with session() as s:
            # Serialize the count→insert across concurrent enqueuers (parallel pull
            # workers / a push concurrent with the pull loop) so the storm cap is a
            # hard bound, not a soft one that overshoots by the number of writers
            # (review pass 2 #7). Transaction-scoped advisory lock — released on commit.
            s.execute(select(func.pg_advisory_xact_lock(_ENQUEUE_LOCK_KEY)))
            # Storm guard = bound on the UNCLAIMED backlog (claimable-now pending rows).
            # Rows currently leased by the worker, or a failed row in lease backoff,
            # are excluded — otherwise a handful of stuck/retrying jobs would pin the
            # cap and permanently starve new mail from ever enqueuing (review #3).
            backlog = s.execute(
                select(func.count())
                .select_from(ask_event_jobs)
                .where(
                    ask_event_jobs.c.status == "pending",
                    (ask_event_jobs.c.lease_until.is_(None))
                    | (ask_event_jobs.c.lease_until < func.now()),
                )
            ).scalar_one()
            if backlog >= settings.ask_event_orch_max_per_cycle:
                if span is not None:
                    span.add_meta(event_orch_skipped="storm_cap", backlog=backlog)
                return None
            job_id = uuid4()
            stmt = (
                pg_insert(ask_event_jobs)
                .values(
                    job_id=job_id,
                    source_kind="mail",
                    source_ref=source_ref,
                    seed_question=seed,
                    scope="company",
                    project=project,
                    created_by=owner_user_id,
                    owner_id=None,  # company-shared
                    meta=_mail_meta(payload),
                    status="pending",
                    attempts=0,
                    correlation_id=correlation_id,
                )
                .on_conflict_do_nothing(constraint="uq_ask_event_jobs_source")
                .returning(ask_event_jobs.c.job_id)
            )
            inserted = s.execute(stmt).first()
            s.commit()
        if inserted is None:
            # a job for this mail already exists — idempotent skip (불변식 29)
            if span is not None:
                span.add_meta(event_orch_skipped="idempotent")
            return None
        if span is not None:
            span.add_meta(event_orch_job_id=str(job_id))
        return job_id
    except Exception as exc:  # noqa: BLE001 — enqueue failure must never break ingest
        if span is not None:
            span.add_meta(event_orch_enqueue_error=type(exc).__name__)
        return None


# ── worker (lifespan; kg_outbox 패턴) ─────────────────────────────────────────


class EventJobRow(BaseModel):
    job_id: UUID
    source_kind: str
    source_ref: str
    seed_question: str
    scope: str
    project: str | None
    created_by: UUID
    owner_id: UUID | None
    meta: dict
    correlation_id: UUID | None
    attempts: int


class EventOrchestrationWorker:
    """poll → claim(`FOR UPDATE SKIP LOCKED` + lease) → orchestrate → AgentWork sink.

    single-worker 가정(lifespan 1개). LLM 호출 실패는 attempts+1로 backoff,
    5회 → dead. 정책/감사는 sink(`persist_event_orchestration_brief` → 결정론 게이트)이
    소유한다 — worker는 transport일 뿐(불변식 3/9).
    """

    def __init__(
        self,
        *,
        poll_seconds: float = POLL_SECONDS,
        lease_seconds: int = LEASE_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.batch_size = batch_size

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            processed = 0
            try:
                processed = self.run_once()
            except Exception as exc:  # noqa: BLE001 — worker loop must survive one bad batch
                self._warn(f"event-orch worker batch failed: {type(exc).__name__}: {exc}")
            # Only idle-wait on an empty batch; drain a backlog back-to-back
            # (kg_outbox cadence). Otherwise a burst would clear at batch_size/poll.
            if processed == 0:
                stop.wait(self.poll_seconds)

    def _warn(self, message: str) -> None:
        print(message, file=sys.stderr)

    def run_once(self) -> int:
        """1 batch claim→orchestrate. 성공 처리한 job 수를 반환한다.

        Fail-closed re-guard (defense-in-depth, kg_outbox.run_once 선례): even though
        the lifespan starter only spawns the worker on a company node with the flag on,
        re-check here so the CLI `drain` path and any direct caller can NEVER process
        jobs with the flag off or on a personal node (불변식 32 / company-scope only).
        """
        settings = get_settings()
        if not settings.ask_event_orch_enabled or settings.node_kind != "company":
            return 0
        rows = self.claim()
        if not rows:
            return 0
        processed = 0
        for row in rows:
            try:
                if self.process_one(row):
                    processed += 1
            except Exception as exc:  # noqa: BLE001 — isolate one job; backoff + dead-letter ceiling
                # Uniform failure handling (no transient/fatal classification — that
                # proved both too broad and too narrow, review pass 2 #1/#2/#6):
                # _mark_failed keeps the lease as a backoff (no busy re-claim) and
                # dead-letters at MAX_ATTEMPTS so a poison-pill can never loop forever.
                # A genuine DB-down case is still survived WITHOUT burning an attempt:
                # if the DB is unreachable, claim() raises first (run_forever sleeps,
                # no row touched), and if even _mark_failed cannot write, it raises out
                # of run_once → run_forever idles → the leased row is re-claimed after
                # the lease expires with attempts unchanged.
                self._mark_failed(row, f"{type(exc).__name__}: {exc}")
        return processed

    def process_one(self, row: EventJobRow) -> bool:
        from orthus.agentwork.service import persist_event_orchestration_brief
        from orthus.router.decompose import answer_or_decompose
        from orthus.router.tools import knowledge_only_leaves

        settings = get_settings()
        with audit("router.event_orchestration", correlation_id=row.correlation_id) as span:
            span.add_meta(
                job_id=str(row.job_id),
                source_kind=row.source_kind,
                source_ref=row.source_ref,
            )
            # knowledge_only_leaves(): the offline orchestration must never spawn or
            # auto-execute action AgentWork items from inbound mail (§P3B E3 / 불변식 29,
            # review #1). The ContextVar propagates into fan_out's leaf threads via
            # copy_context().run, so action-shaped sub-questions route as knowledge.
            with knowledge_only_leaves():
                routed = answer_or_decompose(
                    row.created_by,
                    row.seed_question,
                    scope=row.scope,
                    project=row.project,
                    # None on purpose. Every decompose helper resolves
                    # `chat_model or get_chat_model_for(TASK_*)`, so injecting a model here
                    # would win and silently strip this path of its per-task assignment.
                    chat_model=None,
                    context_wiki_slug=None,
                    history=None,
                    stream_id=None,  # no synchronous client → no SSE (§P3B.6)
                    actor_role="scheduler",
                    allow_agentic_in_leaf=False,
                    learn=False,
                    record_gaps=False,
                )
            # Persist a brief only when decompose actually produced a grounded
            # synthesis. A single fall-through (mode != decomposed / empty body)
            # adds no value, so we close the job without a noisy empty item.
            synth = routed.synthesized_body
            if routed.mode != "decomposed" or synth is None or not (synth.answer or "").strip():
                span.add_meta(result="no_brief", mode=routed.mode)
                self._mark_done(row.job_id, None)
                return True
            item = persist_event_orchestration_brief(
                row.created_by,
                routed,
                source_ref=row.source_ref,
                title=_brief_title(row),
                meta=row.meta,
                settings=settings,
            )
            span.add_meta(result="brief", mode=routed.mode, result_work_id=str(item.work_id))
            self._mark_done(row.job_id, item.work_id)
            return True

    # --- claim / lease -------------------------------------------------------

    def claim(self) -> list[EventJobRow]:
        with session() as s:
            rows = (
                s.execute(
                    select(
                        ask_event_jobs.c.job_id,
                        ask_event_jobs.c.source_kind,
                        ask_event_jobs.c.source_ref,
                        ask_event_jobs.c.seed_question,
                        ask_event_jobs.c.scope,
                        ask_event_jobs.c.project,
                        ask_event_jobs.c.created_by,
                        ask_event_jobs.c.owner_id,
                        ask_event_jobs.c.meta,
                        ask_event_jobs.c.correlation_id,
                        ask_event_jobs.c.attempts,
                    )
                    .where(
                        ask_event_jobs.c.status == "pending",
                        (ask_event_jobs.c.lease_until.is_(None))
                        | (ask_event_jobs.c.lease_until < func.now()),
                    )
                    .order_by(ask_event_jobs.c.enqueued_at)
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .all()
            )
            if not rows:
                return []
            ids = [r["job_id"] for r in rows]
            s.execute(
                update(ask_event_jobs)
                .where(ask_event_jobs.c.job_id.in_(ids))
                .values(lease_until=func.now() + timedelta(seconds=self.lease_seconds))
            )
            s.commit()
        return [EventJobRow(**dict(r)) for r in rows]

    def _mark_done(self, job_id: UUID, work_id: UUID | None) -> None:
        with session() as s:
            s.execute(
                update(ask_event_jobs)
                .where(ask_event_jobs.c.job_id == job_id)
                .values(
                    status="done",
                    lease_until=None,
                    last_error=None,
                    result_work_id=work_id,
                )
            )
            s.commit()

    def _mark_failed(self, row: EventJobRow, error: str) -> None:
        attempts = row.attempts + 1
        status = "dead" if attempts >= self.max_attempts else "pending"
        # pending으로 남는 실패는 lease를 backoff로 유지 — 즉시 재claim되어 일시 오류가
        # 한 poll 안에 attempts를 소진하는 것을 막는다(kg_outbox 선례).
        with session() as s:
            s.execute(
                update(ask_event_jobs)
                .where(ask_event_jobs.c.job_id == row.job_id)
                .values(
                    status=status,
                    attempts=attempts,
                    last_error=error[:500],
                    lease_until=func.now() + timedelta(seconds=self.lease_seconds),
                )
            )
            s.commit()


def _brief_title(row: EventJobRow) -> str:
    subject = (row.meta or {}).get("subject") or ""
    return (f"수신 메일 지식 브리프: {subject}").strip()[:_BRIEF_TITLE_MAX]


# ── lifespan 통합 ──────────────────────────────────────────────────────────────


def start_event_orchestration_worker_thread() -> tuple[threading.Thread, threading.Event] | None:
    """기동 조건: `ask_event_orch_enabled and node_kind == "company"`. 미충족이면 None.

    uvicorn reload 모드의 감시용 부모 프로세스는 lifespan을 실행하지 않아 worker가
    뜨지 않는다(kg_outbox와 동일). flag off면 prod 동작 불변(불변식 32).
    """
    settings = get_settings()
    if not settings.ask_event_orch_enabled or settings.node_kind != "company":
        return None
    stop = threading.Event()
    worker = EventOrchestrationWorker()
    thread = threading.Thread(
        target=worker.run_forever,
        args=(stop,),
        name="ask-event-orch-worker",
        daemon=True,
    )
    thread.start()
    return thread, stop


# ── CLI: python -m orthus.router.event_orchestration drain|trim (테스트·복구·GC) ──


def drain(*, max_batches: int = 10_000) -> int:
    """pending job 전부 처리 시도. 처리한 job 수를 반환한다."""
    worker = EventOrchestrationWorker(poll_seconds=0)
    total = 0
    for _ in range(max_batches):
        processed = worker.run_once()
        if processed == 0:
            break
        total += processed
    return total


def trim(*, retention_days: int = RETENTION_DAYS) -> int:
    """Delete done/dead rows older than retention_days; returns rows deleted.

    Runtime-decoupled GC (kg_outbox `trim` / ask_cache `gc` 선례). Without it,
    terminal rows accumulate forever and bloat the per-mail storm-cap COUNT and the
    worker's claim() index scans. pending/leased (live) rows are never touched, and
    it runs regardless of the feature flag — cleaning terminal rows is harmless.
    """
    with session() as s:
        result = s.execute(
            delete(ask_event_jobs).where(
                ask_event_jobs.c.status.in_(("done", "dead")),
                ask_event_jobs.c.enqueued_at < func.now() - timedelta(days=retention_days),
            )
        )
        s.commit()
    return result.rowcount or 0


def status_counts() -> dict[str, int]:
    with session() as s:
        rows = s.execute(
            select(ask_event_jobs.c.status, func.count()).group_by(ask_event_jobs.c.status)
        ).all()
    return {r[0]: r[1] for r in rows}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "drain"
    if cmd not in ("drain", "trim"):
        print(
            f"unknown command: {cmd} "
            "(usage: python -m orthus.router.event_orchestration [drain|trim])",
            file=sys.stderr,
        )
        return 2
    if cmd == "trim":
        # GC is flag-independent (terminal-row cleanup is harmless even if the feature
        # is off — same rationale as ask_cache gc).
        deleted = trim()
        print(f"ask event orchestration trim: deleted={deleted}")
        return 0
    settings = get_settings()
    if not settings.ask_event_orch_enabled or settings.node_kind != "company":
        # Fail-closed: drain must not process jobs with the flag off or off a company
        # node (불변식 32). run_once re-guards too, so this is a friendly early message.
        print(
            "event orchestration drain refused: requires ORTHUS_ASK_EVENT_ORCH_ENABLED "
            "+ company node (fail-closed)",
            file=sys.stderr,
        )
        return 1
    processed = drain()
    counts = status_counts()
    print(
        f"ask event orchestration drain: processed={processed}, "
        f"pending={counts.get('pending', 0)}, dead={counts.get('dead', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
