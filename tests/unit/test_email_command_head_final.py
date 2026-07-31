"""메일 명령 head-final 판정 (`agentwork/service.py::_email_action_is_head_final`).

프로덕션 재현(2026-07-16): "해당 내용 요약된 거 메일 보내줘"가 `_INFO_QUERY_TERMS`의
'요약' 가드에 걸려 read로 강등돼, 메일 명령이 큐에 들어가지도 못하고 엉뚱한 문서 요약이
돌아왔다. 한국어는 head-final이라 **마지막 서술어**가 주동사다 — 그 순서로 명령/질문을
가른다. LLM 0회 결정론(설계 원칙 1).
"""

from __future__ import annotations

import pytest

from orthus.agentwork import has_strong_command
from orthus.agentwork.service import detect_assistant_command_action


class TestEmailHeadFinalCommand:
    @pytest.mark.parametrize(
        "question",
        [
            # 발송 동사가 문말 주동사 → 명령. recipient가 없어도 게이트로 보내
            # request_more_data("누구에게?")를 받게 한다(P3.4c).
            "해당 내용 요약된 거 메일 보내줘",
            "메일 보내줘",
            "요약해서 메일 초안 작성해줘",
            "정리한 내용 메일로 전송해줘",
        ],
    )
    def test_send_verb_head_final_is_email_command(self, question):
        assert detect_assistant_command_action(question) == "email_send"

    @pytest.mark.parametrize(
        "question",
        [
            "해당 내용 요약된 거 메일 보내줘",
            "메일 보내줘",
            "요약해서 메일 초안 작성해줘",
        ],
    )
    def test_head_final_does_not_make_it_strong(self, question):
        """head-final은 **감지**만 연다 — strong 계약(recipient literal 필수)은 불변.

        `has_strong_command`는 command-preservation 가드(BLOCKER F)를 구동해 decompose를
        우회시킨다. recipient 없는 메일을 strong으로 올리면 그 가드 계약이 바뀐다
        (test_command_keyword_coverage.py::test_strong_command_semantics_unchanged).
        게이트에 도달해 request_more_data로 수신자를 묻는 것으로 충분하다.
        """
        assert has_strong_command(question) is False

    @pytest.mark.parametrize(
        "question",
        [
            # 읽기 동사가 문말 주동사 → 질문. 메일 단어가 있어도 명령이 아니다.
            "메일 답장 요약해줘",  # 기존 계약 (test_agent_work.py:252)
            "메일 회신 방법 알려줘",
            "메일 초안 작성 방법이 뭐야",
        ],
    )
    def test_read_verb_head_final_stays_query(self, question):
        assert detect_assistant_command_action(question) is None

    @pytest.mark.parametrize(
        "question",
        [
            "ai 툴 관련해서 내용 요약해줘",
            "내용 요약해서 나에게 알려줘",
            "회사 위키에 정리된 내용 요약해줘",
            "회사 휴가 규정 요약해줘",
        ],
    )
    def test_knowledge_questions_unaffected(self, question):
        """메일 신호가 없는 순수 지식 질문은 이 규칙에 닿지 않는다."""
        assert detect_assistant_command_action(question) is None

    def test_explicit_recipient_still_strong_regardless_of_order(self):
        """recipient literal이 있으면 순서와 무관하게 strong (기존 경로 무회귀)."""
        q = "NOVA랑 관련된 문서 요약해서 ceo@nova.example로 메일 초안 작성해"
        assert detect_assistant_command_action(q) == "email_send"
        assert has_strong_command(q) is True

    def test_connector_sync_unaffected(self):
        assert detect_assistant_command_action("gmail 동기화해줘") == "connector_sync"
        assert has_strong_command("gmail 동기화해줘") is True
