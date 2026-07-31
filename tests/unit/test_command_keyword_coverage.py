"""§S3 keyword-miss 갭 축소 회귀 (docs/command-split-hold-followup.md §S3).

불변식14(명령 family=결정론 keyword, LLM-enum 부활 금지)를 유지한 채
`_detect_command_family` 표면형 커버리지를 넓힌다. 축은 둘:

1. **양성**: 실측 miss였던 자연 표면형이 이제 올바른 family로 잡힌다.
2. **음성**: 질문형/명령형-지식-질문은 여전히 None(검색 폴스루)이다 — 커버리지
   확장이 질문→큐잉 오검출을 만들지 않는다. 광의어("연동"/"업데이트"/"공유"/
   "메모"/"작업")는 명사 collocation FP 때문에 의도적으로 미추가이며, 그 결정도
   여기 음성 케이스로 고정한다.
"""

from __future__ import annotations

import pytest

from orthus.agentwork import detect_assistant_command_action


@pytest.mark.parametrize(
    ("question", "family"),
    [
        # 기존 커버 (회귀 가드)
        ("gmail 동기화해줘", "connector_sync"),
        ("메일 초안 만들어줘", "email_send"),
        ("문서 초안 작성해줘", "document_draft"),
        ("보드 정리해줘", "personal_board_cleanup"),
        # connector_sync 동의어: 싱크/최신화
        ("slack 싱크해줘", "connector_sync"),
        ("슬랙 최신화해줘", "connector_sync"),
        # email_send: 작성/써/회신 + 써줘/회신/부탁 동사
        ("메일 작성해줘", "email_send"),
        ("김부장에게 메일 써줘", "email_send"),
        ("메일 회신해줘", "email_send"),
        # document_draft: 보고서/리포트/회의록 + 부탁 동사
        ("보고서 써줘", "document_draft"),
        ("리포트 만들어줘", "document_draft"),
        ("회의록 초안 부탁해", "document_draft"),
        # personal_board_cleanup: 태스크 + 정돈/청소
        ("할일 정돈해줘", "personal_board_cleanup"),
        ("칸반 청소해줘", "personal_board_cleanup"),
        ("완료된 태스크 정리해줘", "personal_board_cleanup"),
    ],
)
def test_expanded_surface_forms_detect_command_family(question, family):
    assert detect_assistant_command_action(question) == family


@pytest.mark.parametrize(
    "question",
    [
        # 질문형은 큐잉하지 않는다 (verb 게이트 또는 INFO 가드)
        "메일 회신 방법 알려줘",
        "슬랙 연동 방법 알려줘",
        "보고서 작성 가이드 알려줘",
        "휴가정책 알려줘",
        "리포트가 뭐야",
        "지난 보고서 요약해줘",
        # 기존 회귀: read-verb 지식 질문 (P3.2)
        "회사 위키에 정리된 내용 요약해줘",
        # 의도적 미추가 광의어 — 명사 collocation FP 방지 결정 고정
        "노션 업데이트 내용 정리해줘",
        "슬랙 연동 문제 정리해줘",
        "노션 연동해줘",
        "gmail 업데이트해줘",
        "메일로 공유해줘",
        "메모 작성해줘",
        # 기존 회귀: 보드 '만들기'는 cleanup 명령이 아님
        "보드 만들어줘",
    ],
)
def test_question_shaped_and_excluded_terms_stay_search(question):
    assert detect_assistant_command_action(question) is None


def test_strong_command_semantics_unchanged():
    """_is_strong_command 계약 불변: connector_sync는 항상 strong, email은
    recipient literal이 있을 때만 strong (확장 표면형에도 동일 적용)."""
    from orthus.agentwork.service import has_strong_command

    assert has_strong_command("slack 싱크해줘") is True
    assert has_strong_command("김부장에게 메일 써줘") is True
    assert has_strong_command("메일 회신해줘") is False  # recipient 없음
    assert has_strong_command("보고서 써줘") is False  # document는 strong 아님
