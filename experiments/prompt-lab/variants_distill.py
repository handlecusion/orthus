"""Track C — distill 프롬프트 변형 레지스트리.

`baseline`은 프로덕션 `orthus/wiki/distill.py`의 `_SYSTEM`/`_JSON_SHAPE`를 그대로
쓴다(패치 0). 변형은 라운드마다 여기 추가한다 — 한 라운드에 1–2개 축만 바꾼다
(동시 변경 금지: 승인 요인 추적 불가). 변형 dict 키:

- ``system``: `_SYSTEM` 대체 텍스트 (None이면 프로덕션 유지)
- ``json_shape``: `_JSON_SHAPE` 대체 텍스트 (None이면 프로덕션 유지)
- ``note``: 가설 한 줄 (analysis/round-N.md에 기록)
"""

from __future__ import annotations

VARIANTS: dict[str, dict] = {
    "baseline": {
        "system": None,
        "json_shape": None,
        "note": "프로덕션 현행 (cap 20 + 전수 추출, C6 fallback 제거 이후)",
    },
    "kr-v1": {
        # H-C1: 지시문 한국어화. 규칙 내용(cap 20·원자성·slug 영어 kebab·headline
        # 120자·open_questions 3·entity 16)은 baseline과 1:1 동일 — 언어 축만 변경.
        # "(knowledge distillation)" 영문 병기는 프로덕션 채택 시 MockChat matcher
        # 폴백("distillation" 부분문자열)을 깨지 않기 위한 보존이다.
        "system": (
            "당신은 사내 위키를 위한 지식 증류(knowledge distillation) 엔진이다. "
            "문서를 읽고 구조화된 요약과 원자적 claim들을 추출하라. "
            "단일 JSON 객체만 출력하고 다른 텍스트는 내지 마라. "
            "산문은 문서의 언어를 따르라. "
            "각 claim의 page.slug는 그 claim이 속한 개념을 가리키는 간결한 영어 "
            "소문자 kebab-case 식별자(a-z, 0-9, 하이픈만)로 정하라: 비영어 개념명은 "
            "영어로 번역하고, 같은 개념이면 다른 문서에서도 정확히 같은 slug를 "
            "재사용해 관련 claim들이 하나의 위키 페이지로 수렴하게 하라. "
            "page.title과 그 외 모든 산문은 문서의 언어를 유지하라. "
            "각 claim은 문서에서 뽑은 구체적 근거(evidence)를 갖춘, 하나의 "
            "자기완결적이고 검증 가능한 진술이어야 한다. "
            "또한 각 claim에 `headline`을 부여하라: 지식그래프 노드 라벨로 읽기 "
            "좋은, 사람이 읽는 한 줄 제목(약 120자 이내, 문서의 언어, `Q:`/`A:` "
            "접두사 금지)으로 claim을 요약한다. "
            "문서가 뒷받침하는 검증 가능한 claim을 빠짐없이 추출하라 — 일찍 멈추지 "
            "말고, 여러 사실을 하나의 claim으로 합치지 마라. 복합 claim 몇 개보다 "
            "원자적 claim 여러 개를 선호하라. "
            "claim은 최대 20개까지 반환하라. open_questions는 최대 3개까지, 사람의 "
            "답이 지속 가치 있는 위키 지식을 실질적으로 개선하는 경우에만 반환하라. "
            "텍스트에 보이지 않는 미디어 내용, 파일 소유/업로드 메타데이터, "
            "인증/로그인 상태, CI 실행 ID, 일반적인 후속 작업은 묻지 마라. "
            "또한 문서에 명시적으로 언급된 개체(named entity)를 "
            '{"kind", "name"} 객체로 나열하라. kind는 person, org, project, system '
            "중 하나이고 name은 텍스트에 나온 표기 그대로 쓴다. entity는 최대 "
            "16개까지만 나열하고, 지어내지 마라."
        ),
        "json_shape": None,  # 스키마는 baseline 동일 유지 (축 고립)
        "note": "H-C1 지시문 한국어화 — Solar 한국어 특화와 지시 언어 정합이 이득인지",
    },
    "ext-v1": {
        # H-C4 (round 2): baseline 영어 지시문 + 외부지식/추정 금지 규칙만 추가.
        # round 1 실측 오염 패턴(배경지식 주입·날짜/대상 단정)을 직접 겨냥 — 축 1개.
        "system": None,  # 아래 system_append로 조립 (baseline 문구 드리프트 방지)
        "system_append": (
            " Every claim and its evidence must state ONLY what the document "
            "itself says. Do not add background knowledge, world facts, or "
            "definitions from outside the document. Do not infer or guess "
            "dates, audiences, owners, or numbers that the document does not "
            "state explicitly. If the document does not say it, do not claim it."
        ),
        "json_shape": None,
        "note": "H-C4 외부지식/추정 금지 — round 1 오염 패턴 직접 타격",
    },
    "quote-v1": {
        # round 3: evidence를 원문 verbatim 인용으로 강제. 문구 개입이 아니라
        # 구조 개입 — evidence가 원문 부분문자열인지 결정론 검사가 가능해져,
        # 채택 시 프로덕션 결정론 게이트(비인용 evidence claim drop) 후보.
        "system": None,
        "system_append": (
            " Each claim's `evidence` field must be a VERBATIM quote copied "
            "character-for-character from the document (you may shorten it, but "
            "never paraphrase, translate, or reorder). If you cannot find a "
            "verbatim quote in the document that supports a claim, do not emit "
            "that claim."
        ),
        "json_shape": None,
        "note": "H-C5 evidence verbatim 인용 강제 — 오염의 구조적 차단 + 결정론 게이트 후보",
    },
}
