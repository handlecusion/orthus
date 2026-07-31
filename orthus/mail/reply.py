"""P7.1 mail reply draft transport + approved-reply real send.

This module owns transport for reply drafts: it builds the reply candidate from
an inbound mail, and performs the REAL send for an approved reply draft through
the P6.3 `send_mail` adapter (no raw SMTP/provider). Auto-send POLICY lives in
`orthus.agentwork.autosend`; this module never auto-sends.

Import boundary: `orthus.mail.reply` may import `orthus.agentwork` (the route and
ingest call into it), but `orthus.agentwork` must NOT import `orthus.mail`.
`persist_reply_candidate` is imported lazily by `orthus.mail.ingest`, never here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from orthus.audit import audit
from orthus.email.log import (
    email_hash,
    email_send_log_exists,
    email_send_rate_limited,
    insert_email_send_log,
)
from orthus.mail.ingest import mail_source_canonical_id
from orthus.mail.send import (
    MailSendRoutingError,
    MailSendUnconfiguredError,
    backend_for_from_addr,
    send_mail,
)
from orthus.schemas.canonical import (
    AgentWorkCandidate,
    AgentWorkItem,
    MailIngestRequest,
    MailReplyContext,
    MailSendRequest,
)
from orthus.settings import Settings

_THREAD_EXCERPT_MAX = 2000
_REPLY_OPERATOR_ROLES = {"owner", "admin"}


def build_reply_candidate(
    payload: MailIngestRequest,
    owner_user_id: UUID,
    settings: Settings,
) -> AgentWorkCandidate | None:
    """Build an email_send reply-draft candidate for a newly-ingested inbound mail.

    Returns None unless: inbound direction, reply-draft flag on, company node, and
    at least one of the recipient addresses (`to_addr`) routes via a send backend.
    The reply From is that routable company address; the reply recipient is the
    original sender (`from_addr`).
    """
    if payload.direction != "inbound":
        return None
    if not settings.mail_reply_draft_enabled or settings.node_kind != "company":
        return None
    sender = (payload.from_addr or "").strip()
    if not sender:
        return None

    reply_from, backend = _resolve_reply_from(payload.to_addr)
    if reply_from is None or backend is None:
        return None

    subject = _reply_subject(payload.subject)
    reply_context = MailReplyContext(
        backend=backend,
        from_addr=reply_from,
        to_addr=sender,
        in_reply_to_message_id=payload.message_id,
        in_reply_to_external_id=payload.external_id,
        subject=subject,
        # P6 mail ingest path skips redaction (§5-B); this excerpt feeds the agent
        # work payload (not a wiki page), so we only truncate — no redaction here.
        thread_excerpt=(payload.body_text or "").strip()[:_THREAD_EXCERPT_MAX],
    )
    source_ref_id = mail_source_canonical_id(payload)
    inbound_subject = (payload.subject or "").strip() or "(제목 없음)"
    candidate_payload: dict = {
        "question": f"받은 메일에 대한 답장 초안: {inbound_subject}",
        "node_id": settings.node_id,
        "node_kind": settings.node_kind,
        # Recipient hint lets the deterministic gate see a recipient and yield
        # draft_for_review (state.py email_send family).
        "recipient_hint": sender,
        "reply_context": reply_context.model_dump(mode="json"),
    }
    evidence = [
        {
            "kind": "mail_reply",
            "ref": source_ref_id,
            "backend": backend,
        }
    ]
    return AgentWorkCandidate(
        source_kind="mail_reply",
        source_ref_id=source_ref_id,
        action_family="email_send",
        title=f"답장 초안: {inbound_subject}"[:96],
        payload=candidate_payload,
        evidence=evidence,
    )


def _resolve_reply_from(to_addr: list[str]) -> tuple[str | None, str | None]:
    """Pick the first recipient address whose domain routes to a send backend."""
    for addr in to_addr:
        candidate = (addr or "").strip()
        if not candidate:
            continue
        try:
            backend = backend_for_from_addr(candidate)
        except MailSendRoutingError:
            continue
        return candidate, backend
    return None, None


def _reply_subject(subject: str) -> str:
    compact = " ".join((subject or "").split())
    if not compact:
        return "Re: (제목 없음)"
    if compact.lower().startswith("re:"):
        return compact[:120]
    return f"Re: {compact}"[:120]


async def send_approved_reply(
    item: AgentWorkItem,
    *,
    reviewer_id: UUID,
    reviewer_role: str | None,
    settings: Settings,
) -> dict:
    """Perform the REAL send for an approved reply draft.

    Mirrors the P6.3 `post_mail_send` guard sequence. Never raises on a disabled
    state (approve has already committed); returns a structured outcome dict with
    hash-only provider id. Idempotent via `email_send_log` work_id lookup.
    """
    if not settings.mail_send_enabled or settings.node_kind != "company":
        return {"sent": False, "reason": "send_disabled"}
    if reviewer_role not in _REPLY_OPERATOR_ROLES:
        return {"sent": False, "reason": "role_forbidden"}

    draft = (item.payload or {}).get("email_draft")
    if not isinstance(draft, dict):
        return {"sent": False, "reason": "no_reply_draft"}
    raw_context = draft.get("reply_context")
    if not isinstance(raw_context, dict):
        return {"sent": False, "reason": "no_reply_context"}
    try:
        reply_context = MailReplyContext.model_validate(raw_context)
    except ValueError as exc:
        return {"sent": False, "reason": f"invalid_reply_context: {type(exc).__name__}"}

    subject = str(draft.get("subject_hint") or reply_context.subject)
    body = str(draft.get("body_template") or "")
    try:
        request = MailSendRequest(
            from_addr=reply_context.from_addr,
            to=reply_context.to_addr,
            subject=subject,
            text=body,
        )
    except ValueError as exc:
        return {"sent": False, "reason": f"invalid_send_request: {type(exc).__name__}"}

    if email_send_log_exists(item.work_id):
        return {"sent": False, "reason": "already_sent"}

    recipient_hash = email_hash(reply_context.to_addr)
    subject_hash = email_hash(subject)
    body_hash = email_hash(body)
    now = datetime.now(UTC)
    if email_send_rate_limited(
        node_id=settings.node_id,
        owner_id=reviewer_id,
        recipient_hash=recipient_hash,
        now=now,
    ):
        return {"sent": False, "reason": "rate_limited"}

    with audit("mail.send") as span:
        span.add_meta(
            backend=reply_context.backend,
            origin="agent_reply",
            node_id=settings.node_id,
            owner_id=str(reviewer_id),
            work_id=str(item.work_id),
            recipient_hash=recipient_hash,
            subject_hash=subject_hash,
        )
        try:
            result = await send_mail(request, settings)
        except (MailSendRoutingError, MailSendUnconfiguredError) as exc:
            span.set_output({"backend": reply_context.backend, "status": "failed"})
            return {"sent": False, "reason": f"send_error: {type(exc).__name__}"}
        provider_id_hash = (
            email_hash(result.provider_message_id) if result.provider_message_id else None
        )
        insert_email_send_log(
            node_id=settings.node_id,
            owner_id=reviewer_id,
            recipient_hash=recipient_hash,
            subject_hash=subject_hash,
            body_hash=body_hash,
            sender_kind=result.backend,
            status=result.status,
            sent_at=now,
            origin="agent_reply",
            work_id=item.work_id,
            correlation_id=span.correlation_id,
            error_message=result.error,
        )
        span.set_output({"backend": result.backend, "status": result.status})

    return {
        "sent": result.status == "sent",
        "status": result.status,
        "backend": result.backend,
        "provider_message_id_hash": provider_id_hash,
        "error": result.error,
    }
