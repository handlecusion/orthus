"""P10.4b — gated commit-action bridge for the personal agent gateway daemon.

The codebase DELIBERATELY keeps agent-work creation + decisions session-only
(`orthus/api/knowledge_token.py`: promote/allowlist/command-create/agent-work
decisions have no token path). This router is the one bounded exception, so the
owner's outbound-only daemon can propose commit actions and relay the owner's
decision. Every guard is explicit:

- **kill-switch env** ``ORTHUS_AGENT_GATEWAY_ACTIONS_ENABLED`` (``=false`` → 404;
  2026-07-17 owner 결정으로 default ON — PR #777),
  plus the collector-api enable gate + company-node check inherited from
  ``collector_token_auth``.
- **collector-token auth + ``agent_task`` scope**.
- **explicit owner/admin role** resolved from ``token.user_id`` — NOT
  ``require_node_operator`` (which is a silent no-op for tokens).
- **strict owner-scope**: create + decide are keyed on ``token.user_id``; a
  daemon can never act on another owner's rows.
- the deterministic **P3 policy gate** decides the outcome (LLM never does).
- **decisions are draft-only**: they resolve/dismiss the review; real external
  sends stay session-only (no ``send_approved_reply`` here).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from orthus.agentwork import (
    create_agent_task_work_item,
    create_assistant_command_work_item,
    create_email_draft_slot_work_item,
    decide_agent_work_item,
    ensure_email_draft_for_work_item,
    get_agent_work_item,
    list_agent_work_items,
)
from orthus.audit.logger import audit
from orthus.auth import MANAGER_ROLES, allowlist_role
from orthus.collector.auth import CollectorToken, require_scope
from orthus.db import session
from orthus.schemas.canonical import (
    AgentWorkItem,
    AgentWorkReviewDecisionRequest,
    AgentWorkReviewDecisionResult,
)
from orthus.settings import Settings, get_settings
from orthus.tables import agent_gateway_jobs, auth_identities

router = APIRouter(prefix="/collector/gateway", tags=["agent-gateway"])


class AgentGatewayActionRequest(BaseModel):
    """A typed commit-action candidate submitted by the owner's daemon.

    ``kind="command"``: a natural-language command (the deterministic intake
    maps it to email_send / connector_sync / … via the same path as the
    session orchestrator). ``kind="delegate"``: a structured agent_task
    delegation (twin of the session ``/agent-work/delegate`` route).
    ``kind="email_draft"`` (P10 spec §13 Finding 3): a structured email-draft
    slot — ``recipient`` (optional) + ``instruction`` — that bypasses the NL
    command/recipient classifiers entirely; the agent that called the MCP tool
    owns the extraction, the deterministic P3 gate still owns the outcome
    (missing recipient → request_more_data + payload.required_data, P3.4c).
    Legacy daemons keep sending ``kind="command"`` NL payloads unchanged.

    Cross-user delegation is IN scope (spec §5-A / goal ④ "타인 backlog 추가"):
    ``assignee`` may be a teammate — the delegation is routed through the same
    company policy gate (which requires the owner's operator role and dispatches
    to the assignee's own enrolled daemon), exactly like the session route. Blank
    ``assignee`` means self.
    """

    kind: Literal["command", "delegate", "email_draft"]
    text: str | None = None  # kind=command
    assignee: str | None = None  # kind=delegate
    instruction: str | None = None  # kind=delegate | kind=email_draft
    mode: str | None = None
    runner: str | None = None
    cwd: str | None = None
    recipient: str | None = None  # kind=email_draft (옵션 — 없으면 request_more_data)


def _require_gateway(settings: Settings) -> None:
    if not settings.agent_gateway_actions_enabled:
        raise HTTPException(status_code=404, detail="agent gateway actions not found")


def _resolve_owner_role(user_id: UUID, settings: Settings) -> str:
    """Resolve the token owner's allowlist role (owner/admin/…). 403 if the
    owner is not an operator — the collector token itself carries no role, so
    this is the real authorization check (require_node_operator no-ops here).

    A user may have multiple auth_identities (linked accounts). Resolve ALL of
    them in a deterministic order and accept the first that maps to an operator
    role — so a legitimate owner with a second, non-allowlisted email is never
    falsely denied (reviewer Finding 1). A non-operator has no operator-role
    email, so this can never grant access it shouldn't."""
    with session() as s:
        rows = s.execute(
            select(auth_identities.c.email)
            .where(auth_identities.c.user_id == user_id)
            .order_by(auth_identities.c.created_at.asc())
        ).all()
    for row in rows:
        if row.email:
            role = allowlist_role(str(row.email), settings)
            if role in MANAGER_ROLES:
                return role
    raise HTTPException(status_code=403, detail="owner/admin required")


@router.get("/reviews", response_model=list[AgentWorkItem])
def list_reviews(
    token: CollectorToken = Depends(require_scope("agent_task")),
) -> list[AgentWorkItem]:
    """Owner's pending draft_for_review items — the daemon's review surface reads
    this. Gated on ``agent_task`` scope (the daemon token has it) rather than the
    ``knowledge`` scope that ``/agent-work`` requires (reviewer Finding 2), and
    strictly owner-scoped by ``token.user_id``. Server-side ``state`` filter."""
    settings = get_settings()
    _require_gateway(settings)
    _resolve_owner_role(token.user_id, settings)
    return list_agent_work_items(token.user_id, state="draft_for_review", settings=settings)


@router.post("/actions", response_model=AgentWorkItem)
def submit_action(
    body: AgentGatewayActionRequest,
    token: CollectorToken = Depends(require_scope("agent_task")),
) -> AgentWorkItem:
    settings = get_settings()
    _require_gateway(settings)
    role = _resolve_owner_role(token.user_id, settings)

    with audit("agent_gateway.submit") as span:
        span.add_meta(kind=body.kind, owner=str(token.user_id))
        if body.kind == "command":
            text = (body.text or "").strip()
            if not text:
                raise HTTPException(status_code=422, detail="empty command text")
            item = create_assistant_command_work_item(
                token.user_id,
                text,
                actor_role=role,
                intake_source="agent_gateway",
                settings=settings,
            )
        elif body.kind == "email_draft":
            # 구조화 slot — NL 분류기(command detect / recipient 정규식) 미호출.
            # outcome은 동일한 결정론 P3 gate가 정한다 (recipient 부재 →
            # request_more_data + payload.required_data, P3.4c 계약 불변).
            instruction = (body.instruction or "").strip()
            if not instruction:
                raise HTTPException(status_code=422, detail="empty instruction")
            item = create_email_draft_slot_work_item(
                token.user_id,
                instruction,
                recipient=body.recipient,
                actor_role=role,
                intake_source="agent_gateway",
                settings=settings,
            )
        else:  # delegate
            instruction = (body.instruction or "").strip()
            if not instruction:
                raise HTTPException(status_code=422, detail="empty instruction")
            runner = (body.runner or "codex").strip().lower()
            mode = (body.mode or "knowledge").strip().lower()
            if runner not in {"codex", "claude", "hermes"}:
                raise HTTPException(status_code=422, detail=f"invalid runner: {runner}")
            if mode not in {"code", "knowledge"}:
                raise HTTPException(status_code=422, detail=f"invalid mode: {mode}")
            assignee = (body.assignee or "").strip() or str(token.user_id)
            item = create_agent_task_work_item(
                token.user_id,
                actor_role=role,
                assignee=assignee,
                mode=mode,
                runner=runner,
                instruction=instruction,
                cwd=(body.cwd or None),
                settings=settings,
            )
        span.add_meta(created=item is not None)

    if item is None:
        raise HTTPException(status_code=422, detail="action not allowed or empty")
    return item


@router.post("/actions/{work_id}/decision", response_model=AgentWorkReviewDecisionResult)
def decide_action(
    work_id: UUID,
    body: AgentWorkReviewDecisionRequest,
    token: CollectorToken = Depends(require_scope("agent_task")),
) -> AgentWorkReviewDecisionResult:
    settings = get_settings()
    _require_gateway(settings)
    _resolve_owner_role(token.user_id, settings)

    # Strict owner-scope: only the token owner's own item is visible/decidable.
    pre = get_agent_work_item(token.user_id, work_id)
    if pre is None:
        raise HTTPException(status_code=404, detail="work item not found")
    if body.decision == "approve":
        # idempotent: materialize the email draft so no-edit telemetry is recorded
        ensure_email_draft_for_work_item(token.user_id, pre)

    with audit("agent_gateway.decision") as span:
        span.add_meta(work_id=str(work_id), decision=body.decision, owner=str(token.user_id))
        try:
            result = decide_agent_work_item(
                token.user_id,
                work_id,
                body.decision,
                note=body.note,
                no_edit=body.no_edit,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    # NOTE (P10.4b boundary): the gateway decision is DRAFT-ONLY. Unlike the
    # session decision route (agent_work.py), it intentionally does NOT call
    # send_approved_reply (no real external mail send) NOR
    # resume_held_command_split_action (no deferred external action resumes via a
    # daemon token). A command_split_hold item approved here transitions to
    # resolved without running the held action — safer (no unintended external
    # side effect), at the cost of the operator needing the session UI to resume
    # a held external action (reviewer Finding 2). Real send + hold-resume stay
    # session-only until a later slice.
    return result


# --- C-7 잡 정의 central 미러 (조회 전용, best-effort push) ---------------------


class GatewayJobMirrorEntry(BaseModel):
    """데몬 로컬 SQLite 잡 정의 1건의 미러 페이로드. owner 필드는 없다 —
    owner_user_id는 서버가 token.user_id로만 바인딩한다(페이로드로 위조 불가).
    seen-set 항목(민감 URL 가능 — PO 결정)과 chat_id는 미러하지 않는다."""

    job_id: str
    name: str
    instruction: str
    schedule: dict
    schedule_human: str | None = None
    enabled: bool = True
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    next_run_at: datetime | None = None


class GatewayJobsMirrorRequest(BaseModel):
    jobs: list[GatewayJobMirrorEntry] = Field(default_factory=list)


class GatewayJobsMirrorResult(BaseModel):
    mirrored: int
    deleted: int


@router.put("/jobs", response_model=GatewayJobsMirrorResult)
def mirror_gateway_jobs(
    body: GatewayJobsMirrorRequest,
    token: CollectorToken = Depends(require_scope("agent_task")),
) -> GatewayJobsMirrorResult:
    """PUT = 그 owner의 잡 정의 전체 교체(full-replace) 미러.

    (token.user_id, job_id) 키로 upsert하고 페이로드에 없는 그 owner의 row는
    삭제한다. 로컬 SQLite가 실행 SoR이고 이 미러는 조회 전용이다 — central을
    편집해 로컬 실행을 바꾸는 인바운드 경로는 존재하지 않는다(spec §9 단방향).
    미러 실패는 데몬 쪽에서 best-effort로 삼켜지므로 잡 실행을 막지 않는다."""
    settings = get_settings()
    _require_gateway(settings)
    _resolve_owner_role(token.user_id, settings)

    now = datetime.now(UTC)
    with audit("agent_gateway.jobs_mirror") as span:
        span.add_meta(owner=str(token.user_id), jobs=len(body.jobs))
        keep = [entry.job_id for entry in body.jobs]
        with session() as s:
            for entry in body.jobs:
                values = {
                    # strict owner-bind: 페이로드가 아니라 token에서만 온다.
                    "owner_user_id": token.user_id,
                    "job_id": entry.job_id,
                    "name": entry.name,
                    "instruction": entry.instruction,
                    "schedule": entry.schedule,
                    "schedule_human": entry.schedule_human,
                    "enabled": entry.enabled,
                    "last_run_at": entry.last_run_at,
                    "last_status": entry.last_status,
                    "last_error": entry.last_error,
                    "next_run_at": entry.next_run_at,
                    "mirrored_at": now,
                }
                stmt = pg_insert(agent_gateway_jobs).values(id=uuid4(), **values)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_agent_gateway_jobs_owner_job",
                    set_={**values, "updated_at": now},
                )
                s.execute(stmt)
            # full-replace: 페이로드에 없는 이 owner의 row만 삭제 (타 owner 불변).
            deleted = s.execute(
                delete(agent_gateway_jobs).where(
                    agent_gateway_jobs.c.owner_user_id == token.user_id,
                    agent_gateway_jobs.c.job_id.notin_(keep),
                )
            ).rowcount
            s.commit()
        span.add_meta(deleted=deleted)
    return GatewayJobsMirrorResult(mirrored=len(body.jobs), deleted=deleted)
