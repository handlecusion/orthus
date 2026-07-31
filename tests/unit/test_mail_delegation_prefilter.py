"""메일 위임 결정론 프리필터 (R2). LLM 0회.

기계가 보낸 메일 — 회의록, 부재중 자동응답, 티켓 배정 통보, 캘린더 초대 — 은 형식으로 판별된다.
LLM에게 물을 가치가 없고, 물으면 오탐이 난다: 적대적 홀드아웃에서 EXAONE은
`[회의록] … 액션 아이템 1. 최수민 — 테스트 커버리지 보강`을 위임으로 읽고 **최수민을 assignee로
뽑았다**. 플래그가 켜져 있었다면 최수민의 Mac에서 샌드박스 없는 에이전트가 돌았을 것이다.

**이 파일의 1차 기준은 FN=0이다** — 진짜 위임을 하나라도 죽이면 필터가 기능을 망가뜨린 것이다.
`test_no_real_delegation_is_ever_filtered`가 두 골든의 진짜 위임 **18건 전수**를 고정한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orthus.mail.delegation_prefilter import non_delegation_reason

GOLDEN = Path(__file__).resolve().parents[2] / "experiments" / "fugu-ko" / "golden"


def _reason(text: str, *, subject: str | None = None, sender: str | None = None) -> str | None:
    return non_delegation_reason(subject=subject, body_text=text, from_addr=sender)


# ── ★ 1차 기준: 진짜 위임을 절대 죽이지 않는다 ────────────────────────────────


def _true_delegations() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name in ("t10_delegation.json", "t10_holdout2.json"):
        path = GOLDEN / name
        if not path.exists():  # 실험 골든이 없는 체크아웃에서는 스킵
            continue
        for item in json.loads(path.read_text(encoding="utf-8"))["items"]:
            if item["is_delegation"]:
                out.append((item["id"], item["text"]))
    return out


def test_no_real_delegation_is_ever_filtered() -> None:
    """★ FN=0. 두 골든의 진짜 위임 18건 중 하나라도 잘리면 실패다.

    이게 이 필터의 가장 위험한 실패 모드다. 단순 키워드 매칭이었다면
    "김철수님, **회의록** 정리해서 공유해주세요"가 잘렸을 것이다 — 그래서 내용어가 아니라
    **구조적 신호**(첫머리 대괄호 태그, 자동응답 정형구, 기계 발신자, 액션아이템 목록 형식)만 본다.
    """
    delegations = _true_delegations()
    if not delegations:
        pytest.skip("실험 골든 없음")

    killed = [(i, _reason(t)) for i, t in delegations if _reason(t) is not None]
    assert killed == [], f"프리필터가 진짜 위임을 죽였다: {killed}"


def test_a_delegation_that_mentions_a_meeting_log_survives() -> None:
    """내용어 매칭이었다면 죽었을 문장. 사람이 회의록을 **언급하며** 일을 시키는 건 위임이다."""
    assert _reason("김철수님, 회의록 정리해서 위키에 올려주세요.") is None
    assert _reason("액션 아이템 목록 좀 만들어 주세요, 박기획님.") is None
    assert _reason("정민수님, 부재중 자동응답 템플릿 문구 좀 다듬어 주실래요?") is None


# ── 구조적 함정: LLM에 보내지 않는다 ─────────────────────────────────────────


def test_meeting_minutes_with_action_items_is_filtered() -> None:
    """★ EXAONE이 실제로 오탐한 메일(h-09). 이제 LLM에 도달조차 하지 않는다."""
    text = (
        "[회의록] 2026-07-10 주간 스프린트\n\n"
        "액션 아이템\n"
        "1. 최수민 — 테스트 커버리지 70%까지 보강\n"
        "2. 오세훈 — 마이그레이션 스크립트 초안\n"
    )
    assert _reason(text) == "machine_tag"


def test_action_items_without_a_tag_still_filtered_by_shape() -> None:
    """대괄호 태그가 없어도 회의록의 **형식**(액션아이템 헤더 + 번호 매긴 배정)으로 잡는다."""
    text = "주간 회의 정리\n\n액션 아이템:\n1. 최수민 — 커버리지 보강\n2. 오세훈 — 마이그레이션\n"
    assert _reason(text) == "meeting_action_items"


def test_out_of_office_is_filtered() -> None:
    assert _reason("부재중 자동응답: 휴가입니다. 급한 건은 박기획님께 연락 주세요.") == "auto_reply"
    assert _reason("Out of Office: back on Monday. Contact 박기획 for urgent items.") == "auto_reply"


def test_ticket_notification_is_filtered() -> None:
    assert _reason("[Jira] NOVA-482가 정민수님에게 할당되었습니다.") == "machine_tag"
    # 태그가 없어도 배정 **통보** 문구로 잡는다 — 이미 일어난 일의 기록이지 지시가 아니다.
    assert _reason("NOVA-482가 정민수님에게 할당되었습니다.") == "assignment_notice"


def test_calendar_invite_is_filtered() -> None:
    assert _reason("[캘린더] 7/17 15:00 배포 리뷰 — 참석자: 김철수, 정민수") == "machine_tag"


def test_machine_sender_is_filtered() -> None:
    """발신자만으로도 충분하다. 사람이 no-reply 주소에서 위임하지 않는다."""
    assert _reason("김철수님, 배포 스크립트 고쳐주세요.", sender="no-reply@jira.example.com") == (
        "machine_sender"
    )
    assert _reason("아무 내용", sender="notifications@github.com") == "machine_sender"


def test_subject_line_tag_is_filtered() -> None:
    """제목의 태그도 본다 — 본문이 평범해도 기계 메일이다."""
    assert _reason("정민수님이 처리 예정", subject="[Jira] NOVA-482 상태 변경") == "machine_tag"


# ── 프리필터가 **못 잡는** 것 (설계상 R1이 받는다) ────────────────────────────


def test_semantic_traps_are_not_filtered_here() -> None:
    """의미적 함정은 구조가 없다 — 여기서 잡으려 하면 진짜 위임을 죽인다.

    이건 필터의 **결함이 아니라 경계**다. R1(사람 검토)이 직렬로 받는다. 이 테스트는 그 경계를
    명시해 둔다 — 나중에 누가 "왜 h-15를 못 잡지?"라며 필터를 넓히다 FN을 만들지 않도록.
    """
    # h-15: 자기 배정 — "제가 맡고, 오세훈님은 리뷰만"
    assert _reason("이번 마이그레이션은 제가 직접 맡겠습니다. 오세훈님은 리뷰만 부탁드릴게요.") is None
    # h-30: 제3자 인용 — 남이 한 말의 전달
    assert _reason("박기획님께서 '마이그레이션은 오세훈님이 맡기로 했다'고 하셨습니다.") is None


def test_empty_input_is_not_filtered() -> None:
    """빈 입력은 필터의 관심사가 아니다 — 아래 단계에서 자연히 None이 된다."""
    assert _reason("") is None
    assert non_delegation_reason(subject=None, body_text=None, from_addr=None) is None
