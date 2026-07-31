"""Phase 3-B MA.8a — event-triggered decompose orchestration.

Covers the contract gates (docs/company-agent-orchestration.md §P3B.9/§P3B.11):
deterministic trigger signal, flag-off byte-identical (no enqueue), company+inbound
+signal scoping, idempotency, storm cap, the offline worker producing a
draft_for_review knowledge brief (gate), loop-impossible (sink is not a trigger),
dead-letter, and the mail-ingest hook wiring.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, insert, select, update

from orthus.agentwork.state import apply_policy
from orthus.db import session
from orthus.router import event_orchestration as eo
from orthus.schemas.canonical import (
    AgentWorkCandidate,
    MailIngestRequest,
    RoutedAnswer,
    SubAnswer,
    SynthesizedBody,
)
from orthus.settings import get_settings
from orthus.tables import agent_work_items, ask_event_jobs


def _mail(**over) -> MailIngestRequest:
    base = dict(
        backend="nova",
        owner_addr="owner@nova.example",
        external_id="evt-1",
        message_id="<evt-1@nova.example>",
        direction="inbound",
        from_addr="Lead <lead@example.com>",
        subject="매출 보고 요청",
        body_text="지난주 매출 알려주고 김부장에게 보고 메일 보내줘. jane@example.com 참조.",
    )
    base.update(over)
    return MailIngestRequest(**base)


def _enable(monkeypatch, *, max_per_cycle: int = 5) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "ask_event_orch_enabled", True)
    monkeypatch.setattr(s, "ask_event_orch_max_per_cycle", max_per_cycle)
    monkeypatch.setattr(s, "node_kind", "company")
    monkeypatch.setattr(s, "node_id", "company")


def _count(status: str | None = None) -> int:
    with session() as s:
        q = select(func.count()).select_from(ask_event_jobs)
        if status is not None:
            q = q.where(ask_event_jobs.c.status == status)
        return s.execute(q).scalar_one()


def _enqueue(payload, user_id, **over):
    s = get_settings()
    kw = dict(scope="company", source_canonical_id="canon-1", project="company", changed=True)
    kw.update(over)
    return eo.enqueue_mail_event_orchestration(payload, user_id, s, **kw)


def _row_factory(user_id, source_ref: str, status: str, *, days_old: int) -> dict:
    """A raw ask_event_jobs row with an aged enqueued_at, for trim/GC tests."""
    return {
        "job_id": uuid.uuid4(),
        "source_kind": "mail",
        "source_ref": source_ref,
        "seed_question": "seed",
        "scope": "company",
        "created_by": user_id,
        "status": status,
        "enqueued_at": func.now() - timedelta(days=days_old),
    }


def _decomposed_answer(answer: str = "회사는 지난주 매출이 1억이라고 기록한다.") -> RoutedAnswer:
    return RoutedAnswer(
        question="seed",
        mode="decomposed",
        synthesized_body=SynthesizedBody(answer=answer, sources=[], warnings=[]),
        parts=[SubAnswer(sub_question_id=uuid.uuid4(), grounded=True)],
    )


# ── trigger signal (결정론, no DB) ─────────────────────────────────────────────


def test_signal_detects_compound_and_command():
    assert eo.mail_has_orchestration_signal("지난주 매출 알려주고 김부장에게 메일 보내줘")
    assert eo.mail_has_orchestration_signal("A 그리고 B 정리")  # connective
    assert not eo.mail_has_orchestration_signal("회의록 공유합니다")  # plain
    assert not eo.mail_has_orchestration_signal("")  # empty


def test_seed_question_is_redacted():
    seed = eo.build_mail_seed_question(_mail(body_text="문의: jane@example.com 으로 회신"))
    assert "jane@example.com" not in seed
    assert "제목:" in seed and "회사 위키" in seed


# ── policy gate (no DB) ────────────────────────────────────────────────────────


def test_gate_event_orchestration_is_draft_for_review():
    candidate = AgentWorkCandidate(
        source_kind="event_orchestration",
        source_ref_id="mail:canon-1",
        action_family="event_orchestration",
        title="brief",
        payload={"brief": "x"},
    )
    decision = apply_policy(candidate)
    assert decision.state == "draft_for_review"
    assert decision.required_review_role == "owner"
    assert "no_external_write" in decision.reason_codes


# ── enqueue gating (DB) ────────────────────────────────────────────────────────


def test_flag_off_no_enqueue(clean, user_id):
    # flag defaults off in clean → enqueue is a no-op (불변식 32)
    assert _enqueue(_mail(), user_id) is None
    assert _count() == 0


def test_enqueue_compound_company_inbound(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    job_id = _enqueue(_mail(), user_id)
    assert job_id is not None
    with session() as s:
        row = (
            s.execute(select(ask_event_jobs).where(ask_event_jobs.c.job_id == job_id))
            .mappings()
            .one()
        )
    assert row["status"] == "pending"
    assert row["scope"] == "company"
    assert row["owner_id"] is None  # company-shared
    assert row["created_by"] == user_id
    assert row["source_kind"] == "mail"
    assert "jane@example.com" not in row["seed_question"]  # redacted
    assert "jane@example.com" not in (row["meta"].get("subject") or "")


def test_enqueue_idempotent(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    first = _enqueue(_mail(), user_id)
    second = _enqueue(_mail(), user_id)  # same canonical id
    assert first is not None
    assert second is None
    assert _count() == 1


def test_enqueue_skips_non_signal(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    assert _enqueue(_mail(subject="안내", body_text="회의록 공유합니다"), user_id) is None
    assert _count() == 0


def test_enqueue_skips_outbound(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    assert _enqueue(_mail(direction="outbound"), user_id) is None
    assert _count() == 0


def test_enqueue_skips_personal_scope(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    # individual mailbox → scope=personal is excluded (MA.8a company-scope only)
    assert _enqueue(_mail(), user_id, scope="personal") is None
    assert _count() == 0


def test_enqueue_skips_unchanged(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    assert _enqueue(_mail(), user_id, changed=False) is None
    assert _count() == 0


def test_storm_cap(clean, user_id, monkeypatch):
    _enable(monkeypatch, max_per_cycle=1)
    assert _enqueue(_mail(external_id="a"), user_id, source_canonical_id="a") is not None
    # one (unleased) pending already → cap blocks the next enqueue
    assert _enqueue(_mail(external_id="b"), user_id, source_canonical_id="b") is None
    assert _count("pending") == 1


def test_storm_cap_excludes_leased_jobs(clean, user_id, monkeypatch):
    # review #3: a stuck/leased pending job must NOT count toward the backlog cap,
    # else a few failing jobs permanently starve all new enqueues.
    _enable(monkeypatch, max_per_cycle=1)
    job_id = _enqueue(_mail(external_id="a"), user_id, source_canonical_id="a")
    assert job_id is not None
    # simulate the worker holding a future lease on the only pending row
    with session() as s:
        s.execute(
            update(ask_event_jobs)
            .where(ask_event_jobs.c.job_id == job_id)
            .values(lease_until=func.now() + timedelta(hours=1))
        )
        s.commit()
    # backlog excludes the leased row → a new mail still enqueues (not wedged)
    assert _enqueue(_mail(external_id="b"), user_id, source_canonical_id="b") is not None
    assert _count("pending") == 2


# ── worker (DB; answer_or_decompose monkeypatched to isolate the LLM/engine) ───


def test_worker_persists_brief_draft_for_review(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    job_id = _enqueue(_mail(), user_id)
    assert job_id is not None

    monkeypatch.setattr(
        "orthus.router.decompose.answer_or_decompose",
        lambda *a, **k: _decomposed_answer(),
    )
    processed = eo.drain()
    assert processed == 1

    with session() as s:
        job = (
            s.execute(select(ask_event_jobs).where(ask_event_jobs.c.job_id == job_id))
            .mappings()
            .one()
        )
        item = (
            s.execute(
                select(agent_work_items).where(
                    agent_work_items.c.source_kind == "event_orchestration"
                )
            )
            .mappings()
            .one()
        )
    assert job["status"] == "done"
    assert job["result_work_id"] == item["work_id"]
    assert item["action_family"] == "event_orchestration"
    assert item["state"] == "draft_for_review"
    assert item["owner_id"] is None  # company-shared
    assert "1억" in item["payload"]["brief"]
    assert item["payload"]["grounded"] is True


def test_worker_does_not_inject_a_chat_model(clean, user_id, monkeypatch):
    """The offline path must leave model choice to the per-task assignment table.

    Every decompose helper resolves `chat_model or get_chat_model_for(TASK_*)`. So an
    injected model WINS, and this path would silently run on the default model — the
    assignment would look wired but never fire here. Passing None is what lets
    TASK_DECOMPOSE / TASK_SYNTHESIZE actually apply.
    """
    _enable(monkeypatch)
    assert _enqueue(_mail(), user_id) is not None

    seen: dict = {}

    def _capture(*a, **k):
        seen.update(k)
        return _decomposed_answer()

    monkeypatch.setattr("orthus.router.decompose.answer_or_decompose", _capture)
    assert eo.drain() == 1

    assert "chat_model" in seen, "answer_or_decompose requires chat_model — it has no default"
    assert seen["chat_model"] is None


def test_worker_no_brief_on_single_fallthrough(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    job_id = _enqueue(_mail(), user_id)

    # single-mode answer (no synthesized body) → job closes with no noisy item
    single = RoutedAnswer(question="seed", mode="wiki")
    monkeypatch.setattr("orthus.router.decompose.answer_or_decompose", lambda *a, **k: single)
    assert eo.drain() == 1

    with session() as s:
        job = (
            s.execute(select(ask_event_jobs).where(ask_event_jobs.c.job_id == job_id))
            .mappings()
            .one()
        )
        items = s.execute(select(func.count()).select_from(agent_work_items)).scalar_one()
    assert job["status"] == "done"
    assert job["result_work_id"] is None
    assert items == 0


def test_worker_loop_impossible(clean, user_id, monkeypatch):
    # The sink (event_orchestration AgentWork item) must not enqueue a new job —
    # the queue is acyclic by construction (불변식 29). After the worker runs, the
    # only ask_event_jobs row is the original mail job; no new row appears.
    _enable(monkeypatch)
    _enqueue(_mail(), user_id)
    monkeypatch.setattr(
        "orthus.router.decompose.answer_or_decompose",
        lambda *a, **k: _decomposed_answer(),
    )
    eo.drain()
    with session() as s:
        kinds = s.execute(select(ask_event_jobs.c.source_kind).distinct()).scalars().all()
    assert kinds == ["mail"]
    assert _count() == 1


def test_worker_dead_letter_after_max_attempts(clean, user_id, monkeypatch):
    _enable(monkeypatch)
    job_id = _enqueue(_mail(), user_id)

    def _boom(*a, **k):
        raise RuntimeError("decompose blew up")

    monkeypatch.setattr("orthus.router.decompose.answer_or_decompose", _boom)
    # Each drain claims the leased row only after the lease elapses; force-clear the
    # lease between attempts so the test does not wait LEASE_SECONDS.
    for _ in range(eo.MAX_ATTEMPTS):
        eo.drain()
        with session() as s:
            s.execute(
                update(ask_event_jobs)
                .where(ask_event_jobs.c.job_id == job_id)
                .values(lease_until=None)
            )
            s.commit()
    with session() as s:
        job = (
            s.execute(select(ask_event_jobs).where(ask_event_jobs.c.job_id == job_id))
            .mappings()
            .one()
        )
    assert job["status"] == "dead"
    assert job["attempts"] == eo.MAX_ATTEMPTS
    assert "decompose blew up" in (job["last_error"] or "")


def test_worker_operational_error_dead_letters_with_ceiling(clean, user_id, monkeypatch):
    # review pass 2 #1/#2/#8: failures are handled uniformly via _mark_failed (no
    # transient/fatal classification), so even a recurring OperationalError hits the
    # MAX_ATTEMPTS ceiling and dead-letters instead of looping forever.
    from sqlalchemy.exc import OperationalError

    _enable(monkeypatch)
    job_id = _enqueue(_mail(), user_id)

    def _op_error(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("deadlock detected"))

    monkeypatch.setattr("orthus.router.decompose.answer_or_decompose", _op_error)
    # _mark_failed keeps a future lease (backoff) — clear it between drains so the test
    # exercises the ceiling without waiting LEASE_SECONDS.
    for _ in range(eo.MAX_ATTEMPTS):
        eo.drain()
        with session() as s:
            s.execute(
                update(ask_event_jobs)
                .where(ask_event_jobs.c.job_id == job_id)
                .values(lease_until=None)
            )
            s.commit()
    with session() as s:
        job = (
            s.execute(select(ask_event_jobs).where(ask_event_jobs.c.job_id == job_id))
            .mappings()
            .one()
        )
    assert job["status"] == "dead"  # ceiling reached — NOT an infinite retry
    assert job["attempts"] == eo.MAX_ATTEMPTS


def test_mark_failed_keeps_backoff_lease(clean, user_id, monkeypatch):
    # A single failure leaves a FUTURE lease so the job is not re-claimed immediately
    # (no 5s busy-loop, review pass 2 #6).
    _enable(monkeypatch)
    job_id = _enqueue(_mail(), user_id)
    monkeypatch.setattr(
        "orthus.router.decompose.answer_or_decompose",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    eo.drain()
    with session() as s:
        job = (
            s.execute(select(ask_event_jobs).where(ask_event_jobs.c.job_id == job_id))
            .mappings()
            .one()
        )
    assert job["status"] == "pending"
    assert job["attempts"] == 1
    assert job["lease_until"] is not None  # backoff lease set → not re-claimed for LEASE_SECONDS


def test_trim_deletes_old_terminal_rows_keeps_live(clean, user_id):
    # review pass 2 #3: trim GC removes aged done/dead rows, preserves live + recent.
    now_pending = _row_factory(user_id, "live", "pending", days_old=0)
    old_done = _row_factory(user_id, "olddone", "done", days_old=40)
    old_dead = _row_factory(user_id, "olddead", "dead", days_old=40)
    recent_done = _row_factory(user_id, "newdone", "done", days_old=1)
    with session() as s:
        s.execute(insert(ask_event_jobs).values([now_pending, old_done, old_dead, recent_done]))
        s.commit()
    deleted = eo.trim(retention_days=30)
    assert deleted == 2  # old_done + old_dead
    with session() as s:
        refs = set(s.execute(select(ask_event_jobs.c.source_ref)).scalars().all())
    assert refs == {"live", "newdone"}  # pending + recent terminal preserved


def test_queue_agent_work_suppressed_under_knowledge_only(clean, user_id):
    # review pass 2 #5: the action-creation chokepoint itself refuses under knowledge-only,
    # so even if classify suppression were bypassed no action item is ever created.
    from orthus.router import tools

    with tools.knowledge_only_leaves():
        item = tools.queue_agent_work(user_id, "김부장에게 메일 보내줘", actor_role="scheduler")
    assert item is None
    with session() as s:
        count = s.execute(select(func.count()).select_from(agent_work_items)).scalar_one()
    assert count == 0  # no AgentWork item created


# ── fail-closed CLI guard (review #2) ──────────────────────────────────────────


def test_cli_drain_refuses_when_flag_off(clean, user_id, monkeypatch):
    # review #2: drain()/run_once must fail-closed even though it bypasses the lifespan
    # start guard — a pending job is NOT processed with the flag off.
    _enable(monkeypatch)
    job_id = _enqueue(_mail(), user_id)
    assert job_id is not None
    monkeypatch.setattr(get_settings(), "ask_event_orch_enabled", False)

    assert eo.drain() == 0  # run_once re-guard short-circuits
    assert eo.main(["drain"]) == 1  # CLI refuses fail-closed
    assert _count("pending") == 1  # job untouched


# ── knowledge-only suppression (review #1) ─────────────────────────────────────


def test_knowledge_only_leaves_suppresses_action_intake(monkeypatch):
    # review #1: within knowledge_only_leaves(), an action-shaped sub-question must NOT
    # route to the action-intake leaf (no AgentWork action item is ever created).
    from orthus.router import tools

    action_q = "김부장에게 보고 메일 보내줘"
    # Normally this is detected as an action-intake (email_send) leaf.
    leaf, family = tools.classify_subpart(action_q)
    assert leaf == "action-intake"
    assert family is not None
    # Under knowledge-only, the same sub-question routes to a knowledge leaf instead.
    with tools.knowledge_only_leaves():
        leaf2, family2 = tools.classify_subpart(action_q)
    assert leaf2 != "action-intake"
    assert family2 is None


# ── ingest hook wiring (DB) ────────────────────────────────────────────────────


def test_ingest_mail_enqueues_when_enabled(clean, user_id, monkeypatch):
    from orthus.mail.ingest import ingest_mail

    s = get_settings()
    _enable(monkeypatch)
    monkeypatch.setattr(s, "mail_nova_kind", "shared")  # shared mailbox → company scope
    monkeypatch.setattr(s, "mail_reply_draft_enabled", False)
    monkeypatch.setattr(s, "mail_reply_draft_agent_enabled", False)
    # bootstrap-resolve the mailbox owner to our test user
    monkeypatch.setattr(s, "mail_nova_owner", "owner@nova.example")
    with session() as conn:
        from orthus.tables import auth_identities

        conn.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=user_id,
                provider="email",
                provider_subject="owner@nova.example",
                email="owner@nova.example",
                email_verified=True,
            )
        )
        conn.commit()

    result = ingest_mail(_mail(), s)
    assert result.scope == "company"
    assert _count("pending") == 1


def test_ingest_mail_no_enqueue_when_flag_off(clean, user_id, monkeypatch):
    from orthus.mail.ingest import ingest_mail

    s = get_settings()
    # flag stays off (default) — mail ingest byte-identical, no job
    monkeypatch.setattr(s, "mail_nova_kind", "shared")
    monkeypatch.setattr(s, "mail_reply_draft_enabled", False)
    monkeypatch.setattr(s, "mail_reply_draft_agent_enabled", False)
    monkeypatch.setattr(s, "mail_nova_owner", "owner@nova.example")
    with session() as conn:
        from orthus.tables import auth_identities

        conn.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=user_id,
                provider="email",
                provider_subject="owner@nova.example",
                email="owner@nova.example",
                email_verified=True,
            )
        )
        conn.commit()

    ingest_mail(_mail(external_id="off-1", message_id="<off-1@nova.example>"), s)
    assert _count() == 0
