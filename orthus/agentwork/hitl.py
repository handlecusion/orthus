"""Human-in-the-Loop for the agent-work chat orchestrator.

The orchestrator can end a turn with a structured question to the chat owner
instead of guessing or silently queuing. Two origins:

- "policy_gate": deterministically projected from a policy-gate request_more_data
  outcome (the gate already computed `payload.required_data`). No LLM — the
  question is a pure function of the gate output (설계 원칙 1). Resume re-runs the
  SAME gate with the answer merged in.
- "agentic": raised by the Solar agentic loop's ask_user tool (PR3). Resume
  runs a fresh answer() turn carrying the saved context.

State model (turn-ending, no live process waiting):
  1. build_gate_question(item) -> UserInputRequest | None    (pure)
  2. persist_question_message(...)  -> chat row + work-item pending marker
  3. user answers via POST /agent-work/chats/{id}/hitl-response
  4. mark_question_answered(...) + resume_from_answer(...) -> new turn

This module is pure orchestration glue: it never modifies the deterministic
policy gate (orthus/agentwork/state.py) and every write goes through the
owner-scoped chat / service layers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from orthus.agentwork import chat as agent_chat
from orthus.agentwork.service import (
    create_assistant_command_work_item,
    extract_command_recipient,
    mark_work_item_user_input_answered,
)
from orthus.audit import audit, redact_pii_text
from orthus.schemas.canonical import (
    AgentChatMessage,
    AgentWorkIntakeResult,
    AgentWorkItem,
    RoutedAnswer,
    UserInputField,
    UserInputRequest,
)
from orthus.settings import Settings, get_settings

# Reason code stamped on a gate-origin item once its question is answered (mirrors
# the agent_task "awaiting_user_input_answered" convention).
HITL_ANSWERED_REASON = "hitl_answered"

# action_families whose request_more_data already has a dedicated resume path and
# must NOT be double-driven by the orchestrator HITL question:
#  - agent_task: the delegated turn-based [입력 필요] flow owns pending_user_input.
_HITL_EXCLUDED_FAMILIES = frozenset({"agent_task"})

# The one declared form field of slice 1: the email gate's missing recipient.
_EMAIL_RECIPIENT_FIELD = UserInputField(
    name="recipient", label="받는 사람", placeholder="이름 또는 이메일", required=True
)


def _recipient_required(action_family: str | None, required: list) -> bool:
    """Single predicate shared by field projection AND answer weaving so a
    recipient form always resumes through the recipient merge branch."""
    return action_family == "email_send" and any(
        "recipient" in str(r).lower() or "수신" in str(r) for r in required
    )


def _gate_fields(action_family: str | None, required: list) -> list[UserInputField]:
    if _recipient_required(action_family, required):
        return [_EMAIL_RECIPIENT_FIELD]
    return []


def merge_field_values(fields: list[dict], values: dict[str, str]) -> str | None:
    """Deterministically merge a form submission into one answer text.
    None ⇒ invalid submission (route returns 422). Rules:
      - every submitted key must be a declared field name
      - every required field must have a non-blank value
      - single declared field → its bare value (keeps _merge_gate_answer's
        recipient weaving byte-identical to a composer answer)
      - multi field (schema-open, unused in slice 1) → "label: value" lines
    """
    declared = [f for f in fields if isinstance(f, dict) and str(f.get("name") or "").strip()]
    if not declared:
        return None
    declared_names = {str(f["name"]) for f in declared}
    if any(key not in declared_names for key in values):
        return None
    for f in declared:
        if f.get("required", True) and not str(values.get(str(f["name"])) or "").strip():
            return None
    if len(declared) == 1:
        value = str(values.get(str(declared[0]["name"])) or "").strip()
        return value or None
    lines = []
    for f in declared:
        value = str(values.get(str(f["name"])) or "").strip()
        if value:
            label = str(f.get("label") or "").strip() or str(f["name"])
            lines.append(f"{label}: {value}")
    return "\n".join(lines) or None


def build_gate_question(item: AgentWorkItem) -> UserInputRequest | None:
    """Project a policy-gate request_more_data outcome into a chat question.

    Deterministic (no LLM): returns a text question when the gate parked the item
    for more data (`payload.required_data`), and None otherwise. Excludes items
    that already have their own resume path (agent_task awaiting-input, a held
    command-split action) so the same item is never driven from two mechanisms.
    """
    if item.state != "request_more_data":
        return None
    if item.action_family in _HITL_EXCLUDED_FAMILIES:
        return None
    reason_codes = item.reason_codes or []
    if "command_split_hold" in reason_codes:
        return None
    payload = item.payload or {}
    required = payload.get("required_data")
    if not isinstance(required, list) or not required:
        return None
    required_text = ", ".join(str(r) for r in required if str(r).strip())
    if not required_text:
        return None
    question = f"이 작업을 진행하려면 추가 정보가 필요합니다: {required_text}"
    original_question = str(payload.get("question") or item.title or "").strip()
    return UserInputRequest(
        question=question,
        input_type="text",
        fields=_gate_fields(item.action_family, required),
        origin="policy_gate",
        work_id=str(item.work_id),
        status="waiting",
        resume={
            "original_question": original_question,
            "action_family": item.action_family,
            "required_data": [str(r) for r in required],
        },
    )


def persist_question_message(
    user_id: UUID,
    node_id: str,
    session_id: UUID,
    req: UserInputRequest,
    *,
    stamp_pending: bool = True,
    settings: Settings | None = None,
) -> AgentChatMessage | None:
    """Append the question as a user_input_request chat message and (for a
    gate-origin question) stamp the work item with a pending_user_input marker so
    the queue reflects that it is waiting on the chat owner.

    Returns the persisted message (with message_id filled into the payload). None
    if the session is missing/non-owner (caller -> 404).
    """
    work_id = None
    if req.work_id:
        try:
            work_id = UUID(str(req.work_id))
        except (ValueError, TypeError):
            work_id = None
    with audit("agent_chat.hitl_question") as span:
        # message_id is not known until insert; persist with a placeholder-free
        # payload, then re-write the payload with the final message_id.
        msg = agent_chat.append_message(
            user_id,
            node_id,
            session_id,
            role="agent",
            text=req.question,
            work_id=work_id,
            kind="user_input_request",
            payload=req.model_dump(),
        )
        if msg is None:
            return None
        filled = req.model_copy(update={"message_id": msg.message_id})
        msg = agent_chat.update_message_payload(
            user_id, node_id, session_id, UUID(msg.message_id), filled.model_dump()
        )
        span.add_meta(
            session_id=str(session_id),
            origin=req.origin,
            input_type=req.input_type,
            work_id=(str(work_id) if work_id else None),
        )
    if stamp_pending and req.origin == "policy_gate" and work_id is not None and msg is not None:
        _stamp_pending_user_input(user_id, work_id, msg.message_id, settings=settings)
    return msg


def _stamp_pending_user_input(
    user_id: UUID,
    work_id: UUID,
    message_id: str,
    *,
    settings: Settings | None = None,
) -> None:
    """Mark a gate-origin work item as waiting on chat-owner input (payload only;
    no state change — it is already request_more_data)."""
    from sqlalchemy import update

    from orthus.agentwork.service import get_agent_work_item
    from orthus.db import session as db_session
    from orthus.tables import agent_work_items

    settings = settings or get_settings()
    item = get_agent_work_item(user_id, work_id, settings=settings)
    if item is None:
        return
    next_payload = dict(item.payload or {})
    next_payload["pending_user_input"] = {
        "status": "waiting",
        "requested_at": datetime.now(UTC).isoformat(),
        "message_id": message_id,
        "origin": "policy_gate",
    }
    with db_session() as s:
        s.execute(
            update(agent_work_items)
            .where(agent_work_items.c.work_id == work_id)
            .values(payload=next_payload, updated_at=datetime.now(UTC))
        )
        s.commit()


def mark_question_answered(
    user_id: UUID,
    node_id: str,
    session_id: UUID,
    question_msg: AgentChatMessage,
    answer_message_id: str,
) -> AgentChatMessage | None:
    """Flip a waiting user_input_request message to status="answered"."""
    payload = dict(question_msg.payload or {})
    payload["status"] = "answered"
    payload["answered_at"] = datetime.now(UTC).isoformat()
    payload["answer_message_id"] = answer_message_id
    return agent_chat.update_message_payload(
        user_id, node_id, session_id, UUID(question_msg.message_id), payload
    )


def resume_from_answer(
    user_id: UUID,
    session_id: UUID,
    question_msg: AgentChatMessage,
    *,
    answer: str | None,
    decision: str | None,
    actor_role: str | None,
    auth_mode: str | None,
    scope: str,
    project: str | None,
    context_wiki_slug: str | None,
    stream_id: str | None,
    settings: Settings | None = None,
) -> RoutedAnswer:
    """Start the resume turn after the chat owner answers a HITL question.

    origin="policy_gate": merge the answer into the original command and re-run it
    through create_assistant_command_work_item (the SAME deterministic gate). The
    originally-blocked item is marked answered + resolved.

    origin="agentic": run a fresh answer() turn with the saved context.
    """
    settings = settings or get_settings()
    payload = question_msg.payload or {}
    origin = payload.get("origin")
    resume = payload.get("resume") or {}
    answer_text = redact_pii_text((answer or "").strip())
    with audit("agent_chat.hitl_response") as span:
        span.add_meta(
            session_id=str(session_id),
            message_id=question_msg.message_id,
            origin=str(origin),
        )
        if origin == "policy_gate":
            return _resume_policy_gate(
                user_id,
                session_id,
                question_msg,
                answer_text=answer_text,
                actor_role=actor_role,
                context_wiki_slug=context_wiki_slug,
                resume=resume,
                settings=settings,
            )
        # agentic origin (PR3): a fresh turn carrying the saved context.
        return _resume_agentic(
            user_id,
            session_id,
            answer_text=answer_text or (decision or ""),
            actor_role=actor_role,
            auth_mode=auth_mode,
            scope=resume.get("scope") or scope,
            project=resume.get("project") or project,
            context_wiki_slug=resume.get("context_wiki_slug") or context_wiki_slug,
            stream_id=stream_id,
            resume=resume,
        )


def _resume_policy_gate(
    user_id: UUID,
    session_id: UUID,
    question_msg: AgentChatMessage,
    *,
    answer_text: str,
    actor_role: str | None,
    context_wiki_slug: str | None,
    resume: dict,
    settings: Settings,
) -> RoutedAnswer:
    original = str(resume.get("original_question") or "").strip()
    action_family = resume.get("action_family")
    merged = _merge_gate_answer(original, answer_text, action_family, resume)
    item = create_assistant_command_work_item(
        user_id,
        merged,
        actor_role=actor_role,
        context_wiki_slug=context_wiki_slug,
        # The caller already decided the family at the first turn; keep it so the
        # merged text is not re-classified by keyword detection.
        action_family=action_family if isinstance(action_family, str) else None,
        intake_source="hitl_resume",
        settings=settings,
    )
    # Mark the originally-blocked item answered + resolved so it leaves the queue.
    blocked_work_id = question_msg.work_id
    if blocked_work_id:
        from orthus.agentwork.service import get_agent_work_item

        try:
            blocked = get_agent_work_item(user_id, UUID(blocked_work_id), settings=settings)
        except (ValueError, TypeError):
            blocked = None
        if blocked is not None:
            mark_work_item_user_input_answered(
                blocked,
                answer_ref=question_msg.message_id,
                answer_ref_key="answer_message_id",
                reason_code=HITL_ANSWERED_REASON,
                audit_name="agent_chat.hitl_answered",
            )
    if item is None:
        return RoutedAnswer(
            question=merged,
            mode="agent_work",
            warnings=["hitl_resume_no_item"],
        )
    return _intake_routed_answer(item, merged)


def _merge_gate_answer(
    original: str, answer_text: str, action_family: str | None, resume: dict
) -> str:
    """Build the resume command text.

    For email_send with a missing recipient, the recipient regex only matches
    "X에게 메일" shaped text, so a bare-name answer ("김민수") must be woven into
    that shape or the resume would loop on the same missing-recipient gate. A
    literal email address answer is matched anywhere by the address regex, so this
    template is safe either way. Other families just append the extra info.
    """
    if not answer_text:
        return original
    required = resume.get("required_data") or []
    if _recipient_required(action_family, required):
        return f"{answer_text}에게 {original}".strip()
    return f"{original}\n(사용자 추가 정보: {answer_text})".strip()


def _resume_agentic(
    user_id: UUID,
    session_id: UUID,
    *,
    answer_text: str,
    actor_role: str | None,
    auth_mode: str | None,
    scope: str,
    project: str | None,
    context_wiki_slug: str | None,
    stream_id: str | None,
    resume: dict,
) -> RoutedAnswer:
    # Lazy import: router imports the agentwork package, so import answer() at call
    # time to avoid an import cycle.
    from orthus.router import answer as router_answer

    settings = get_settings()
    original = str(resume.get("original_question") or "").strip()
    merged = f"{original}\n(사용자 답변: {answer_text})".strip() if original else answer_text
    history = agent_chat.load_chat_history(user_id, settings.node_id, str(session_id), merged)
    return router_answer(
        user_id,
        merged,
        scope=scope,
        project=project,
        context_wiki_slug=context_wiki_slug,
        history=history,
        stream_id=stream_id,
        allow_agentic=True,
        allow_decompose=False,
        allow_cache=False,
        actor_role=actor_role,
        auth_mode=auth_mode,
        chat_session_id=session_id,
    )


def _intake_routed_answer(item: AgentWorkItem, question: str) -> RoutedAnswer:
    """Shape a created assistant_command item into the same RoutedAnswer the
    orchestrator's single-command intake branch returns."""
    return RoutedAnswer(
        question=question,
        mode="agent_work",
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
            message=_intake_message(item.state),
            recipient=extract_command_recipient(item.payload),
            question_text=(item.payload or {}).get("question"),
        ),
    )


def _intake_message(state: str) -> str:
    if state == "resolved":
        return "Executed by Agent Work policy gate."
    if state == "request_more_data":
        return "Queued in Agent Work for additional data or operator review."
    return "Queued in Agent Work for policy-gated review."
