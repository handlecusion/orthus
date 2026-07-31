"""ask_event_jobs — Phase 3-B MA.8a event-triggered decompose orchestration queue

An inbound company mail with a compound/action signal enqueues one job row here
(docs/company-agent-orchestration.md §P3B.2). A lifespan worker claims it
(FOR UPDATE SKIP LOCKED + lease, modeled on kg_outbox) and runs a knowledge brief
offline via answer_or_decompose; the sink is a draft_for_review event_orchestration
AgentWork item. The sink is NOT a trigger source, so the queue is
acyclic-by-construction (불변식 29, R16). UNIQUE (source_kind, source_ref) is the
idempotency key — re-pulling the same mail does not re-enqueue.

All behavior sits behind ORTHUS_ASK_EVENT_ORCH_ENABLED (default false), so this
table is simply unused until an operator opts in (불변식 32).

Revision ID: 0079_ask_event_jobs
Revises: 0078_ask_cache_drop_ivfflat
Create Date: 2026-06-26
"""

from __future__ import annotations

from alembic import op

revision = "0079_ask_event_jobs"
down_revision = "0078_ask_cache_drop_ivfflat"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ask_event_jobs (
          job_id          UUID PRIMARY KEY,
          source_kind     TEXT NOT NULL,
          source_ref      TEXT NOT NULL,
          seed_question   TEXT NOT NULL,
          scope           TEXT NOT NULL DEFAULT 'company',
          project         TEXT,
          created_by      UUID NOT NULL,
          owner_id        UUID,
          meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
          status          TEXT NOT NULL DEFAULT 'pending',
          attempts        INTEGER NOT NULL DEFAULT 0,
          lease_until     TIMESTAMPTZ,
          last_error      TEXT,
          result_work_id  UUID,
          correlation_id  UUID,
          enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_ask_event_jobs_source UNIQUE (source_kind, source_ref)
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_ask_event_jobs_status_enqueued ON ask_event_jobs (status, enqueued_at);"
    )
    # Partial index on the claimable-pending predicate. The per-mail storm-cap COUNT
    # and the worker claim() both filter `status='pending' AND (lease_until IS NULL OR
    # lease_until < now())`; a partial index on lease_until (pending rows only) keeps
    # those scans bounded to live rows and excludes accumulated done/dead rows entirely.
    op.execute(
        "CREATE INDEX idx_ask_event_jobs_pending_lease ON ask_event_jobs (lease_until) "
        "WHERE status = 'pending';"
    )


def downgrade() -> None:
    op.execute("DROP TABLE ask_event_jobs;")
