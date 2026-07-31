"""AI compose/reply draft generation for the unified mail client.

The LLM writes draft text ONLY; it never sends. The human still clicks send
through the unchanged P6.3 `/mail/send` manual gate, so this stays inside the
"LLM drafts, deterministic/human gate executes" rule (AGENTS.md 설계 원칙 1).
No corpus/wiki authoring, no provider call — this just turns an instruction
(+ optional thread context) into a `{subject, body}` draft.
"""

from __future__ import annotations

import json
from uuid import UUID

from orthus.audit import audit
from orthus.mail.grounding import gather_wiki_grounding, grounding_prompt_block
from orthus.models.orchestration import TASK_EMAIL_DRAFT, get_chat_model_for
from orthus.schemas.canonical import MailDraftRequest, MailDraftResult

_INSTRUCTION_MAX = 2000
_CONTEXT_MAX = 6000

_SYSTEM = (
    "당신은 한국어 비즈니스 이메일 작성 비서입니다. 사용자의 지시와 (있다면) 이전 "
    "메일 맥락을 바탕으로 보낼 이메일 초안을 작성합니다. 규칙:\n"
    "- 초안만 작성하고 절대 발송하지 않습니다.\n"
    "- 정중하고 자연스러운 한국어 비즈니스 어투를 사용합니다.\n"
    "- 본문은 바로 보낼 수 있게 완결된 형태로 쓰되, 임의의 사실/수치/약속을 지어내지 "
    "않습니다. 모르는 정보는 '[확인 후 기입]' 같은 자리표시자로 둡니다.\n"
    "- 답장(reply)이면 맥락의 핵심에 정확히 답하고, 제목은 원 제목을 유지(Re: 접두)합니다.\n"
    '- 반드시 JSON 객체 하나만 출력합니다: {"subject": "...", "body": "..."}.'
)


def _build_user_prompt(req: MailDraftRequest) -> str:
    instruction = (req.instruction or "").strip()[:_INSTRUCTION_MAX]
    context = (req.context or "").strip()[:_CONTEXT_MAX]
    lines: list[str] = []
    if req.mode == "reply":
        lines.append("작업: 아래 메일에 대한 답장 초안을 작성하세요.")
    else:
        lines.append("작업: 새 이메일 초안을 작성하세요.")
    if req.to.strip():
        lines.append(f"받는 사람: {req.to.strip()}")
    if req.subject.strip():
        lines.append(f"현재 제목: {req.subject.strip()}")
    lines.append("")
    lines.append("사용자 지시:")
    lines.append(instruction or "(별도 지시 없음 — 맥락에 맞는 적절한 답장을 작성)")
    if context:
        lines.append("")
        lines.append("이전 메일 맥락:")
        lines.append(context)
    # 실측: 발신자 정보가 프롬프트에 없으니 모델이 맥락에 있는 이름을 서명으로 가져다
    # 썼다 — 상대 회사명("버텍스랩 드림")으로 서명한 초안이 나왔다. 같은 취지를
    # `_SYSTEM`에 넣었을 때는 무시됐고(gpt-4o-mini), 프롬프트 끝으로 옮기니 지켜졌다.
    lines.append("")
    lines.append(
        "서명 규칙: 발신자가 누구인지 모르므로 서명을 지어내지 마라. 받는 사람이나 "
        "원문 발신자의 이름·회사명을 발신자 서명으로 쓰지 말고, 맺음말은 '감사합니다.'로 "
        "끝내라."
    )
    return "\n".join(lines)


def coerce_mail_draft(raw: str, *, mode: str = "compose", subject: str = "") -> MailDraftResult:
    """Parse a model/agent answer into a `{subject, body}` draft.

    Tolerant: accepts a bare JSON object, a ```json fenced object, or — when the
    runner returns plain text (hermes, or a chatty claude) — falls back to using
    the whole text as the body. Request-free so the local-agent completion handler
    (which only has the work-item payload, not a MailDraftRequest) can reuse it."""
    text = (raw or "").strip()
    # Tolerate ```json fenced output.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    fallback_subject = (subject or "").strip() or ("Re: " if mode == "reply" else "")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            parsed_subject = str(data.get("subject") or fallback_subject).strip()
            body = str(data.get("body") or "").strip()
            if body:
                return MailDraftResult(subject=parsed_subject, body=body)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    # Non-JSON model output: treat the whole thing as the body.
    return MailDraftResult(subject=fallback_subject, body=text)


def _coerce_draft(raw: str, req: MailDraftRequest) -> MailDraftResult:
    return coerce_mail_draft(raw, mode=req.mode, subject=req.subject)


def build_mail_draft_instruction(req: MailDraftRequest) -> str:
    """Fold the system rules + the user prompt into ONE instruction string.

    The central `draft_email` passes `_SYSTEM` and the user prompt as separate
    arguments to the assigned model's `.complete()`, but a delegated local agent
    receives a single argv prompt. This builds that combined prompt so the local
    claude/hermes produces the same JSON-only `{subject, body}` draft. Built
    centrally from typed fields (injection defence at the company node per the
    agent_task carve-out)."""
    return f"{_SYSTEM}\n\n{_build_user_prompt(req)}"


def draft_email(req: MailDraftRequest, *, user_id: UUID | None = None) -> MailDraftResult:
    """Generate a `{subject, body}` draft. Never sends.

    `user_id`가 주어지고 위키 그라운딩 flag가 켜져 있으면, 받은 메일 내용으로 회사
    위키를 먼저 조회해 확인된 사실을 프롬프트에 얹는다 — 사내에 답이 있는 문의에
    `[확인 후 기입]`으로 비운 초안이 나가지 않게 한다. 조회 실패는 fail-open이라
    기존 ungrounded 경로로 그대로 떨어진다(`orthus/mail/grounding.py`).
    """
    grounding = gather_wiki_grounding(user_id, subject=req.subject, body=req.context)
    with audit("mail.draft") as span:
        span.add_meta(
            mode=req.mode,
            has_context=bool(req.context.strip()),
            wiki_grounded=grounding is not None,
        )
        model = get_chat_model_for(TASK_EMAIL_DRAFT)
        prompt = _build_user_prompt(req) + grounding_prompt_block(grounding)
        raw = model.complete(_SYSTEM, prompt, json_only=True)
        result = _coerce_draft(raw, req)
        if grounding is not None:
            result = result.model_copy(update={"wiki_grounding": grounding})
        span.set_output({"subject_len": len(result.subject), "body_len": len(result.body)})
    return result
