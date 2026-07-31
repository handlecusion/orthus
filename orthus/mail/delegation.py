"""Delegation slice 4: inbound mail -> agent_task delegation candidate.

When a new inbound company-domain mail is ingested, this module asks the LLM to
extract a delegation intent (assignee/mode/runner/instruction) from the mail body.
The LLM only EXTRACTS — it never decides execution. The deterministic policy gate
in `orthus.agentwork.state` decides auto_execute vs request_more_data, and the
dispatch itself still requires `agent_task_enabled` (defence in depth).

Import boundary: this is a mail-side module that calls into `orthus.agentwork`
(same direction as `orthus.mail.reply`). `orthus.agentwork` must not import
`orthus.mail`. `ingest.py` imports `build_delegation_candidate` lazily.

Returns None — never guesses — when the mail is not a delegation request, when
the flag is off / not a company node, or when extraction fails or is uncertain.
"""

from __future__ import annotations

from uuid import UUID

from orthus.agentwork.delegation import extract_delegation_intent
from orthus.audit import audit
from orthus.mail.delegation_prefilter import non_delegation_reason
from orthus.mail.ingest import mail_source_canonical_id
from orthus.schemas.canonical import AgentWorkItem, MailIngestRequest
from orthus.settings import Settings

_THREAD_EXCERPT_MAX = 4000


def build_delegation_candidate(
    payload: MailIngestRequest,
    owner_user_id: UUID,
    settings: Settings,
) -> AgentWorkItem | None:
    """Extract a delegation intent from an inbound mail and queue an agent_task.

    Returns the created Agent Work item (classified by the deterministic policy
    gate), or None when the mail is not a delegation request, the flag is off /
    not a company node, the actor lacks operator authority, or extraction fails.
    """
    if not settings.mail_agent_task_delegation_enabled or settings.node_kind != "company":
        return None
    if payload.direction != "inbound":
        return None

    extracted = _extract_delegation(payload, settings)
    if extracted is None:
        return None
    assignee, mode, runner, instruction = extracted

    # Import locally to keep agentwork free of a mail import at module load.
    from orthus.agentwork.service import create_agent_task_work_item

    mail_origin = {
        "source_canonical_id": mail_source_canonical_id(payload),
        "backend": payload.backend,
        "message_id": payload.message_id,
        "external_id": payload.external_id,
        "sender": (payload.from_addr or "").strip(),
        "subject": (payload.subject or "").strip(),
        "reply_from": _first_routable_recipient(payload.to_addr),
    }
    return create_agent_task_work_item(
        owner_user_id,
        actor_role="scheduler",
        # The "this mail delegates work to X" judgment is the LLM's, over untrusted
        # third-party text. The gate must not auto-dispatch on it — see state.py.
        llm_inferred=True,
        assignee=assignee,
        mode=mode,
        runner=runner,
        instruction=instruction,
        mail_origin=mail_origin,
        settings=settings,
    )


def _extract_delegation(
    payload: MailIngestRequest,
    settings: Settings,
) -> tuple[str, str, str, str] | None:
    """LLM extraction only. Returns (assignee, mode, runner, instruction) or None.

    None means "not a delegation / uncertain / extraction failed" — never guess.
    Mail requires an explicit assignee: an empty assignee is treated as "not a
    delegation" (unlike chat, where the caller defaults to self).
    """
    excerpt = (payload.body_text or "").strip()[:_THREAD_EXCERPT_MAX]
    subject = (payload.subject or "").strip()

    # R2 결정론 프리필터 (LLM 0회). 기계가 보낸 메일 — 회의록, 부재중 자동응답, 티켓 배정 통보,
    # 캘린더 초대 — 은 **형식으로** 판별된다. LLM에게 물을 가치가 없고, 물으면 오탐이 난다:
    # 적대적 홀드아웃에서 EXAONE은 `[회의록] … 액션 아이템 1. 최수민 — 테스트 커버리지 보강`을
    # 위임으로 읽고 최수민을 assignee로 뽑았다. 이 필터가 그 메일을 LLM에 보내지 않는다.
    #
    # 못 잡은 함정(자기 배정, 제3자 인용 같은 **의미적** 함정)은 R1의 사람 검토가 받는다 —
    # 두 장치는 직렬이고, 이건 그중 값싼 쪽이다.
    skip_reason = non_delegation_reason(
        subject=subject, body_text=excerpt, from_addr=payload.from_addr
    )
    if skip_reason is not None:
        with audit("mail.delegation_prefilter") as span:
            span.add_meta(reason=skip_reason, backend=payload.backend)
            span.set_output({"skipped": True})
        return None

    text = (
        f"메일 제목: {subject}\n"
        f"보낸 사람: {(payload.from_addr or '').strip()}\n\n"
        f"메일 본문:\n{excerpt}\n\n"
        "위 메일이 특정 팀원에게 작업을 위임하는 지시인지 판단하고 스키마대로 JSON만 출력하라."
    )
    extracted = extract_delegation_intent(text, settings, backend=payload.backend)
    if extracted is None:
        return None
    assignee = extracted["assignee"]
    if not assignee:
        return None
    return assignee, extracted["mode"], extracted["runner"], extracted["instruction"]


def _first_routable_recipient(to_addr: list[str]) -> str | None:
    """The company address that received the mail (used as the reply From hint in
    slice 5). First non-empty recipient is good enough for the completion draft."""
    for addr in to_addr:
        candidate = (addr or "").strip()
        if candidate:
            return candidate
    return None
