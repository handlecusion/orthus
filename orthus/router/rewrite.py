"""후속질문 지시어 해소 (follow-up reference rewrite).

Agent-chat 오케스트레이터의 retrieval/분류는 현재 메시지 텍스트만 본다 — 히스토리는
wiki 답변 프롬프트에 "참고용" 블록으로만 붙고 retrieval에는 절대 닿지 않는다
(`orthus/agentwork/chat.py::load_chat_history` docstring). 그래서 "그거 내용 알려줘"
같은 후속질문은 지시어가 해소되지 않은 채 문자 그대로 검색돼 매 턴 엉뚱한 문서를
집어온다 (2026-07-16 prod 재현).

이 모듈은 그 간극을 닫는다: **결정론 프리필터**(지시어 토큰 존재 + 히스토리 존재)를
통과한 경우에만 LLM 1콜로 질문을 self-contained 형태로 재작성해 돌려준다. LLM은
재작성(입력 생성)만 하고, 재작성 결과가 이상하면(빈 문자열/과대 길이/여러 줄) 원문으로
fail-open한다 — 실행 결정이 아니므로 설계 원칙 1과 충돌하지 않는다.

히스토리 자체는 여전히 grounding에 들어가지 않는다(AGENTS.md 원칙 7 유지): 재작성된
질문만 retrieval로 가고, 사실은 계속 retrieved passages에서만 나온다.

Flag: `ORTHUS_CHAT_FOLLOWUP_REWRITE_ENABLED` (default false, fail-closed) — off이면
호출부(orchestrate)가 이 모듈에 진입하지 않아 응답이 byte-identical이다.
"""

from __future__ import annotations

from orthus.audit import audit
from orthus.models.base import ChatModel
from orthus.models.orchestration import TASK_FOLLOWUP_REWRITE, get_chat_model_for
from orthus.schemas.canonical import ChatTurn

# 결정론 프리필터: 이 토큰이 하나도 없으면 LLM을 부르지 않는다 (공백 제거 비교).
# 후속질문에서 앞 턴을 가리키는 한국어 지시어/조응 표현만 — 일반 명사는 넣지 않는다.
_REFERENCE_TOKENS = (
    "그거",
    "그것",
    "그건",
    "그게",
    "그내용",
    "그문서",
    "그부분",
    "그중",
    "해당",
    "이거",
    "이것",
    "이내용",
    "저거",
    "아까",
    "방금",
    "위에서",
    "위의",
    "앞서",
    "앞의",
    "거기",
    "그때",
)

_MAX_REWRITE_CHARS = 300

_SYSTEM = (
    "You rewrite a follow-up chat question into ONE self-contained question, "
    "resolving pronouns/references (e.g. '그거', '해당 내용') from the prior "
    "conversation. Keep the user's language and intent EXACTLY — do not answer, "
    "do not add new asks, do not drop any part of the request. If the question "
    "is already self-contained, return it unchanged. Return ONLY the rewritten "
    "question text, one line, no quotes, no explanation."
    # strict-v1 (prompt-lab Track A, 2026-07-19): 잔존 지시어 금지 명시가 없던
    # 프롬프트에서 재작성의 17.5%(21/120)에 지시어가 남았다. 아래 문장 추가로
    # 3.3%(4/120)로 감소(paired McNemar 18:1, p=0.0001), 판정 품질 동등(75:77).
    # 근거: experiments/prompt-lab/analysis/qa-round-3.md
    " The rewritten question must contain NO deictic reference words "
    "('그거', '그것', '해당', '아까', '방금', '위에서', '그때', '거기') — "
    "replace every one of them with the concrete thing from the prior "
    "conversation. Re-check before answering: if any such word remains, "
    "rewrite again."
)


def needs_rewrite(question: str, history: list[ChatTurn] | None) -> bool:
    """결정론 프리필터 — 히스토리가 있고 지시어 토큰이 보일 때만 True (LLM 0회)."""
    if not history:
        return False
    compact = (question or "").replace(" ", "")
    if not compact:
        return False
    return any(token in compact for token in _REFERENCE_TOKENS)


def resolve_followup(
    question: str,
    history: list[ChatTurn],
    *,
    chat_model: ChatModel | None = None,
) -> str:
    """지시어가 해소된 self-contained 질문을 돌려준다. 실패 시 원문 그대로(fail-open).

    호출 전제: `needs_rewrite`가 True. LLM 출력 가드 — 빈 문자열, 300자 초과,
    다중 줄(첫 줄만 채택 후 재검증)은 전부 원문 폴백. 어떤 예외도 원문 폴백.
    """
    convo = "".join(f"{turn.role}: {turn.text}\n" for turn in history)
    user_prompt = (
        f"Prior conversation:\n{convo}\nFollow-up question: {question}\n\nRewritten question:"
    )
    try:
        chat = chat_model or get_chat_model_for(TASK_FOLLOWUP_REWRITE)
        with audit("router.followup_rewrite") as span:
            raw = chat.complete(_SYSTEM, user_prompt)
            rewritten = (raw or "").strip().splitlines()[0].strip() if raw else ""
            # 따옴표로 감싼 출력 정규화 (모델이 지시를 어겨 인용부호를 붙이는 경우)
            rewritten = rewritten.strip("\"'“”‘’").strip()
            ok = bool(rewritten) and len(rewritten) <= _MAX_REWRITE_CHARS
            span.add_meta(
                rewritten=ok and rewritten != question.strip(),
                model_id=getattr(chat, "model_id", "unknown"),
            )
            return rewritten if ok else question
    except Exception:  # noqa: BLE001 — 재작성은 보조 경로; 어떤 실패도 답변을 막지 않는다
        return question
