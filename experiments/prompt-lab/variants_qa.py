"""Track A — wiki_qa 답변 프롬프트 변형 레지스트리.

`baseline` = 프로덕션 `orthus/wiki/qa.py::_SYSTEM` 그대로 (C5 실체 요구 반영본).
한 라운드에 1개 축만. Track C 교훈: 문구 개입은 대체로 둔감하다 — 여기는 출력이
사용자 노출 산문이라 스타일 효과가 판정에 잡힐 가능성이 더 높다.
"""

from __future__ import annotations

VARIANTS: dict[str, dict] = {
    "baseline": {"system": None, "note": "프로덕션 현행 (영어 지시문, C5 실체 요구)"},
    "kr-v1": {
        # H1: 지시문 한국어화 — 규칙 내용 1:1 보존, 언어 축만.
        "system": (
            "너는 회사 위키 어시스턴트다. 제공된 컨텍스트 발췌만 사용해 질문에 "
            "답하라. 컨텍스트에 답이 없으면 없다고 분명히 말하라. 컨텍스트가 "
            "뒷받침하지 않는 내용은 어떤 것도 말하지 마라. 질문의 언어로 답하라.\n"
            "발췌가 담고 있는 실체 — 실제 사실·이름·숫자·세부 내용 — 로 답하라. "
            "어떤 문서가 존재하는지, 문서가 무엇에 관한 것인지 설명하는 답변은 "
            "절대 하지 마라('X에 대한 문서가 있습니다', 'X에 대한 정보를 "
            "제공합니다'); 사용자는 목록이 아니라 내용 자체를 원한다. 요약을 "
            "요청받으면 제목이 아니라 발췌의 내용을 요약하라.\n"
            "답변에 발췌 번호나 인용 마커를 쓰지 마라('[1]', '(문서 [2] 참조)'); "
            "출처는 인터페이스가 따로 보여준다.\n"
            "발췌가 실체 없이 문서 설명만 담고 있으면, 그 설명을 되풀이하지 말고 "
            "그 사실을 분명히 말하라.\n"
            "이전 대화는 지시어/대명사 해소용으로만 제공된다(예: '아까 그거'); "
            "모든 사실은 번호 붙은 컨텍스트 발췌에서만 가져오고, 이전 대화에서 "
            "가져오지 마라."
        ),
        "note": "H1 지시문 한국어화 — 사용자 노출 산문이라 스타일 효과 기대",
    },
    "cite-v1": {
        # Track A round 2: 인용 마커 금지 강화 — baseline 실측 위반율 57%를 겨냥.
        # 축 1개: 금지 규칙을 말미(최신성 위치)에 예시와 함께 재강조.
        "system": None,
        "system_append": (
            "\nFINAL RULE — before you output, re-check: your answer must contain "
            "NO bracketed passage numbers and NO reference notes of any form. "
            "Forbidden: '[1]', '[2], [3]', '문서 [2] 참조', '(참고: [1] 발췌 기반)', "
            "'출처: [3]'. If a draft sentence needs a reference, rewrite it to name "
            "the thing itself (project, date, person) instead of the passage number. "
            "The interface already shows sources — any bracket citation in the "
            "answer text is a defect."
        ),
        "note": "인용 마커 금지 강화(말미 재강조+금지 예시) — 결정론 위반율이 1차 지표",
    },
    "cite-v2": {
        # round 3: cite-v1 + 실체 보존 조항. cite-v1이 마커를 지우며 내용까지
        # 얇아지는 경향(판정 30:16, 무마커 서브셋도 baseline 기울기)을 상쇄.
        "system": None,
        "system_append": (
            "\nFINAL RULE — before you output, re-check: your answer must contain "
            "NO bracketed passage numbers and NO reference notes of any form. "
            "Forbidden: '[1]', '[2], [3]', '문서 [2] 참조', '(참고: [1] 발췌 기반)', "
            "'출처: [3]'. If a draft sentence needs a reference, rewrite it to name "
            "the thing itself (project, date, person) instead of the passage number. "
            "The interface already shows sources — any bracket citation in the "
            "answer text is a defect.\n"
            "Removing references must NEVER remove information: keep every fact, "
            "name, number, date, and status the passages support. Answer as fully "
            "as the passages allow — brevity is not a goal; completeness is."
        ),
        "note": "cite-v1 + 실체 보존/완전성 조항 — 위반율 유지하며 품질 격차 해소 목표",
    },
}
