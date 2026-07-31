"""Local-agent reply draft: inbound owner-scope mail -> self-assigned agent_task.

Unlike slice-4 delegation (`orthus.mail.delegation`), which extracts an explicit
delegation intent from the mail, this fires for every owner-scope (personal)
inbound mail when the flag is on. It self-assigns an `agent_task` to the mailbox
OWNER, so the reply draft is authored by that employee's own collector daemon
(claude/codex) instead of the central LLM (P7.1). The deterministic policy gate
still decides auto_execute vs request_more_data, and dispatch requires
`agent_task_enabled` (defence in depth).

Import boundary: a mail-side module that calls into `orthus.agentwork` (same
direction as `orthus.mail.reply` / `orthus.mail.delegation`). `orthus.agentwork`
must not import `orthus.mail`. `ingest.py` imports `build_reply_draft_agent_task`
lazily.

The agent run is `mode="knowledge"` (read-only; the local agent drafts text and
can read company knowledge over the auto-attached orthus MCP) — never `code`.
"""

from __future__ import annotations

from uuid import UUID

from orthus.mail.ingest import mail_source_canonical_id
from orthus.schemas.canonical import AgentWorkItem, MailIngestRequest
from orthus.settings import Settings

_THREAD_EXCERPT_MAX = 4000
_RUNNER = "claude"
_MODE = "knowledge"


def build_reply_draft_agent_task(
    payload: MailIngestRequest,
    owner_user_id: UUID,
    settings: Settings,
) -> AgentWorkItem | None:
    """Self-assign an agent_task to draft a reply for an inbound owner-scope mail.

    The mailbox owner is the assignee (assignee = their own user_id), so the
    dispatch routes to their own enrolled collector daemon. Returns the created
    Agent Work item (classified by the policy gate), or None when the flag is off,
    this is not a company node, the mail is not inbound, or it has no sender.
    """
    if not settings.mail_reply_draft_agent_enabled or settings.node_kind != "company":
        return None
    if payload.direction != "inbound":
        return None

    sender = (payload.from_addr or "").strip()
    if not sender:
        return None

    # Import locally to keep agentwork free of a mail import at module load.
    from orthus.agentwork.service import create_agent_task_work_item

    subject = (payload.subject or "").strip() or "(제목 없음)"
    excerpt = (payload.body_text or "").strip()[:_THREAD_EXCERPT_MAX]
    instruction = (
        "받은 회사 메일에 대한 한국어 답장 초안을 작성하라. "
        "회사 지식이 필요하면 orthus MCP(wiki_search/wiki_ask)로 조회하라. "
        "미팅/일정 조율 요청이면 team_schedule(since, until)로 팀 전원 공유 일정을 "
        "확인하고, 이미 잡힌 일정과 겹치지 않는 시간만 제안하라. "
        "최종 출력은 보낼 답장 본문 텍스트만 작성한다.\n\n"
        f"제목: {subject}\n"
        f"보낸 사람: {sender}\n\n"
        f"본문:\n{excerpt}"
    )
    mail_origin = {
        "source_canonical_id": mail_source_canonical_id(payload),
        "backend": payload.backend,
        "message_id": payload.message_id,
        "external_id": payload.external_id,
        "sender": sender,
        "subject": subject,
        "reply_from": _first_routable_recipient(payload.to_addr),
        # Real recipient address + subject for the OUTGOING reply (slice-5 send).
        # `sender`/`subject` above get PII-redacted in the stored payload; these
        # two are kept clean because send_approved_reply needs the actual To
        # address and a usable Re: subject — the same data the P7.1
        # ReplyDraftPayload already stores for a reply draft.
        "reply_to_addr": sender,
        "reply_subject": subject,
        # Marks this completion so slice-5 surfaces the agent's draft as the
        # reply body verbatim, not a generic "task completed" summary.
        "kind": "reply_draft",
    }
    return create_agent_task_work_item(
        owner_user_id,
        actor_role="scheduler",
        assignee=str(owner_user_id),
        mode=_MODE,
        runner=_RUNNER,
        instruction=instruction,
        mail_origin=mail_origin,
        settings=settings,
    )


def _first_routable_recipient(to_addr: list[str]) -> str | None:
    """The company address that received the mail (reply From hint for slice 5)."""
    for addr in to_addr:
        candidate = (addr or "").strip()
        if candidate:
            return candidate
    return None
