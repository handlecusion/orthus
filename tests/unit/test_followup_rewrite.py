"""후속질문 지시어 해소 (`router/rewrite.py`).

결정론 프리필터(지시어 토큰 + 히스토리 존재)와 LLM 출력 가드(빈/과대/다중 줄/예외
→ 원문 fail-open)를 고정한다. flag off 경로의 byte-identity는 orchestrate 쪽에서
`needs_rewrite` 미호출로 보장되므로 여기서는 모듈 계약만 검증한다.
"""

from __future__ import annotations

from orthus.router.rewrite import _MAX_REWRITE_CHARS, needs_rewrite, resolve_followup
from orthus.schemas.canonical import ChatTurn

_HISTORY = [
    ChatTurn(role="user", text="ai 툴 관련해서 내용 요약해줘"),
    ChatTurn(role="agent", text="AI 영상관련툴에 대한 순위를 정리한 문서가 있습니다."),
]


class _FakeChat:
    model_id = "fake"

    def __init__(self, response):
        self._response = response

    def complete(self, system: str, user: str) -> str:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# --- needs_rewrite: 결정론 프리필터 (LLM 0회) ---


def test_needs_rewrite_false_without_history():
    assert not needs_rewrite("그거 내용 알려줘", None)
    assert not needs_rewrite("그거 내용 알려줘", [])


def test_needs_rewrite_false_without_reference_token():
    assert not needs_rewrite("회사 휴가 규정 알려줘", _HISTORY)


def test_needs_rewrite_true_on_anaphora_with_history():
    assert needs_rewrite("그거 내용 알려줘", _HISTORY)
    assert needs_rewrite("해당 내용 요약된 거 메일 보내줘", _HISTORY)
    assert needs_rewrite("아까 그 문서 다시 보여줘", _HISTORY)


def test_needs_rewrite_compact_matching_ignores_spaces():
    # "그 내용" — 토큰은 공백 제거 비교라 띄어쓰기 변형도 잡는다.
    assert needs_rewrite("그 내용 좀 알려줘", _HISTORY)


# --- resolve_followup: LLM 재작성 + fail-open 가드 ---


def test_resolve_followup_returns_rewritten():
    chat = _FakeChat("AI 영상관련툴 순위 문서 내용 알려줘")
    out = resolve_followup("그거 내용 알려줘", _HISTORY, chat_model=chat)
    assert out == "AI 영상관련툴 순위 문서 내용 알려줘"


def test_resolve_followup_takes_first_line_and_strips_quotes():
    chat = _FakeChat('"AI 영상관련툴 순위 문서 내용 알려줘"\n(참고: 재작성했습니다)')
    out = resolve_followup("그거 내용 알려줘", _HISTORY, chat_model=chat)
    assert out == "AI 영상관련툴 순위 문서 내용 알려줘"


def test_resolve_followup_empty_output_falls_back():
    assert resolve_followup("그거 내용 알려줘", _HISTORY, chat_model=_FakeChat("")) == (
        "그거 내용 알려줘"
    )
    assert resolve_followup("그거 내용 알려줘", _HISTORY, chat_model=_FakeChat("   \n")) == (
        "그거 내용 알려줘"
    )


def test_resolve_followup_oversized_output_falls_back():
    chat = _FakeChat("가" * (_MAX_REWRITE_CHARS + 1))
    assert resolve_followup("그거 내용 알려줘", _HISTORY, chat_model=chat) == "그거 내용 알려줘"


def test_resolve_followup_exception_falls_back():
    chat = _FakeChat(RuntimeError("provider down"))
    assert resolve_followup("그거 내용 알려줘", _HISTORY, chat_model=chat) == "그거 내용 알려줘"
