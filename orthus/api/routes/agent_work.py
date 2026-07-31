"""P3 Agent Work review queue endpoints."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from orthus.agentwork import (
    _completion_failure_summary,
    _completion_result_summary,
    agent_work_notify_state,
    build_inbox_summary,
    continue_agent_task_after_user_input,
    create_agent_task_work_item,
    execute_reviewed_agent_task,
    create_assistant_command_work_item,
    decide_agent_work_item,
    detect_assistant_command_action,
    ensure_email_draft_for_work_item,
    ensure_reply_draft_for_work_item,
    extract_command_recipient,
    has_strong_command,
    generate_policy_memory_wiki_summary,
    get_agent_work_item,
    is_agent_task_awaiting_user_input,
    list_agent_work_items,
    list_policy_memory_summaries,
    resume_held_command_split_action,
    sync_connector_runs_to_agent_work,
    sync_data_gaps_to_agent_work,
    sync_promote_staging_to_agent_work,
    sync_wiki_tasks_to_agent_work,
    update_email_draft,
)
from orthus.agentwork import stream as agent_stream
from orthus.agentwork.app_chat_routing import should_route_app_chat
from orthus.agentwork.chat import (
    append_message as append_chat_message,
    create_session as create_chat_session,
    delete_session as delete_chat_session,
    get_session as get_chat_session,
    latest_waiting_input_request,
    list_sessions as list_chat_sessions,
    load_chat_history,
    rename_session as rename_chat_session,
)
from orthus.agentwork.hitl import (
    build_gate_question,
    mark_question_answered,
    merge_field_values,
    persist_question_message,
    resume_from_answer,
)
from orthus.agentwork.wiki_link import (
    AGENT_WORK_WIKI_LINK_LOOKBACK_LIMIT,
    enrich_agent_work_item,
    enrich_agent_work_items,
)
from sqlalchemy import select

from orthus.api.deps import get_current_user
from orthus.api.knowledge_token import get_session_user_or_knowledge_token
from orthus.auth import MANAGER_ROLES, AuthenticatedUser, require_node_operator
from orthus.collector.auth import effective_scopes
from orthus.collector.ws_registry import WS_REGISTRY
from orthus.db import session
from orthus.federation.query import should_federate
from orthus.router import answer, resolve_answer_scope
from orthus.schemas.canonical import (
    AgentChatMessage,
    AgentChatMessageCreate,
    AgentChatContinueRequest,
    AgentChatHitlResponseRequest,
    AgentChatHitlResponseResult,
    AgentChatOrchestrateRequest,
    AgentChatSession,
    AgentChatSessionCreate,
    AgentChatSessionDetail,
    AgentChatSessionUpdate,
    AgentChatWorkResultRequest,
    AgentWorkIntakeResult,
    AgentGatewayJobMirror,
    AgentDelegateTarget,
    AgentDelegateTargetList,
    AgentDelegateTargetNode,
    AgentFolderList,
    AgentWorkDelegateRequest,
    AgentWorkDraftEditRequest,
    AgentWorkItem,
    AgentWorkInboxSummary,
    AgentWorkNotifyState,
    AgentPolicyBucketSummary,
    AgentPolicyWikiSummary,
    AgentWorkReviewDecisionRequest,
    AgentWorkReviewDecisionResult,
    AgentWorkSyncResult,
    RoutedAnswer,
    SubAnswer,
)
from orthus.router.decompose import command_split_signal, is_command_leaf
from orthus.router.rewrite import needs_rewrite, resolve_followup
from orthus.settings import command_split_active, get_settings
from orthus.tables import agent_gateway_jobs, auth_identities, collector_tokens, users
from orthus.wiki.slug import clean_wiki_slug

router = APIRouter(prefix="/agent-work", tags=["agent-work"])

# SVC: 명령 리프가 read→act 게이트에 의해 **의도적으로 보류**됐음을 알리는 경고.
# "조용히 사라졌다"(command_split_command_recovered가 다루는 상황)와 구분된다.
_CMD_HELD_WARNING = "command_held_unreliable_upstream"


def _command_leaf_held_by_gate(parts: list[SubAnswer]) -> bool:
    """decompose의 read→act 게이트가 **명령형** 리프를 보류시켰는가(SVC).

    `_deps_unavailable`이 결정론 신호(0행/결과가 0 등)가 뜬 structured 상위 위에서
    명령 리프를 실행하지 않고 `upstream_unavailable`로 떨어뜨린 경우다. 판정은 게이트와
    **동일한 결정론 감지기**(`is_command_leaf`, LLM 0회)를 쓴다 — 두 곳이 다른 기준을
    쓰면 게이트가 막은 명령을 net이 되살린다.

    `error == "upstream_unavailable"`만 보면 과도하다(명령과 무관한 읽기 리프도 같은
    상태가 될 수 있다). 그래서 **그 리프 자신이 명령형인 경우**로 좁힌다. `text`는
    `_enrich_parts`(MA.6c)가 이미 wire에 채워 둔 기존 필드라 스키마 변경이 없다.
    """
    return any(
        part.error == "upstream_unavailable" and bool(part.text) and is_command_leaf(part.text)
        for part in parts
    )


_ALLOWED_STATES = {
    "pending",
    "classified",
    "auto_execute",
    "draft_for_review",
    "request_more_data",
    "rejected",
    "resolved",
    "dismissed",
}
_ALLOWED_OUTCOMES = {
    "auto_execute",
    "draft_for_review",
    "request_more_data",
    "reject",
}
_MAX_BATCH_IDS = 50  # S4: cap on GET /agent-work?ids= batch fetch


@router.get("", response_model=list[AgentWorkItem])
def list_work_items(
    state: str | None = Query(default=None),
    source_kind: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    wiki_slug: str | None = Query(default=None),
    ids: str | None = Query(default=None),
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> list[AgentWorkItem]:
    require_node_operator(current)
    if state is not None and state not in _ALLOWED_STATES:
        raise HTTPException(status_code=400, detail=f"invalid state: {state}")
    if outcome is not None and outcome not in _ALLOWED_OUTCOMES:
        raise HTTPException(status_code=400, detail=f"invalid outcome: {outcome}")
    if scope is not None and scope not in {"personal", "company"}:
        raise HTTPException(status_code=400, detail=f"invalid scope: {scope}")
    # S4 batch fetch: `?ids=<csv uuid>` polls the fragment work_ids of a decomposed answer in
    # one owner-scoped roundtrip. Capped (DoS / URL length) — decomposed parts are bounded by
    # ask_decompose_max_parts × waves, so 50 is generous.
    id_list: list[UUID] | None = None
    if ids is not None:
        raw = [p.strip() for p in ids.split(",") if p.strip()]
        if len(raw) > _MAX_BATCH_IDS:
            raise HTTPException(status_code=400, detail=f"too many ids (max {_MAX_BATCH_IDS})")
        try:
            id_list = [UUID(p) for p in raw]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid uuid in ids") from exc
        if not id_list:
            # C8: 빈 `?ids=`는 in_([])로 DB까지 갔다가 항상-거짓으로 []가 되던 경로다.
            # 의미는 동일하지만(빈 배치 폴 = 결과 없음, no-filter 아님) 여기서 명시
            # 반환해 no-filter로 오독될 여지와 불필요한 쿼리를 없앤다.
            return []
    raw_items = list_agent_work_items(
        current.user_id,
        state=state,
        source_kind=source_kind,
        outcome=outcome,
        scope=scope,
        ids=id_list,
        limit=AGENT_WORK_WIKI_LINK_LOOKBACK_LIMIT if wiki_slug is not None else 100,
    )
    # S4 badge poll (`?ids=`, no wiki_slug): the FE fragment chip reads only
    # state/title/recipient/reason_codes — never wiki_slugs — and this poll fires every
    # 5s per open chat tab. Skip the per-slug store.load_page disk enrichment it would
    # otherwise pay on every tick for a value no consumer reads. Any path that filters
    # by wiki_slug still enriches (it needs item.wiki_slugs to filter).
    if id_list is not None and wiki_slug is None:
        return raw_items
    items = _enrich_items(raw_items, current)
    if wiki_slug is not None:
        items = [item for item in items if wiki_slug in item.wiki_slugs]
    return items


@router.post("/delegate", response_model=AgentWorkItem)
def delegate_agent_task(
    body: AgentWorkDelegateRequest,
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentWorkItem:
    """Structured agent_task delegation: reuse the deterministic policy gate.

    This is the structured-field twin of the /ask natural-language delegation
    path. It builds the same agent_task work item through
    create_agent_task_work_item (candidate -> classify -> persist ->
    auto-execute dispatch); the LLM is never in the execution decision. A
    non-operator caller or an empty instruction yields 422 (no queue row),
    matching the assistant_command intake behavior.
    """
    require_node_operator(current)
    settings = get_settings()
    runner = (body.runner or "codex").strip().lower()
    mode = (body.mode or "knowledge").strip().lower()
    if runner not in {"codex", "claude", "hermes"}:
        raise HTTPException(status_code=422, detail=f"invalid runner: {runner}")
    if mode not in {"code", "knowledge"}:
        raise HTTPException(status_code=422, detail=f"invalid mode: {mode}")
    assignee = (body.assignee or "").strip() or str(current.user_id)
    chat_session_id: UUID | None = None
    if body.session_id:
        try:
            chat_session_id = UUID(body.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid session_id") from exc
        if get_chat_session(current.user_id, settings.node_id, chat_session_id) is None:
            raise HTTPException(status_code=404, detail="chat session not found")
    item = create_agent_task_work_item(
        current.user_id,
        actor_role=_agent_work_actor_role(current),
        assignee=assignee,
        mode=mode,
        runner=runner,
        instruction=body.instruction,
        cwd=(body.cwd or None),
        device_id=(body.device_id or None),
        chat_session_id=chat_session_id,
        settings=settings,
    )
    if item is None:
        raise HTTPException(
            status_code=422,
            detail="agent_task delegation not allowed or empty instruction",
        )
    return _enrich_item(item, current)


@router.get("/agent-folders", response_model=AgentFolderList)
def list_agent_folders(
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentFolderList:
    """Owner-scoped list of the caller's daemon-reported agent_task folders.

    Fail-closed: returns supported=False when agent_task delegation is off. The
    read is strictly owner-scoped (collector_tokens.user_id == caller) so one
    user can never see another user's run folders.
    """
    require_node_operator(current)
    settings = get_settings()
    if not settings.agent_task_enabled:
        return AgentFolderList(supported=False)
    return _owner_agent_folders(current.user_id)


@router.get("/delegate-targets", response_model=AgentDelegateTargetList)
def list_delegate_targets(
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentDelegateTargetList:
    """List delegatable teammates and their enrolled collector nodes + liveness.

    Fail-closed: returns supported=False when agent_task delegation is off. An
    owner/admin caller sees every user with an enrolled run-scope daemon on this
    node; a member caller sees only themselves. Self is always included (even
    with zero nodes) so the picker can show "내게 위임". Node liveness folds the
    live notify socket (realtime) plus collector poll recency into
    online/stale/offline. Read-only; no command is dispatched here.
    """
    require_node_operator(current)
    settings = get_settings()
    if not settings.agent_task_enabled:
        return AgentDelegateTargetList(supported=False, self_user_id=current.user_id, targets=[])

    role = _agent_work_actor_role(current)
    candidate_ids = _delegate_candidate_user_ids(
        current.user_id, role=role, node_id=settings.node_id
    )
    now = datetime.now(UTC)
    targets: list[AgentDelegateTarget] = []
    for user_id in candidate_ids:
        target = _build_delegate_target(
            user_id,
            is_self=user_id == current.user_id,
            node_id=settings.node_id,
            now=now,
        )
        # A non-self teammate with no runnable node is not delegatable -> drop.
        # Self is always kept so the picker can still offer "내게 위임".
        if not target.is_self and not target.nodes:
            continue
        targets.append(target)
    targets.sort(key=lambda t: (not t.is_self, t.display_name.casefold()))
    return AgentDelegateTargetList(supported=True, self_user_id=current.user_id, targets=targets)


@router.get("/policy-memory", response_model=list[AgentPolicyBucketSummary])
def list_policy_memory(
    current: AuthenticatedUser = Depends(get_current_user),
) -> list[AgentPolicyBucketSummary]:
    require_node_operator(current)
    return list_policy_memory_summaries(current.user_id)


@router.post("/policy-memory/wiki-summary", response_model=AgentPolicyWikiSummary)
def write_policy_memory_wiki_summary(
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentPolicyWikiSummary:
    require_node_operator(current)
    return generate_policy_memory_wiki_summary(current.user_id)


@router.get("/inbox-summary", response_model=AgentWorkInboxSummary)
def get_inbox_summary(
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> AgentWorkInboxSummary:
    require_node_operator(current)
    return build_inbox_summary(current.user_id)


@router.get("/notify-state", response_model=AgentWorkNotifyState)
def get_agent_work_notify_state(
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentWorkNotifyState:
    """Cheap checklist-badge signal for notifications (PR-N1).

    Mirrors GET /mail/notify-state (read-only count/max, safe to poll, no audit
    span) over the checklist-badge population: open agent-work items excluding
    wiki_task source and agent_task family, owner-scoped fail-closed. Session
    auth only (no dual-auth token); operator role required — the checklist list
    read this badge mirrors is operator-gated, so the signal must not widen the
    read boundary.
    """

    require_node_operator(current)
    return agent_work_notify_state(current.user_id)


@router.get("/gateway-jobs", response_model=list[AgentGatewayJobMirror])
def list_gateway_job_mirrors(
    current: AuthenticatedUser = Depends(get_current_user),
) -> list[AgentGatewayJobMirror]:
    """P10 C-7 — 내 게이트웨이 잡 정의 미러 조회 (read-only).

    strict owner-only (P8 fail-closed): owner_user_id = 로그인 유저 row만
    반환하며 admin도 타인 row를 보지 못한다. 데몬 로컬 SQLite가 실행 SoR이고
    이 화면은 조회 전용이다 — central 편집으로 로컬 실행을 바꾸는 경로는
    존재하지 않는다(spec §9 단방향). Dynamic GET /{work_id}보다 위에 선언."""
    with session() as s:
        rows = (
            s.execute(
                select(agent_gateway_jobs)
                .where(agent_gateway_jobs.c.owner_user_id == current.user_id)
                .order_by(agent_gateway_jobs.c.job_id)
            )
            .mappings()
            .all()
        )
    return [
        AgentGatewayJobMirror(
            job_id=row["job_id"],
            name=row["name"],
            instruction=row["instruction"],
            schedule=row["schedule"],
            schedule_human=row["schedule_human"],
            enabled=row["enabled"],
            last_run_at=row["last_run_at"],
            last_status=row["last_status"],
            last_error=row["last_error"],
            next_run_at=row["next_run_at"],
            mirrored_at=row["mirrored_at"],
        )
        for row in rows
    ]


# --- Agent-chat conversation sessions (Slice A, storage only) -------------------
# Declared ABOVE the dynamic GET /{work_id} so the static "chats" segment is
# matched first and never swallowed by the UUID path param. All endpoints are
# owner-scoped to current.user_id with node_id from settings.


@router.get("/chats", response_model=list[AgentChatSession])
def list_chat_sessions_route(
    current: AuthenticatedUser = Depends(get_current_user),
) -> list[AgentChatSession]:
    require_node_operator(current)
    return list_chat_sessions(current.user_id, get_settings().node_id)


@router.post("/chats", response_model=AgentChatSession)
def create_chat_session_route(
    body: AgentChatSessionCreate,
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentChatSession:
    require_node_operator(current)
    return create_chat_session(
        current.user_id,
        get_settings().node_id,
        title=(body.title or ""),
    )


@router.get("/chats/{session_id}", response_model=AgentChatSessionDetail)
def get_chat_session_route(
    session_id: UUID,
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentChatSessionDetail:
    require_node_operator(current)
    detail = get_chat_session(current.user_id, get_settings().node_id, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    return detail


@router.patch("/chats/{session_id}", response_model=AgentChatSession)
def rename_chat_session_route(
    session_id: UUID,
    body: AgentChatSessionUpdate,
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentChatSession:
    require_node_operator(current)
    updated = rename_chat_session(current.user_id, get_settings().node_id, session_id, body.title)
    if updated is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    return updated


@router.delete("/chats/{session_id}", status_code=204)
def delete_chat_session_route(
    session_id: UUID,
    current: AuthenticatedUser = Depends(get_current_user),
) -> None:
    require_node_operator(current)
    deleted = delete_chat_session(current.user_id, get_settings().node_id, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="chat session not found")


@router.post("/chats/{session_id}/messages", response_model=AgentChatMessage)
def append_chat_message_route(
    session_id: UUID,
    body: AgentChatMessageCreate,
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentChatMessage:
    require_node_operator(current)
    if body.role not in {"user", "agent"}:
        raise HTTPException(status_code=422, detail=f"invalid role: {body.role}")
    try:
        work_id = UUID(body.work_id) if body.work_id else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid work_id") from exc
    message = append_chat_message(
        current.user_id,
        get_settings().node_id,
        session_id,
        body.role,
        body.text,
        work_id=work_id,
    )
    if message is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    return message


# Slice C: poll-on-view delegation-result writeback. Declared ABOVE GET /{work_id}
# so the static "chats" segment matches first and is never swallowed by the UUID
# path param. POLL-ON-VIEW (not write-on-complete): the completer may differ from
# the chat owner and the completion path has no chat-session backref, so writing at
# completion would need a cross-owner write into owner-scoped chat. The poll reuses
# the existing owner-scoped reads.

# Terminal AgentWork states: nothing more will happen, so a result row is final.
_TERMINAL_WORK_STATES = {"resolved", "request_more_data", "rejected", "dismissed"}
# Result-message label prefixes. The dedup guard keys on "an agent message with this
# work_id whose text starts with one of these" — the original ack message carries the
# same work_id but never starts with a result label, so it is never confused with a
# result row (no new column / migration needed).
_WORK_RESULT_LABELS = ("[완료]", "[실패]", "[입력 필요]", "[정보 필요]", "[거부]")
_INPUT_REQUEST_LABEL = "[입력 필요]"


def _is_failed_agent_task(item: AgentWorkItem) -> bool:
    """True when a delegated agent_task dispatched and the run itself failed.

    resolve_agent_task_work_item_result maps a failed collector command to the
    `request_more_data` state plus the `auto_execute_failed` reason code. Without
    this check that run would render as a bland "[정보 필요]" — the same as a
    genuine policy-gate 'need more data'. Keying on the reason code + an
    agent_task auto_execution record distinguishes a real execution FAILURE so the
    chat shows a clear failure message.
    """
    if item.action_family != "agent_task":
        return False
    if "auto_execute_failed" not in (item.reason_codes or []):
        return False
    auto_execution = item.payload.get("auto_execution")
    return isinstance(auto_execution, dict) and auto_execution.get("kind") == "agent_task"


def _shape_work_result_text(item: AgentWorkItem) -> str:
    """Server-side result text for a terminal delegated work item. The result body is
    PII-redacted via _completion_result_summary / _completion_failure_summary (which
    unwrap the runner result and run redact_pii_text)."""
    if item.state == "resolved":
        # A delegated agent_task stores the runner result under
        # payload.auto_execution.result (resolve_agent_task_work_item_result).
        # _completion_result_summary tolerates a non-dict/empty result ("(결과 없음)").
        auto_execution = item.payload.get("auto_execution")
        result = auto_execution.get("result") if isinstance(auto_execution, dict) else None
        return f"[완료] {_completion_result_summary(result if isinstance(result, dict) else {})}"
    if item.state == "request_more_data":
        # A dispatched agent_task that FAILED lands here too (failed command ->
        # request_more_data); surface it as a real failure, not "need more data".
        if _is_failed_agent_task(item):
            auto_execution = item.payload.get("auto_execution")
            result = auto_execution.get("result") if isinstance(auto_execution, dict) else None
            return (
                f"[실패] {_completion_failure_summary(result if isinstance(result, dict) else {})}"
            )
        if is_agent_task_awaiting_user_input(item):
            auto_execution = item.payload.get("auto_execution")
            result = auto_execution.get("result") if isinstance(auto_execution, dict) else None
            return (
                f"{_INPUT_REQUEST_LABEL} "
                f"{_completion_result_summary(result if isinstance(result, dict) else {})}"
            )
        return "[정보 필요] 추가 정보가 필요해 검토 대기 중입니다."
    # rejected / dismissed
    return "[거부] 이 작업은 진행되지 않았습니다."


@router.post("/chats/{session_id}/work-result", response_model=AgentChatMessage | None)
def append_work_result_route(
    session_id: UUID,
    body: AgentChatWorkResultRequest,
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentChatMessage | None:
    """Append a delegated agent_task's completed result to the chat session ONCE.

    Owner-scoped on BOTH the session and the work item (non-owner/missing -> 404).
    Returns the appended (or existing) agent message; returns null/204 when the work
    item is not yet in a terminal state.
    """
    require_node_operator(current)
    node_id = get_settings().node_id
    detail = get_chat_session(current.user_id, node_id, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    try:
        work_id = UUID(body.work_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="invalid work_id") from exc
    item = get_agent_work_item(current.user_id, work_id)
    if item is None:
        raise HTTPException(status_code=404, detail="agent work item not found")
    if item.state not in _TERMINAL_WORK_STATES:
        # Nothing to append yet; the FE keeps polling.
        return None
    work_id_str = str(work_id)
    for message in detail.messages:
        if (
            message.role == "agent"
            and message.work_id == work_id_str
            and message.text.startswith(_WORK_RESULT_LABELS)
        ):
            # Idempotent: a result row for this (session, work_id) already exists.
            return message
    text = _shape_work_result_text(item)
    appended = append_chat_message(
        current.user_id,
        node_id,
        session_id,
        "agent",
        text,
        work_id=work_id,
    )
    if appended is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    return appended


@router.post("/chats/{session_id}/continue-agent-task", response_model=AgentWorkItem)
def continue_agent_task_route(
    session_id: UUID,
    body: AgentChatContinueRequest,
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentWorkItem:
    """Continue latest turn-based HITL agent_task in this chat session."""
    require_node_operator(current)
    settings = get_settings()
    detail = get_chat_session(current.user_id, settings.node_id, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="chat session not found")
    pending_work_id = _latest_input_request_work_id(detail.messages)
    if pending_work_id is None:
        raise HTTPException(status_code=409, detail="no pending agent input request")
    item = get_agent_work_item(current.user_id, pending_work_id)
    if item is None:
        raise HTTPException(status_code=404, detail="agent work item not found")
    if not is_agent_task_awaiting_user_input(item):
        raise HTTPException(status_code=409, detail="agent work item is not awaiting user input")
    next_item = continue_agent_task_after_user_input(
        current.user_id,
        item,
        answer=body.text,
        actor_role=_agent_work_actor_role(current),
        settings=settings,
    )
    if next_item is None:
        raise HTTPException(status_code=422, detail="agent input answer could not be continued")
    return _enrich_item(next_item, current)


@router.post("/chats/{session_id}/hitl-response")
def hitl_response_route(
    session_id: UUID,
    body: AgentChatHitlResponseRequest,
    current: AuthenticatedUser = Depends(get_current_user),
):
    """Answer a waiting HITL question and start the resume turn.

    Mirrors the continue-agent-task precedent but for the inline orchestrator's
    structured user_input_request messages. Fail-closed: 404 when the HITL flag is
    off (indistinguishable from not-found). Declared in the static chats block so
    it is matched before the dynamic GET /{work_id} routes."""
    require_node_operator(current)
    settings = get_settings()
    if not settings.agent_hitl_enabled:
        raise HTTPException(status_code=404, detail="not found")
    detail = get_chat_session(current.user_id, settings.node_id, session_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="chat session not found")

    question_msg = None
    for message in detail.messages:
        if message.message_id == body.message_id:
            question_msg = message
            break
    if question_msg is None or question_msg.kind != "user_input_request":
        raise HTTPException(status_code=409, detail="not a user input request")
    if (question_msg.payload or {}).get("status") != "waiting":
        raise HTTPException(status_code=409, detail="question is not waiting")
    latest = latest_waiting_input_request(detail.messages)
    if latest is None or latest.message_id != body.message_id:
        raise HTTPException(status_code=409, detail="not the latest pending question")

    input_type = (question_msg.payload or {}).get("input_type", "text")
    answer_text = (body.answer or "").strip()
    has_values = bool(body.values)
    if input_type == "approval":
        if body.decision not in ("approve", "reject"):
            raise HTTPException(status_code=422, detail="approval requires a decision")
    elif has_values:
        if answer_text:
            raise HTTPException(status_code=422, detail="provide either answer or values, not both")
        merged = merge_field_values((question_msg.payload or {}).get("fields") or [], body.values)
        if merged is None:
            raise HTTPException(status_code=422, detail="values do not match the question fields")
        answer_text = merged
    elif not answer_text:
        raise HTTPException(status_code=422, detail="answer is required")

    # Server-appended user response row (structured kinds are server-authored only).
    response_text = answer_text or ("승인" if body.decision == "approve" else "거부")
    response_payload = {"responds_to": body.message_id, "decision": body.decision}
    if has_values:
        response_payload["values"] = body.values
    response_message = append_chat_message(
        current.user_id,
        settings.node_id,
        session_id,
        "user",
        response_text,
        kind="user_input_response",
        payload=response_payload,
    )
    if response_message is None:
        raise HTTPException(status_code=404, detail="chat session not found")

    mark_question_answered(
        current.user_id, settings.node_id, session_id, question_msg, response_message.message_id
    )

    # Scope is not carried on the response body; the resume defaults it (gate-origin
    # resume ignores scope, agentic resume prefers the saved resume.scope).
    resolved_scope = resolve_answer_scope(None)
    routed = resume_from_answer(
        current.user_id,
        session_id,
        question_msg,
        answer=(answer_text or body.answer),
        decision=body.decision,
        actor_role=_agent_work_actor_role(current),
        auth_mode=current.auth_mode,
        scope=resolved_scope,
        project=None,
        context_wiki_slug=None,
        stream_id=body.stream_id,
        settings=settings,
    )
    # A follow-up agentic question persists as the next waiting user_input_request.
    if routed.user_input_request is not None:
        persisted = persist_question_message(
            current.user_id,
            settings.node_id,
            session_id,
            routed.user_input_request,
            settings=settings,
        )
        if persisted is not None:
            routed = routed.model_copy(
                update={
                    "user_input_request": routed.user_input_request.model_copy(
                        update={"message_id": persisted.message_id}
                    )
                }
            )

    result = AgentChatHitlResponseResult(routed=routed, response_message=response_message)
    payload = result.model_dump(mode="json")
    # Mirror orchestrate's 422 rule for a rejected structured resume.
    if routed.mode == "structured" and routed.structured is not None:
        if routed.structured.status == "rejected":
            return JSONResponse(status_code=422, content=payload)
    return payload


def _latest_input_request_work_id(messages: list[AgentChatMessage]) -> UUID | None:
    for message in reversed(messages):
        if message.role != "agent" or not message.work_id:
            continue
        if message.text.startswith(_INPUT_REQUEST_LABEL):
            try:
                return UUID(message.work_id)
            except ValueError:
                return None
        return None
    return None


def _agent_work_message(state: str) -> str:
    """User-facing note for a single-command intake result (mirrors the old ask.py)."""
    if state == "resolved":
        return "Executed by Agent Work policy gate."
    if state == "request_more_data":
        return "Queued in Agent Work for additional data or operator review."
    return "Queued in Agent Work for policy-gated review."


def _route_app_chat_to_daemon(
    current: AuthenticatedUser,
    body: AgentChatOrchestrateRequest,
    session_id: UUID,
    settings,
) -> dict | None:
    """P10.7 Part B: turn a personal chat turn into a self-`agent_task` on the
    caller's own enrolled daemon and return the intake RoutedAnswer.

    Reuses the deterministic delegation path (candidate -> policy gate -> persist
    -> auto-execute dispatch): a dispatched command lands on the caller's daemon,
    which answers on its resident engine and streams `agent_output` frames back
    over the existing per-work SSE endpoint (GET /agent-work/{work_id}/stream).
    Knowledge mode (no cwd) is the default for a chat turn; code-mode delegation
    stays on the explicit /agent-work/delegate route. Returns None when no work
    item is created (revoked daemon / suppressed candidate) so the caller falls
    through to the inline orchestrator.

    The user-facing message reflects the ACTUAL outcome: "실행 중" only when the
    command truly dispatched to a daemon; otherwise the honest policy-gate copy
    (e.g. queued for review) so a TOCTOU where the daemon vanished between the
    routing check and the create never claims a run that will not happen.
    """
    item = create_agent_task_work_item(
        current.user_id,
        actor_role=_agent_work_actor_role(current),
        assignee=str(current.user_id),  # self-delegation onto the caller's own daemon
        mode="knowledge",
        runner="codex",
        instruction=body.text,
        chat_session_id=session_id,
        settings=settings,
    )
    if item is None:
        return None
    auto_exec = (item.payload or {}).get("auto_execution") or {}
    dispatched = item.state == "auto_execute" and bool(auto_exec.get("command_id"))
    message = (
        "개인 에이전트(상주 데몬)에서 실행 중입니다."
        if dispatched
        else _agent_work_message(item.state)
    )
    return RoutedAnswer(
        question=body.text,
        mode="agent_work",
        warnings=["app_chat_routed_to_daemon"],
        agent_work=AgentWorkIntakeResult(
            work_id=item.work_id,
            source_kind=item.source_kind,
            source_ref_id=item.source_ref_id,
            action_family=item.action_family,
            title=item.title,
            state=item.state,
            policy_outcome=item.policy_outcome,
            policy_reason=item.policy_reason,
            reason_codes=item.reason_codes,
            message=message,
        ),
    ).model_dump(mode="json")


@router.post("/chats/{session_id}/orchestrate")
def orchestrate_chat_route(
    session_id: UUID,
    body: AgentChatOrchestrateRequest,
    current: AuthenticatedUser = Depends(get_current_user),
):
    """Agent-work chat orchestrator: the home of compound-question decompose +
    single-command intake (both moved off /ask, which is now search-only —
    docs/company-agent-orchestration.md §3).

    Synchronous + live SSE: the FE opens GET /agent-work/chats/stream/{stream_id}
    BEFORE awaiting this POST so decompose wave frames render live in the chat bubble.

    Returns a `RoutedAnswer` (mode agent_work | decomposed | wiki | structured | graph).
    A gate-rejected structured query surfaces as 422 carrying the compiled SQL +
    reason, mirroring the old /ask behavior. Declared in the static `chats` block so it
    is matched before the dynamic `GET /{work_id}` routes (:266 ordering rule)."""
    require_node_operator(current)
    settings = get_settings()
    if get_chat_session(current.user_id, settings.node_id, session_id) is None:
        raise HTTPException(status_code=404, detail="chat session not found")

    # Drop an unsafe/empty slug (mirrors the old AskIn validator) before it reaches
    # storage or grounding.
    context_wiki_slug = clean_wiki_slug(body.context_wiki_slug)
    resolved_scope = resolve_answer_scope(body.scope)

    # P10.7 Part B: route an explicitly personal-scope turn to the caller's own
    # enrolled resident daemon (self-delegation). Fail-closed via
    # ORTHUS_APP_CHAT_GATEWAY_ROUTING_ENABLED; company-scope search and the merged
    # scope=all company orchestrator below never enter here (spec §5). When the
    # gate passes but the item can't be created (daemon revoked between the check
    # and the create, or the candidate is suppressed), fall through to the inline
    # orchestrator so the turn is never silently dropped.
    if should_route_app_chat(
        user_id=current.user_id,
        actor_role=_agent_work_actor_role(current),
        scope=resolved_scope,
        settings=settings,
    ):
        routed = _route_app_chat_to_daemon(current, body, session_id, settings)
        if routed is not None:
            return routed

    # Single-command intake (case2): a command-shaped message becomes an
    # `assistant_command` Agent Work item. A lone non-compound command never decomposes
    # (should_decompose=False -> single search answer), so this block is load-bearing —
    # without it a command would silently become a search.
    #
    # command-split narrowing (BLOCKER F/G): when the command-split feature is active this
    # block is narrowed so a compound "명령+질문" input flows to answer()/decompose for
    # per-fragment routing instead of collapsing the whole text into one command. When the
    # feature is OFF the branch is byte-identical to the pre-command-split behavior.
    cs_active = command_split_active(settings)
    guard_degrade = False
    federated_compound = False
    if not cs_active:
        take_535 = detect_assistant_command_action(body.text) is not None
    else:
        # BLOCKER G: only a verified session owner/admin may escalate a command to a typed
        # action via command-split; non-session/non-operator callers fall through to pure
        # search (never queued at elevated authority).
        cmd_split_allowed = _command_split_allowed(current)
        # compound_split: this input would fan out per-fragment (signal + allowed). It must
        # ALSO require not-federated (BLOCKER F federated dead-zone): on a personal node with
        # scope=all the ladder skips decompose (should_federate), so skipping the 535 queue
        # here would drop the command entirely. When federated we keep take_535 so the command
        # is still queued (whole-text; the question part rides along — company-only limitation).
        compound_split = (
            cmd_split_allowed
            and command_split_signal(body.text)
            and not should_federate(resolved_scope)
        )
        # BLOCKER F: if the original carries a strong-command signal, LLM split could drop
        # the command clause. Degrade to a single-queue of the whole text + warn instead.
        guard_degrade = (
            compound_split
            and settings.ask_decompose_command_guard
            and has_strong_command(body.text)
        )
        detected = detect_assistant_command_action(body.text) is not None
        take_535 = cmd_split_allowed and detected and (not compound_split or guard_degrade)
        # review #7: on a federated node (personal, scope=all) compound_split is forced False
        # (should_federate), so a compound "명령+질문" is queued whole-text with no per-fragment
        # split AND the decompose ladder is also skipped downstream — the question clause is
        # never answered. guard_degrade doesn't fire here (it requires compound_split), so
        # without this the loss was silent. Flag it so the reviewer sees the question was dropped.
        federated_compound = (
            take_535
            and not guard_degrade
            and should_federate(resolved_scope)
            and command_split_signal(body.text)
        )
    if take_535:
        item = create_assistant_command_work_item(
            current.user_id,
            body.text,
            actor_role=_agent_work_actor_role(current),
            context_wiki_slug=context_wiki_slug,
            chat_session_id=session_id,
        )
        if item is not None:
            message = _agent_work_message(item.state)
            warnings: list[str] = []
            if guard_degrade:
                # Deterministic degrade copy (no recipient/fragment echo): the command was
                # queued but the compound's question part was not split off.
                message = message + " (복합 입력의 명령 부분만 등록했고 질문은 분리하지 못했습니다)"
                warnings = ["decompose_command_lost_degrade"]
            elif federated_compound:
                # review #7: federated node — command queued, question clause not answered.
                message = (
                    message
                    + " (federated 노드에서는 명령만 등록했고 질문 부분은 답변하지 못했습니다)"
                )
                warnings = ["federated_compound_question_not_answered"]
            # HITL: when the gate parked this command for more data, surface a
            # structured in-chat question instead of only a queue chip. flag off ⇒
            # user_input_request stays None and the response is byte-identical.
            user_input_request = None
            if settings.agent_hitl_enabled:
                uir = build_gate_question(item)
                if uir is not None:
                    persisted = persist_question_message(
                        current.user_id, settings.node_id, session_id, uir, settings=settings
                    )
                    if persisted is not None:
                        user_input_request = uir.model_copy(
                            update={"message_id": persisted.message_id}
                        )
            return RoutedAnswer(
                question=body.text,
                mode="agent_work",
                warnings=warnings,
                agent_work=AgentWorkIntakeResult(
                    work_id=item.work_id,
                    source_kind=item.source_kind,
                    source_ref_id=item.source_ref_id,
                    action_family=item.action_family,
                    title=item.title,
                    state=item.state,
                    policy_outcome=item.policy_outcome,
                    policy_reason=item.policy_reason,
                    reason_codes=item.reason_codes,
                    message=message,
                    # S4: homogenize with the case4 decomposed chip (recipient + command text).
                    recipient=extract_command_recipient(item.payload),
                    question_text=(item.payload or {}).get("question"),
                ),
                user_input_request=user_input_request,
            ).model_dump(mode="json")

    history = load_chat_history(
        current.user_id,
        settings.node_id,
        str(session_id),
        body.text,
    )
    # Follow-up reference rewrite (flag off ⇒ question_text == body.text, byte-identical).
    # Retrieval/routing below see only the current message text — history reaches the
    # answer prompt as a non-authoritative block, never retrieval — so an anaphoric
    # follow-up ("그거 내용 알려줘") retrieves garbage unless the reference is resolved
    # here first. Deterministic prefilter + fail-open (router/rewrite.py).
    question_text = body.text
    if settings.chat_followup_rewrite_enabled and needs_rewrite(body.text, history):
        question_text = resolve_followup(body.text, history)
    result: RoutedAnswer = answer(
        current.user_id,
        question_text,
        scope=resolved_scope,
        project=body.project,
        context_wiki_slug=context_wiki_slug,
        context_mail_id=body.context_mail_id,
        history=history,
        stream_id=body.stream_id,
        allow_agentic=True,
        # review #5: if the command-preservation guard fired (guard_degrade) but the 535
        # single-queue produced no item (candidate suppressed, e.g. the whole text redacted
        # empty), do NOT let this fall through into a command-split decompose — that would
        # re-split the very strong command the guard meant to protect. Force the legacy path.
        allow_decompose=not guard_degrade,
        # command-split (BLOCKER F/G): thread the real actor role + auth mode + chat session
        # so the ladder can gate command escalation to session owner/admin and the fan-out
        # can link queued items back to this chat (S2). When the feature flag is off these are
        # inert (cmd_split degrades to False).
        actor_role=_agent_work_actor_role(current),
        auth_mode=current.auth_mode,
        chat_session_id=session_id,
        # The chat surface always loads session history, so the company-scope semantic
        # answer cache (which requires no history, __init__.py cache_on) is structurally
        # off here; pass allow_cache=False to make that explicit.
        allow_cache=False,
    )
    if question_text != body.text:
        # `answer` echoes back whatever question it was given; restore the user's literal
        # message so the turn renders what they actually typed, and mark the rewrite in
        # warnings so a surprising retrieval is traceable to it.
        result = result.model_copy(
            update={
                "question": body.text,
                "warnings": [*result.warnings, "followup_reference_rewritten"],
            }
        )
    # review C: command-preservation net for the guard-off split path. With
    # ORTHUS_ASK_DECOMPOSE_COMMAND_GUARD off, a strong-command compound is allowed to split
    # (trusting the command-preserving split prompt) instead of degrading to a whole-text
    # queue. If the split nonetheless produced NO queued command among its leaves, the command
    # would be silently lost — the exact failure guard_degrade prevented pre-split. Recover it
    # by queuing the whole command as a held fallback (command_split_origin → held for review,
    # idempotent via the :f fallthrough digest). Only reachable when the guard is off (guard on
    # → take_535 above returned before this decompose), so guard-on behavior is unchanged.
    if (
        cs_active
        and not guard_degrade
        and result.mode == "decomposed"
        and has_strong_command(body.text)
        and not any(part.agent_work_id for part in result.parts)
    ):
        if _command_leaf_held_by_gate(result.parts):
            # SVC: 명령 리프가 **의도적으로 보류**된 것이지 사라진 게 아니다(read→act 게이트가
            # 가짜 0 위에서 행동을 막았다). net을 여기서 발동시키면 게이트가 막은 바로 그
            # 명령을 다시 큐잉해 상태가 모순된다 — FE는 "보류(skip)"를 보여주는데 큐에는
            # item이 있다(실서버 QA 발견). 손실이 아니므로 복구하지 않고, 보류 사실만 남긴다.
            result = result.model_copy(update={"warnings": [*result.warnings, _CMD_HELD_WARNING]})
        else:
            recovered = create_assistant_command_work_item(
                current.user_id,
                body.text,
                actor_role=_agent_work_actor_role(current),
                context_wiki_slug=context_wiki_slug,
                chat_session_id=session_id,
                command_split_origin=True,
                intake_source="decompose_fanout",
            )
            if recovered is not None:
                result = result.model_copy(
                    update={"warnings": [*result.warnings, "command_split_command_recovered"]}
                )
    # HITL (PR3): persist a turn-ending question so the FE renders the bubble and the
    # resume endpoint can find it. Single choke point (run_agentic_answer stays
    # storage-free). Two origins:
    #  - agentic: the ask_user tool already put a UserInputRequest on the result.
    #  - decomposed: a blocked command leaf (gate request_more_data + required_data)
    #    is projected into a question via build_gate_question. One pending question
    #    per turn (the resume endpoint enforces latest-waiting anyway).
    if settings.agent_hitl_enabled and result.user_input_request is None:
        pending_uir = None
        if result.mode == "decomposed":
            for part in result.parts:
                if part.agent_work_id is None:
                    continue
                item = get_agent_work_item(current.user_id, part.agent_work_id)
                if item is None:
                    continue
                candidate_uir = build_gate_question(item)
                if candidate_uir is not None:
                    pending_uir = candidate_uir
                    break
        if pending_uir is not None:
            persisted = persist_question_message(
                current.user_id, settings.node_id, session_id, pending_uir, settings=settings
            )
            if persisted is not None:
                result = result.model_copy(
                    update={
                        "user_input_request": pending_uir.model_copy(
                            update={"message_id": persisted.message_id}
                        )
                    }
                )
    elif settings.agent_hitl_enabled and result.user_input_request is not None:
        # agentic ask_user: persist the question and fill its message_id.
        persisted = persist_question_message(
            current.user_id,
            settings.node_id,
            session_id,
            result.user_input_request,
            settings=settings,
        )
        if persisted is not None:
            result = result.model_copy(
                update={
                    "user_input_request": result.user_input_request.model_copy(
                        update={"message_id": persisted.message_id}
                    )
                }
            )
    payload = result.model_dump(mode="json")
    # A rejected structured query is an explicit 422 (not 200); the body still carries
    # the compiled SQL + validation reason for the UI.
    if result.mode == "structured" and result.structured is not None:
        if result.structured.status == "rejected":
            return JSONResponse(status_code=422, content=payload)
    return payload


@router.get("/chats/stream/{stream_id}")
async def stream_chat_orchestrate(
    stream_id: str,
    request: Request,
    current: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """Live SSE of an agent-work chat orchestration (decompose/agentic) while it runs.

    Best-effort feedback only — the authoritative answer is the POST orchestrate JSON
    response. The FE generates `stream_id` (uuid4), opens this BEFORE awaiting the POST,
    and renders the frames in the chat "생성 중" bubble.

    Tier i (진행 버블): the legacy knowledge ladder now publishes phase frames
    (searching → grounded → answering) on every orchestrate turn, so the old
    fail-closed 404 gate on `agentic_ask_enabled or ask_decompose_enabled` no longer
    matches reality — the stream is always available for chat orchestration. This is
    safe to keep open: it is read-only, feedback-only, and owner-scoped by
    construction — the ring is keyed `{user_id}:{stream_id}` (an unguessable uuid4), so
    a caller only ever sees frames published under their own id. The colon-containing key
    can never collide with the bare-uuid `work_id` keys the agent_task SSE publishes into
    the same ring registry. Declared in the static `chats` block so it is matched before
    the dynamic `GET /{work_id}/stream`."""
    require_node_operator(current)

    stream_key = f"{current.user_id}:{stream_id}"
    backlog = agent_stream.recent(stream_key)
    queue = agent_stream.subscribe(stream_key)

    async def gen():
        try:
            yield ": connected\n\n"
            for frame in backlog:
                yield f"data: {json.dumps(frame)}\n\n"
                if frame.get("type") in {"turn_complete", "decompose_done"}:
                    return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keepalive through proxies
                    continue
                yield f"data: {json.dumps(frame)}\n\n"
                if frame.get("type") in {"turn_complete", "decompose_done"}:
                    break
        finally:
            agent_stream.unsubscribe(stream_key, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{work_id}/stream")
async def stream_work_item(
    work_id: UUID,
    request: Request,
    current: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """Live SSE of a dispatched agent_task's stdout/stderr while it runs.

    Browser-facing feedback channel: the collector daemon streams the agent's lines
    onto its outbound /collector/ws socket; the central WS reader fans them here by
    work_id, and this endpoint relays them to the owner's browser as SSE. The
    authoritative terminal result still arrives via the polled work-result writeback
    (the SoR is the HTTP complete_command path) — this only makes the in-flight run
    visible instead of a silent wait.

    Fail-closed: 404 unless company node + agent_task enabled + streaming enabled.
    Owner-scoped: the work item must exist and belong to the caller (same predicate
    as GET /{work_id}); streaming is gated by ownership, not by who produced frames.
    """
    settings = get_settings()
    require_node_operator(current)
    if not (
        settings.node_kind == "company"
        and settings.agent_task_enabled
        and settings.agent_task_streaming_enabled
    ):
        raise HTTPException(status_code=404, detail="agent work streaming not found")
    if get_agent_work_item(current.user_id, work_id) is None:
        raise HTTPException(status_code=404, detail="agent work item not found")

    work_key = str(work_id)
    backlog = agent_stream.recent(work_key)
    queue = agent_stream.subscribe(work_key)

    async def gen():
        try:
            yield ": connected\n\n"
            for frame in backlog:
                yield f"data: {json.dumps(frame)}\n\n"
                if frame.get("type") == "turn_complete":
                    return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # keepalive through proxies
                    continue
                yield f"data: {json.dumps(frame)}\n\n"
                if frame.get("type") == "turn_complete":
                    break
        finally:
            agent_stream.unsubscribe(work_key, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{work_id}", response_model=AgentWorkItem)
def get_work_item(
    work_id: UUID,
    current: AuthenticatedUser = Depends(get_session_user_or_knowledge_token),
) -> AgentWorkItem:
    require_node_operator(current)
    item = get_agent_work_item(current.user_id, work_id)
    if item is None:
        raise HTTPException(status_code=404, detail="agent work item not found")
    # Lazily generate the reply body when a reviewer opens a reply-draft item
    # (no-op for non-reply items).
    item = ensure_reply_draft_for_work_item(current.user_id, item)
    # S-LAZY: lazily generate the assistant_command email-template body on review
    # (LLM-backed; moved off queue time). Idempotent — no-op unless the item is an
    # email_send draft_for_review with a recipient_hint, no reply_context, and no
    # existing draft. Fires at most once (here or the approve pre-ensure below).
    item = ensure_email_draft_for_work_item(current.user_id, item)
    return _enrich_item(item, current)


@router.post("/{work_id}/decision", response_model=AgentWorkReviewDecisionResult)
async def decide_work_item(
    work_id: UUID,
    body: AgentWorkReviewDecisionRequest,
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentWorkReviewDecisionResult:
    require_node_operator(current)
    # P7.1: generate the reply body while the item is still draft_for_review.
    # ensure_reply_draft_for_work_item no-ops once state leaves draft_for_review,
    # so an approve that arrives before any GET would otherwise resolve the item
    # with no email_draft and silently send nothing. Pre-ensure closes that gap.
    # No-op for non-reply items.
    if body.decision == "approve":
        pre_item = get_agent_work_item(current.user_id, work_id)
        if pre_item is not None:
            ensure_reply_draft_for_work_item(current.user_id, pre_item)
            # S-LAZY: same pre-ensure for the assistant_command email-template draft
            # (LLM-backed, moved off queue time). Idempotent — no-op once state leaves
            # draft_for_review or a draft already exists, so an approve arriving before
            # any GET still records the email_draft telemetry for the no-edit guard.
            ensure_email_draft_for_work_item(current.user_id, pre_item)
    try:
        result = decide_agent_work_item(
            current.user_id,
            work_id,
            body.decision,
            note=body.note,
            no_edit=body.no_edit,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_error_detail(exc)) from exc

    item = result.item
    # An LLM-inferred mail delegation does not auto-execute (agentwork/state.py): the
    # "is this a delegation?" judgment is the LLM's, over untrusted inbound text, and a
    # false positive runs a full-access agent on a teammate's machine. So the dispatch
    # happens here, on a human's approve, and only for a reviewer with operator
    # authority. Mirrors the P7.1 approve-then-send below.
    if (
        body.decision == "approve"
        and item.action_family == "agent_task"
        and item.state == "resolved"
        and item.payload.get("llm_inferred") is True
    ):
        item = execute_reviewed_agent_task(
            current.user_id,
            item,
            reviewer_role=current.role,
            settings=get_settings(),
        )

    # P7.1: an approved reply draft triggers the real send through the P6.3
    # adapter. The send outcome is persisted via email_send_log + audit; it is
    # also attached to the response payload (in-memory) for the reviewer.
    if (
        body.decision == "approve"
        and item.action_family == "email_send"
        and item.state == "resolved"
        and isinstance(item.payload.get("reply_context"), dict)
    ):
        from orthus.mail.reply import send_approved_reply

        send_outcome = await send_approved_reply(
            item,
            reviewer_id=current.user_id,
            reviewer_role=current.role,
            settings=get_settings(),
        )
        next_payload = dict(item.payload)
        next_payload["reply_send"] = send_outcome
        item = item.model_copy(update={"payload": next_payload})
        result = result.model_copy(update={"item": item})

    # review #2: a held command-split external-write fragment (connector_sync / board cleanup /
    # agent_task) had its queue-time auto-execution deferred by _hold_command_split_auto_execute.
    # Approval is the deferred execution — resume the real typed action now (mirrors the email
    # reply approve→send branch above). No-op unless the item is actually a held auto_execute
    # fragment; the execute guards re-validate every precondition.
    if (
        body.decision == "approve"
        and item.state == "resolved"
        and "command_split_hold" in (item.reason_codes or [])
    ):
        item = resume_held_command_split_action(current.user_id, item, settings=get_settings())
        result = result.model_copy(update={"item": item})

    return result.model_copy(update={"item": _enrich_item(result.item, current)})


@router.post("/{work_id}/draft", response_model=AgentWorkItem)
def edit_work_item_draft(
    work_id: UUID,
    body: AgentWorkDraftEditRequest,
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentWorkItem:
    """Reviewer edits a reply/email draft body before approving it (owner/admin)."""
    require_node_operator(current)
    try:
        item = update_email_draft(
            current.user_id,
            work_id,
            body_template=body.body_template,
            subject_hint=body.subject_hint,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=_error_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_error_detail(exc)) from exc
    return _enrich_item(item, current)


@router.post("/sync/data-gaps", response_model=AgentWorkSyncResult)
def sync_data_gaps(
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentWorkSyncResult:
    require_node_operator(current)
    return _enrich_sync_result(sync_data_gaps_to_agent_work(current.user_id), current)


@router.post("/sync/wiki-tasks", response_model=AgentWorkSyncResult)
def sync_wiki_tasks(
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentWorkSyncResult:
    require_node_operator(current)
    return _enrich_sync_result(sync_wiki_tasks_to_agent_work(current.user_id), current)


@router.post("/sync/promote-staging", response_model=AgentWorkSyncResult)
def sync_promote_staging(
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentWorkSyncResult:
    require_node_operator(current)
    return _enrich_sync_result(sync_promote_staging_to_agent_work(current.user_id), current)


@router.post("/sync/connector-runs", response_model=AgentWorkSyncResult)
def sync_connector_runs(
    current: AuthenticatedUser = Depends(get_current_user),
) -> AgentWorkSyncResult:
    require_node_operator(current)
    return _enrich_sync_result(sync_connector_runs_to_agent_work(current.user_id), current)


def _error_detail(exc: Exception) -> str:
    return str(exc.args[0]) if exc.args else str(exc)


def _enrich_item(item: AgentWorkItem, current: AuthenticatedUser) -> AgentWorkItem:
    return enrich_agent_work_item(item, user_id=current.user_id, settings=get_settings())


def _enrich_items(items: list[AgentWorkItem], current: AuthenticatedUser) -> list[AgentWorkItem]:
    return enrich_agent_work_items(items, user_id=current.user_id, settings=get_settings())


def _enrich_sync_result(
    result: AgentWorkSyncResult,
    current: AuthenticatedUser,
) -> AgentWorkSyncResult:
    return result.model_copy(update={"items": _enrich_items(result.items, current)})


def _agent_work_actor_role(current: AuthenticatedUser) -> str:
    """Resolve the policy-gate actor role (mirrors ask.py)."""
    if current.auth_mode == "session" and current.role:
        return current.role
    return "owner"


def _command_split_allowed(current: AuthenticatedUser) -> bool:
    """command_split(명령을 typed action으로 상승/분할)은 검증된 session 로그인 +
    owner/admin일 때만 허용한다(BLOCKER G). 비-session(demo/jwt)/비-operator는
    command_split=False로 강등해 명령을 순수 검색으로 처리한다 — require_node_operator의
    session-only fail-open이 이 경로에서 명령을 최고 권한으로 상승시키는 것을 차단한다."""
    return current.auth_mode == "session" and current.role in MANAGER_ROLES


def _owner_agent_folders(user_id) -> AgentFolderList:
    """Union the owner's live collector_tokens.agent_workspaces, newest first.

    The default comes from the most-recently-reported token; recent cwds are
    deduped across tokens preserving most-recent-first order; reported_at is
    the max last_status_at among the rows that carried workspaces.
    """
    with session() as s:
        rows = s.execute(
            select(
                collector_tokens.c.agent_workspaces,
                collector_tokens.c.last_status_at,
            )
            .where(
                collector_tokens.c.user_id == user_id,
                collector_tokens.c.revoked_at.is_(None),
            )
            .order_by(collector_tokens.c.last_status_at.desc().nullslast())
        ).all()
    default: str | None = None
    recent: list[str] = []
    seen: set[str] = set()
    reported_at = None
    for row in rows:
        workspaces = row.agent_workspaces
        if not isinstance(workspaces, dict):
            continue
        if reported_at is None and row.last_status_at is not None:
            reported_at = row.last_status_at
        if default is None:
            candidate = workspaces.get("default")
            if isinstance(candidate, str) and candidate.strip():
                default = candidate
        for cwd in workspaces.get("recent") or []:
            if isinstance(cwd, str) and cwd.strip() and cwd not in seen:
                seen.add(cwd)
                recent.append(cwd)
    return AgentFolderList(
        supported=True,
        default=default,
        recent=recent,
        reported_at=reported_at,
    )


# --- delegate-targets helpers -------------------------------------------------
# Mirror the collector liveness windows so node status here matches the collector
# health surface (live <=30min, stale <=2h, else offline).
_DELEGATE_LIVE_WINDOW = timedelta(minutes=30)
_DELEGATE_STALE_WINDOW = timedelta(hours=2)
_DELEGATE_RUN_SCOPES = frozenset({"agent_task", "commands"})


def _delegate_candidate_user_ids(caller_id: UUID, *, role: str, node_id: str) -> list[UUID]:
    """Resolve which users the caller may delegate to (self always included).

    owner/admin -> every user with >=1 non-revoked run-scope collector token on
    this node; member -> only self. Self is appended even with zero qualifying
    nodes so the picker can always offer "내게 위임".
    """
    ids: list[UUID] = []
    seen: set[UUID] = set()
    if role in {"owner", "admin"}:
        with session() as s:
            rows = s.execute(
                select(collector_tokens.c.user_id, collector_tokens.c.scopes).where(
                    collector_tokens.c.node_id == node_id,
                    collector_tokens.c.revoked_at.is_(None),
                )
            ).all()
        for row in rows:
            if not (effective_scopes(frozenset(row.scopes or [])) & _DELEGATE_RUN_SCOPES):
                continue
            if row.user_id in seen:
                continue
            seen.add(row.user_id)
            ids.append(row.user_id)
    if caller_id not in seen:
        ids.append(caller_id)
    return ids


def _build_delegate_target(
    user_id: UUID, *, is_self: bool, node_id: str, now: datetime
) -> AgentDelegateTarget:
    """Build one delegate target from a user's non-revoked run-scope tokens."""
    with session() as s:
        profile = s.execute(select(users.c.display_name).where(users.c.user_id == user_id)).first()
        email_row = s.execute(
            select(auth_identities.c.email)
            .where(auth_identities.c.user_id == user_id)
            .order_by(auth_identities.c.created_at.desc())
        ).first()
        token_rows = s.execute(
            select(
                collector_tokens.c.name,
                collector_tokens.c.scopes,
                collector_tokens.c.device_id,
                collector_tokens.c.last_polled_at,
                collector_tokens.c.agent_workspaces,
            ).where(
                collector_tokens.c.user_id == user_id,
                collector_tokens.c.node_id == node_id,
                collector_tokens.c.revoked_at.is_(None),
            )
        ).all()
    connected = WS_REGISTRY.connected_device_ids(user_id)
    # Keep only tokens that have actually run a daemon: polled at least once OR
    # live on a WS notify socket right now. Never-polled, non-connected tokens
    # are CLI registration artifacts (the "device".."device-16" sprawl), not real
    # nodes. Among deviceless tokens (device_id == "") collapse to the single
    # most-recently-polled one so a user shows one "기본 노드", not duplicates;
    # non-empty device_ids are unique per user (DB partial index).
    usable_rows = []
    best_deviceless = None
    for row in token_rows:
        if not (effective_scopes(frozenset(row.scopes or [])) & _DELEGATE_RUN_SCOPES):
            continue
        device_id = row.device_id or ""
        if row.last_polled_at is None and device_id not in connected:
            continue
        if device_id == "":
            if best_deviceless is None or _polled_after(row, best_deviceless):
                best_deviceless = row
            continue
        usable_rows.append(row)
    if best_deviceless is not None:
        usable_rows.append(best_deviceless)

    nodes: list[AgentDelegateTargetNode] = []
    for row in usable_rows:
        device_id = row.device_id or ""
        realtime = device_id in connected
        status = _delegate_node_status(row.last_polled_at, realtime=realtime, now=now)
        default_folder: str | None = None
        recent_folders: list[str] = []
        workspaces = row.agent_workspaces
        if isinstance(workspaces, dict):
            candidate = workspaces.get("default")
            if isinstance(candidate, str) and candidate.strip():
                default_folder = candidate
            for cwd in workspaces.get("recent") or []:
                if isinstance(cwd, str) and cwd.strip():
                    recent_folders.append(cwd)
        label = (row.name or "").strip() or device_id.strip() or "기본 노드"
        nodes.append(
            AgentDelegateTargetNode(
                device_id=device_id or None,
                label=label,
                status=status,
                realtime=realtime,
                last_polled_at=row.last_polled_at,
                default_folder=default_folder,
                recent_folders=recent_folders,
            )
        )
    # online first, then by last_polled_at desc (nulls last).
    nodes.sort(
        key=lambda n: (
            n.status != "online",
            n.last_polled_at is None,
            -(n.last_polled_at.timestamp() if n.last_polled_at is not None else 0.0),
        )
    )
    return AgentDelegateTarget(
        user_id=user_id,
        display_name=(profile.display_name if profile else "") or str(user_id),
        email=email_row.email if email_row else None,
        is_self=is_self,
        online=any(n.status == "online" for n in nodes),
        nodes=nodes,
    )


def _delegate_node_status(last_polled_at: datetime | None, *, realtime: bool, now: datetime) -> str:
    """online if realtime or polled <=30min ago; stale <=2h; else offline."""
    if realtime:
        return "online"
    if last_polled_at is None:
        return "offline"
    age = now - _as_aware_utc(last_polled_at)
    if age <= _DELEGATE_LIVE_WINDOW:
        return "online"
    if age <= _DELEGATE_STALE_WINDOW:
        return "stale"
    return "offline"


def _polled_after(row, other) -> bool:
    """True when ``row`` was polled more recently than ``other`` (None = oldest)."""
    a = row.last_polled_at
    b = other.last_polled_at
    if b is None:
        return a is not None
    if a is None:
        return False
    return _as_aware_utc(a) > _as_aware_utc(b)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
