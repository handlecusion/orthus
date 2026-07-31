"""T2 learn contamination guard (`wiki/qa.py::_should_learn`).

프로덕션 증거(2026-07-16): 학습된 qa 페이지에 인사말과 죽은 번호 인용("(문서 [1] 및
[2] 참조)")을 품은 과거 답변이 그대로 들어가, 다음 검색의 grounding으로 재소환되는
자기오염 루프가 있었다. 게이트는 결정론(코드 결정, LLM 0회)이며 아래 세 조건에서
학습을 막는다: gap 감지됨 / 질문 6자 미만 / 답변에 번호 인용 패턴.
"""

from __future__ import annotations

import uuid

import pytest

from orthus.models.adapters.mock import MockChat
from orthus.schemas.canonical import WikiSourceRef
from orthus.wiki import qa as qa_module
from orthus.wiki.qa import _should_learn, answer_from_hits


def _hits() -> list[WikiSourceRef]:
    return [
        WikiSourceRef(page_slug="p1", title="T1", kind="page", excerpt="alpha", score=0.9),
    ]


class _Gap:  # detect_gap 반환 객체의 자리표시자 (게이트는 None 여부만 본다)
    pass


def test_should_learn_normal_qa_passes():
    assert _should_learn("회사 휴가 규정이 어떻게 되나요?", "휴가는 연 15일이다.", None)


def test_should_learn_blocks_when_gap_detected():
    assert not _should_learn("회사 휴가 규정이 어떻게 되나요?", "휴가는 연 15일이다.", _Gap())


def test_should_learn_blocks_short_greeting_questions():
    assert not _should_learn("안녕?", "안녕하세요!", None)
    assert not _should_learn("  ㅎㅇ ", "안녕하세요!", None)


def test_should_learn_blocks_numbered_citation_answers():
    # 실제 오염 사례 형태 그대로.
    assert not _should_learn(
        "우리 ai 툴에 관련된 문서 뭐가 있지?",
        "AI 영상관련툴에 대한 순위를 정리한 문서가 있습니다. (문서 [1] 및 [2] 참조)",
        None,
    )
    assert not _should_learn("질문입니다 무엇인가요?", "근거는 [3]에 있습니다.", None)
    assert not _should_learn("질문입니다 무엇인가요?", "AI 툴 내용입니다 [1], [2] 참조", None)


@pytest.mark.parametrize(
    "answer",
    [
        # 살아 있는 company wiki의 실제 청크들 — 아래첨자 표기지 인용이 아니다.
        "SQL is loaded from a file via parents[2] path-walking; every sibling KG module …",
        "No off-by-one — framings[1] is consistently B in the short-circuit",
        "For relation intent, _bind blindly uses slugs[0]/slugs[1] for path_between",
    ],
)
def test_should_learn_does_not_block_subscript_prose(answer):
    """`[N]` 가드가 정상 기술 지식(배열 인덱싱)을 오탐하면 안 된다.

    company wiki에 이 형태의 정상 청크가 35개 있다 — 인용은 항상 공백/`(` 뒤에 오지만
    아래첨자는 식별자 바로 뒤에 붙는다.
    """
    assert _should_learn("이 모듈 구현이 어떻게 되나요?", answer, None)


def test_answer_from_hits_gated_answer_does_not_author(monkeypatch):
    """번호 인용 답변은 answer_from_hits 경로에서 author_from_qa 호출 자체가 없어야 한다."""
    calls: list[tuple] = []
    monkeypatch.setattr(qa_module, "author_from_qa", lambda *a, **k: calls.append((a, k)) or {})
    chat = MockChat(rules=[("", "그 내용은 문서에 있습니다. (문서 [1] 참조)")])
    answer_from_hits(
        uuid.uuid4(),
        "ai 툴 관련해서 내용 요약해줘",
        _hits(),
        chat_model=chat,
        learn=True,
        record_gaps=False,
    )
    assert calls == []


def test_answer_from_hits_clean_answer_still_authors(monkeypatch):
    """게이트는 정상 Q&A 학습(T2 본기능)을 막지 않는다."""
    calls: list[tuple] = []
    monkeypatch.setattr(qa_module, "author_from_qa", lambda *a, **k: calls.append((a, k)) or {})
    chat = MockChat(rules=[("", "휴가는 연 15일이고 이월은 5일까지 가능하다.")])
    answer_from_hits(
        uuid.uuid4(),
        "회사 휴가 규정이 어떻게 되나요?",
        _hits(),
        chat_model=chat,
        learn=True,
        record_gaps=False,
    )
    assert len(calls) == 1


def test_answer_from_hits_learn_false_still_never_authors(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(qa_module, "author_from_qa", lambda *a, **k: calls.append((a, k)) or {})
    chat = MockChat(rules=[("", "휴가는 연 15일이다.")])
    answer_from_hits(
        uuid.uuid4(),
        "회사 휴가 규정이 어떻게 되나요?",
        _hits(),
        chat_model=chat,
        learn=False,
        record_gaps=False,
    )
    assert calls == []
