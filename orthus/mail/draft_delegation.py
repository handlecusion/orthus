"""On-demand local-agent mail draft: the "AI 초안" button -> self-assigned agent_task.

Today the "AI 초안" compose/reply button posts to the synchronous central
`POST /mail/draft`, which runs whatever `ORTHUS_LLM` the company node holds. This
module routes that draft to the REQUESTING user's OWN collector daemon instead, so
the draft is authored by that employee's local claude/hermes (their own auth, their
own machine) and no central provider key is needed.

It is the on-demand sibling of `orthus.mail.reply_delegation` (which fires per
inbound owner-scope mail). Both self-assign an `agent_task` to the user, so the
dispatch routes to their own enrolled daemon, and both carry a `mail_origin`
discriminator so the completion handler knows how to surface the result. The
difference: a reply_draft completion becomes an `email_send` draft_for_review item;
a `compose_draft` completion writes the parsed `{subject, body}` back onto the SAME
work item (`payload.mail_draft`) for the compose box — it never creates a second
send item (the user sends manually through the unchanged `/mail/send` gate).

The deterministic policy gate still decides auto_execute vs request_more_data, and
dispatch requires `agent_task_enabled` on both processes (defence in depth). The
agent run is `mode="knowledge"` (read-only; it drafts text and may read company
knowledge over the auto-attached orthus MCP) — never `code`.

Import boundary: a mail-side module that calls into `orthus.agentwork` (same
direction as `orthus.mail.reply` / `orthus.mail.reply_delegation`). `orthus.agentwork`
must not import `orthus.mail`; callers import `build_compose_draft_agent_task` lazily.
"""

from __future__ import annotations

from uuid import UUID

from orthus.schemas.canonical import AgentWorkItem, MailDraftDispatchRequest
from orthus.settings import Settings

# A literal, PII-free origin marker. `_sanitize_mail_origin` requires a non-empty
# `sender` to keep an origin, but a compose draft has no inbound sender — this marker
# satisfies that without storing any address, and `kind` routes the completion.
_COMPOSE_SENDER_MARKER = "compose_draft"
_DEFAULT_RUNNER = "claude"
_MODE = "knowledge"


def build_compose_draft_agent_task(
    payload: MailDraftDispatchRequest,
    owner_user_id: UUID,
    *,
    actor_role: str | None,
    settings: Settings,
) -> AgentWorkItem | None:
    """Self-assign an agent_task so the requester's own daemon drafts the mail.

    Returns the created Agent Work item (in `auto_execute` when dispatched), or None
    when the flag/node/role check fails OR the requester has no enrolled daemon — in
    which case the caller falls back to the central draft / unavailable tiers. The
    daemon is pre-resolved here so a missing daemon never leaves a stray
    request_more_data row in the queue.
    """
    if not settings.mail_agent_draft_enabled or settings.node_kind != "company":
        return None
    if not settings.agent_task_enabled:
        return None

    # Import locally to keep agentwork free of a mail import at module load.
    from orthus.agentwork.delegation import resolve_enrolled_daemon

    requested_device = (payload.device_id or "").strip()
    if resolve_enrolled_daemon(owner_user_id, device_id=requested_device, settings=settings) is None:
        # No enrolled daemon for this owner (/device): let the caller fall back.
        return None

    from orthus.agentwork.service import create_agent_task_work_item
    from orthus.mail.compose import build_mail_draft_instruction

    runner = payload.runner or _DEFAULT_RUNNER
    instruction = build_mail_draft_instruction(payload.to_draft_request())
    mail_origin = {
        "sender": _COMPOSE_SENDER_MARKER,
        "subject": (payload.subject or "").strip(),
        "kind": "compose_draft",
        "draft_mode": payload.mode,
    }
    return create_agent_task_work_item(
        owner_user_id,
        actor_role=actor_role,
        assignee=str(owner_user_id),
        mode=_MODE,
        runner=runner,
        instruction=instruction,
        device_id=requested_device or None,
        mail_origin=mail_origin,
        # §5-B mail carve-out: the draft is authored on the OWNER's own machine
        # (assignee == requester) from the owner's own thread context — no
        # cross-user exposure — so the folded mail context is passed unredacted,
        # matching the central sync `/mail/draft` path. Reply-to-teammate
        # delegation keeps the default redaction.
        redact_instruction=False,
        settings=settings,
    )
