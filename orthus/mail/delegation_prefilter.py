"""메일 위임 추출 앞단의 **결정론 프리필터** (R2). LLM 0회.

왜: `agent_task` 게이트는 "이 텍스트가 정말 위임인가"를 검사하지 않는다 — 그 판단은 LLM이 신뢰할 수
없는 외부 메일 본문 위에서 내린다. 적대적 홀드아웃(`experiments/fugu-ko/golden/t10_holdout2.json`)에서
**가장 좋은 모델(EXAONE)조차 함정 24개 중 2개를 오탐**했고, 둘 다 실존 팀원을 assignee로 뽑았다.
R1이 그 오탐을 사람 검토로 받아내지만, **애초에 LLM에게 묻지 않아도 되는 메일이 있다.**

기계가 보낸 메일 — 회의록, 부재중 자동응답, 티켓 시스템 알림 — 은 **형식으로 판별된다.** 그런 건
LLM에 보내지 않는다. 오탐이 줄고, LLM 호출도 준다.

## 설계 원칙: 진짜 위임을 죽이지 않는다 (FN=0이 1차 기준)

단순 키워드 매칭은 **위험하다** — "김철수님, **회의록** 정리해서 공유해주세요"는 **진짜 위임**인데
`회의록`이라는 단어를 담고 있다. 그래서 내용어가 아니라 **구조적 신호**만 본다:

- 제목/본문 **첫머리의 대괄호 태그**(`[회의록]`, `[Jira]`, `[캘린더]`) — 사람은 본문을 이렇게 안 연다
- **자동응답 정형구**로 시작(`부재중`, `Out of Office`, `자동응답:`)
- **기계 발신자**(`no-reply@`, `notifications@`, `mailer-daemon@`)
- **액션아이템 목록 형식**(`액션 아이템` + 번호 매긴 `이름 —` 줄) — 회의록의 서명이다

이 신호들은 **사람이 쓴 위임 지시문에는 나타나지 않는다.** 반대로 함정에는 나타난다.
전 골든(진짜 위임 18건: t10 10 + holdout2 8)에서 **오차단 0**임을 회귀로 고정한다.

프리필터가 잡지 못한 함정은 R1(사람 검토)이 받는다. 두 장치는 **직렬**이다.
"""

from __future__ import annotations

import re

# 제목이나 본문 **첫머리**의 대괄호 태그. 기계/템플릿이 붙이는 것이고, 사람이 팀원에게 일을
# 부탁하는 메일은 이렇게 시작하지 않는다. (본문 중간의 `[회의록]` 인용은 잡지 않는다 — 그건
# 사람이 회의록을 언급하며 일을 시키는 정상 위임일 수 있다.)
_MACHINE_TAG = re.compile(
    r"^\s*\[\s*(?:"
    r"회의록|미팅\s*노트|meeting\s*notes?|minutes"
    r"|jira|linear|asana|notion|github|gitlab|slack|confluence"
    r"|캘린더|calendar|invite|초대"
    r"|알림|notification|reminder|자동\s*알림"
    r")\s*[\]:]",
    re.IGNORECASE,
)

# 자동응답 정형구 — **첫머리**에서만 본다.
_AUTO_REPLY = re.compile(
    r"^\s*(?:"
    r"부재\s*중(?:\s*자동)?\s*응답|자동\s*응답|자동회신"
    r"|out\s+of\s+office|auto[-\s]?reply|automatic\s+reply"
    r"|이\s*메일은\s*자동으로"
    r")",
    re.IGNORECASE,
)

# 기계 발신자. 사람이 여기서 위임하지 않는다.
_MACHINE_SENDER = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|noreply|notifications?|alerts?"
    r"|mailer-daemon|postmaster|automated?|system|jira|github|gitlab)@",
    re.IGNORECASE,
)

# 회의록의 서명: "액션 아이템" 헤더 + 번호 매긴 "이름 — 할 일" 줄. 둘 다 있어야 한다.
# ("액션 아이템 정리해줘" 같은 위임은 목록 형식이 없으므로 살아남는다.)
_ACTION_ITEMS_HEADER = re.compile(r"(?:^|\n)\s*(?:액션\s*아이템|action\s*items?)\s*[:\n]", re.I)
_NUMBERED_ASSIGNMENT = re.compile(r"(?m)^\s*\d+[.)]\s*\S+\s*(?:—|-|–|:)\s*\S")

# 티켓 시스템의 배정 **통보**. "~에게 할당되었습니다"는 이미 일어난 일의 기록이지 지시가 아니다.
_ASSIGNMENT_NOTICE = re.compile(
    r"(?:에게\s*)?(?:할당|배정)(?:되었습니다|됨|되었음)"
    r"|has\s+been\s+assigned\s+to"
    r"|was\s+assigned\s+to",
    re.IGNORECASE,
)


def non_delegation_reason(
    *,
    subject: str | None,
    body_text: str | None,
    from_addr: str | None,
) -> str | None:
    """메일이 **구조적으로** 위임이 아니면 그 사유를, 아니면 None.

    None은 "위임일 수도 있다"이지 "위임이다"가 아니다 — 판단은 여전히 LLM이 하고, 그 판단은
    R1의 사람 검토를 거친다. 이 함수는 **LLM에게 물을 가치도 없는 메일**만 걸러낸다.

    반환된 사유는 audit에 남는다(어떤 메일이 왜 잘렸는지 추적 가능해야 한다).
    """
    subj = (subject or "").strip()
    body = (body_text or "").strip()
    sender = (from_addr or "").strip()

    if sender and _MACHINE_SENDER.search(sender):
        return "machine_sender"
    if _MACHINE_TAG.search(subj) or _MACHINE_TAG.search(body):
        return "machine_tag"
    if _AUTO_REPLY.search(subj) or _AUTO_REPLY.search(body):
        return "auto_reply"
    if _ACTION_ITEMS_HEADER.search(body) and _NUMBERED_ASSIGNMENT.search(body):
        return "meeting_action_items"
    if _ASSIGNMENT_NOTICE.search(subj) or _ASSIGNMENT_NOTICE.search(body):
        return "assignment_notice"
    return None
