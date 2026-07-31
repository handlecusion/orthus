"""Compound-question decompose: should_decompose → split → fan_out → synthesize.

Phase 1 (MVP): single-pass parallel dispatch. No ReAct loop. coordinate() is a
no-op that always returns done=True (Phase 2 hook, kept as a test-fixed stub).
depends_on on SubQuestion is reserved and always ignored in Phase 1.

Priority in answer(): decompose > agentic > legacy.
- Leaf calls use allow_decompose=False to prevent recursion (depth 1 fixed).
- learn=False / record_gaps=False on every leaf to suppress T2 writes and gap
  duplicates (불변식 5).

Concurrency: ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) per request.
ContextVar forwarding: executor.submit(copy_context().run, leaf_fn, ...) is
mandatory — run_in_executor() does NOT copy ContextVars (불변식 15).

SSE: publish_threadsafe() from worker threads; stream key = f"{user_id}:{stream_id}".

docs/company-agent-orchestration.md §4–§12.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from orthus.agentwork import stream as agent_stream
from orthus.audit import audit, redact_pii_text
from orthus.models.base import ChatModel
from orthus.models.orchestration import TASK_DECOMPOSE, TASK_SYNTHESIZE, get_chat_model_for
from orthus.router.tools import classify_subpart
from orthus.schemas.canonical import (
    CoordinateResult,
    GapReason,
    RoutedAnswer,
    SubAnswer,
    SubQuestion,
    SynthesizedBody,
    WikiSourceRef,
)
from orthus.settings import get_settings
from orthus.structured.query import retry_signal

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# Gap reasons that mean "this leaf produced NO answer" — see _run_leaf. `weak_retrieval` is
# deliberately absent: detect_gap only reaches it when `_looks_insufficient` is False, i.e. the
# leaf DID answer and merely scored below the retrieval threshold (measured on 25 answerable
# company questions: 1/25 fired, at 0.544 vs a 0.55 threshold). Dropping those would delete a
# sub-answer from the synthesized body — content loss unrelated to the refusal bug being fixed.
# `missing_link` IS ungrounded: wiki/gap.py::maybe_missing_link only upgrades an
# `insufficient_grounding` gap, so the body is still a refusal; the reason merely adds "…and the
# related pages exist but are disconnected in the KG". (Decompose leaves run record_gaps=False, so
# the upgrade is never even invoked there — this pins the meaning, not a live branch.)
#
# INTENTIONALLY different from the cache path (router/cache.py::is_cacheable_result requires the
# stricter `gap is None`). Cost asymmetry, not drift: a cache misjudgment costs a cache miss
# (performance), a decompose misjudgment costs a dropped sub-answer (content). Do not "re-unify"
# these two predicates — the strictness gap is the design.
_UNGROUNDED_GAP_REASONS: frozenset[GapReason] = frozenset(
    {"no_data", "insufficient_grounding", "missing_link"}
)

# ── §4.1 False-prefilter signals ────────────────────────────────────────────
# Reused from agentwork/service.py — import at call-time to avoid circular dep
_CONNECTIVE_TOKENS = (
    "그리고",
    "그다음",
    "또한",
    "동시에",
    "그후",
    "알려주고",
    "해주고",
)
_ENUM_TOKENS = (
    "①",
    "②",
    "③",
    "첫째",
    "둘째",
)

# ── O4/E3 확장 신호 — should_decompose 전용, 티어별 ───────────────────────────
# 위 두 상수는 `command_split_signal`(command-split 모집단)과 `mail_has_orchestration_signal`
# (event-orch 트리거 모집단)이 함께 읽는 **공유 토큰셋**이다. 상수를 직접 늘리면 세 경로의
# 모집단이 한꺼번에 오염된다. 그래서 확장분은 별도 티어 테이블로 두고 `should_decompose`만
# 티어를 넘겨 읽는다. tier=0(default)이면 아래 테이블은 조회되지 않아 세 경로 모두 기존과
# byte-identical하다.
#
# O4 발견: 기존 신호가 접속어 7개 + 열거 5개 + "1."&"2." 뿐이라 "랑/이랑/각각/비교형" 복합
# 질문은 LLM 게이트에 도달조차 못 하고 단일로 답한다(누락형 프리필터 recall 0%). 티어는 단일
# 질문에서의 출현 빈도(= FP 위험) 순이다:
#   T1 저위험 — 단일 질문에 드문 어휘 (각각/비교/차이/vs/둘 다/모두 알려/셋째)
#   T2 중위험 — 조사·연결어미라 단일 질문에도 출현 ("환불이랑 배송", "~는 뭐고")
#   T3 고위험 — 병렬 조사. 단일 "A와 B의 관계는?"과 어휘로는 구분 불가 (채택은 측정 조건부)
#   T4 고위험 — "-고" 접속어미. 순진하게 "고"를 넣으면 "이거 처리하고 확인해줘" 같은 단일
#       명령절까지 삼켜 precision이 붕괴한다. 그래서 토큰 매칭이 아니라 **co-occurrence 규칙**
#       (`_has_t4_go_signal`): 절경계 "-고"(뒤에 공백/쉼표) + 보조용언 활용 제외 + 고-종결
#       명사 제외 + 후행 절이 독립 의문/순차 지시(그·거기·다음)일 때만 발화. 이 tier는
#       토큰이 아니라 predicate라 아래 테이블에 빈 엔트리로 두고 `_has_ext_signal`에서
#       특수 분기한다(MAX_TIER 자동 갱신 + clamp 로직 재사용 목적).
# 티어는 누적(tier=2 → T1+T2). 프리필터 오통과의 실비용은 LLM 게이트 1콜(지연)이고, 진짜
# 비용인 최종 오분해는 뒤의 LLM 게이트가 거른다 — 두 비용을 분리해 측정한 뒤 티어를 채택한다.
#
# 각 티어는 (compact 매칭 토큰, 원문 매칭 토큰). compact는 lower()+공백제거이므로 공백에
# 의존하는 조사("랑 ", "와 ")는 원문으로 매칭해야 한다 — compact에서는 "사랑"의 "랑"과
# 구분되지 않는다.
_PREFILTER_EXT_TIERS: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {
    1: (("각각", "비교", "차이", "vs", "둘다", "모두알려"), ("셋째", "④")),
    # "뭐고"는 는/은/이 뭐고를 한 토큰으로 덮는다(기존 t7 골든의 "리텐션 정책이 뭐고 …"
    # 형태 포함). "랑 "은 공백이 필요해 원문 매칭이다 — 모음 종결 명사는 "메뉴랑 가격"처럼
    # "이랑" 없이 붙는다.
    2: (("이랑", "뭐고", "이고"), ("랑 ",)),
    3: ((), ("와 ", "과 ")),
    # T4는 predicate 기반 — 토큰 테이블은 비운다(`_T4_TIER` 특수 분기 참조).
    4: ((), ()),
}
_PREFILTER_EXT_MAX_TIER = max(_PREFILTER_EXT_TIERS)
_T4_TIER = 4

# 병렬 조사 + 관계어 억제 (실측 기반, LLM 0회).
#
# 1차 측정에서 확장 티어가 자연 트래픽(t5/t7 단일 질문 27)에 흘린 오통과는 **전부 관계형
# 질문**이었다 — "박기획이랑 김철수는 무슨 관계야?", "nova와 연결된 문서", "이 문서와 관련된
# 담당자가 누구야?". 이건 두 항목의 **병렬 나열**이 아니라 **쌍에 대한 단일 질문**(graph 라우트
# 모집단)이고, LLM 게이트도 전부 no로 정확히 막았다(= 품질 비용 0, 지연 비용만 발생).
#
# 그래서 신호를 결정론으로 좁힌다: **병렬 조사**(아래 집합)만으로 발화하려는데 관계어가 함께
# 있으면 발화하지 않는다. 어휘 신호(각각/비교/차이/vs/둘 다/뭐고/이고)는 억제하지 않는다 —
# 그 신호는 관계어와 무관하게 병렬 의도를 가리킨다. 이 억제로 자연 트래픽 오통과 증분이
# +22.2%p → +0%p가 되고 누락형 recall 100%는 그대로다(§7 실측).
#
# 억제어는 **명시적 관계 명사**만 넣는다. "관련"은 일부러 뺐다 — 관계 표지로는 약한 데다
# 회사 고유명사와 충돌한다(실측: "AI관련툴 목록이랑 팀원 목록 보여줘"가 Notion DB 이름
# "AI관련툴" 때문에 억제돼 누락형 1건을 잃었다). 억제어를 4개로 좁히면 자연 오통과 증분
# +0%p를 유지하면서 recall 100%가 된다.
_PARALLEL_PARTICLES = frozenset({"이랑", "랑 ", "와 ", "과 "})
_RELATIONAL_SUPPRESS = ("관계", "연결", "연관", "사이")

# ── T4 "-고" 접속어미 신호 (predicate; LLM 0회) ──────────────────────────────
# 절경계 "-고" 뒤에 두 번째 절이 독립 의문(모드 A) 또는 순차 지시(모드 C)를 가질 때만 발화.
# 보조용언 활용("높이고 싶다"/"진행하고 있다")과 고-종결 명사("사고 원인")는 제외해 단일
# 질문 오통과를 막는다. 실측(prefilter-t4-results.md): 모드 A+C recall 14/14, e3_control/H2
# trap 오통과 0%.
_T4_AUX_AFTER = (
    "싶",
    "있",
    "계시",
    "나서",
    "나면",
    "보니",
    "보면",
    "자",
    "서",
    "말",
    "드리",
    "봐",
    "볼",
    "나가",
)
# 두 번째 절의 독립 의문 표지(의문사 + 종결 의문 어미). 요청/명령형 종결(줘/세요)은 command
# 분할 소관이라 의도적으로 뺐다 — 여기는 순수 질문 병렬만 본다.
_T4_INTERROG = (
    "뭐",
    "무엇",
    "무슨",
    "어때",
    "어떻",
    "어디",
    "얼마",
    "누구",
    "누가",
    "몇",
    "왜",
    "언제",
    "어느",
    "어떤",
    "있어",
    "없어",
    "됐어",
    "했어",
    "인지",
    "는지",
    "을지",
)
_T4_Q_END = ("?", "야", "어", "까", "나요", "가요", "까요")
# 순차 의존 지시어(모드 C): "…찾고, 그 사람이 …".
_T4_DEMONSTRATIVE = ("그", "거기", "다음", "이후", "해당")
# 고-종결 명사 — "-고" 절경계로 오인되면 안 되는 것들(단독 단어일 때만 차단).
# 실트래픽 검증(prefilter-t4-realtraffic.md §3)에서 misfire 27건 중 20건(74.1%)이 이 리스트의
# 공백("회고" 63.0%·"로고" 7.4%·"공고" 3.7%) 때문이었다 — "회고"는 회사가 "주간 회고" 문서를
# 반복 생성하는 상용어라 압도적 1위. "충고"/"재고"는 실트래픽 misfire엔 없었지만 같은 패턴의
# 흔한 "고"-종결 명사라 선제 보강한다. 이 명사들 다수는 "-하다" 동사 어간이기도 하다
# (보고하다/참고하다/신고하다/재고하다/충고하다) — 그 활용형 "…하고"는 매칭 위치가 노트와
# 겹치지 않아(노트 끝 = 절경계 "고" 바로 앞인데 "…하고"는 그 앞이 "하"라 노트 문자열과
# 불일치) 이 리스트에 넣어도 억제되지 않는다. 회귀: `TestT4NounExceptionPrecision`.
_T4_NOUN_GO = (
    "사고",
    "보고",
    "광고",
    "신고",
    "참고",
    "최고",
    "예고",
    "창고",
    "원고",
    "중고",
    "냉장고",
    "경고",
    "권고",
    "금고",
    "회고",
    "로고",
    "공고",
    "충고",
    "재고",
)


def _is_hangul_syllable(ch: str) -> bool:
    return "가" <= ch <= "힣"


def _has_relational_term(lowered: str) -> bool:
    """관계 명사가 **경계 인식**으로 실재하는가 (bug fix, LLM 0회).

    기존 구현은 `any(x in compact ...)`라 "디아더사이드"의 "사이"처럼 회사 고유명사 내부
    부분일치까지 관계어로 잡아 병렬 조사 신호를 통째로 무효화했다(진단 §3-E, c34). 억제어가
    양쪽 모두 한글 음절로 둘러싸인 순수 단어-내부 매칭이면 관계 표지로 보지 않는다 — 최소
    한쪽 경계(공백/조사/문장부호/문자열 끝 등 비한글)가 있어야 실제 관계어로 발화한다."""
    for term in _RELATIONAL_SUPPRESS:
        start = 0
        while True:
            i = lowered.find(term, start)
            if i < 0:
                break
            before = lowered[i - 1] if i > 0 else ""
            after = lowered[i + len(term)] if i + len(term) < len(lowered) else ""
            if not (_is_hangul_syllable(before) and _is_hangul_syllable(after)):
                return True
            start = i + 1
    return False


def _t4_is_noun_go(lowered: str, i: int) -> bool:
    """위치 i의 "고"가 고-종결 명사의 끝이고, 그 명사가 단독 단어(앞 경계 비한글)인가."""
    for noun in _T4_NOUN_GO:
        s = i - len(noun) + 1
        if s >= 0 and lowered[s : i + 1] == noun:
            before = lowered[s - 1] if s > 0 else ""
            if not _is_hangul_syllable(before):
                return True
    return False


def _has_t4_go_signal(lowered: str) -> bool:
    """절경계 "-고" + 독립 후행절(의문/순차 지시) co-occurrence 신호 (LLM 0회)."""
    for m in re.finditer("고", lowered):
        i = m.start()
        nxt = lowered[i + 1] if i + 1 < len(lowered) else ""
        if nxt not in (" ", "\t", ",", "，", "."):
            continue  # 절경계(뒤에 공백/쉼표)만 후보
        rest = lowered[i + 1 :].lstrip(" \t,，")
        if rest[:1] and any(rest.startswith(a) for a in _T4_AUX_AFTER):
            continue  # 보조용언 활용("-고 싶다"/"-고 있다") → 단일 절
        if _t4_is_noun_go(lowered, i):
            continue  # 고-종결 명사("사고 원인") → "-고" 접속 아님
        tail = lowered[i + 1 :]
        tail_stripped = tail.rstrip()
        if (
            any(t in tail for t in _T4_INTERROG)
            or tail_stripped.endswith("?")
            or any(tail_stripped.endswith(e) for e in _T4_Q_END)
            or any(d in tail for d in _T4_DEMONSTRATIVE)
        ):
            return True
    return False


# ── §4.2 LLM enum prompts ────────────────────────────────────────────────────
# few-shot 예시는 DF1 재측정(experiments/fugu-ko/analysis/df-results.md §6, owner 승인
# 2026-07-19)에서 채택했다 — Solar few-shot 94.4% vs zero-shot 91.2%(H2 n=160, 자체
# 차이는 p=.18로 유의하지 않지만 무비용이고 손해가 없다). 예시는 H2 홀드아웃과 배타적인
# t7.json(22문항 골든)에서만 뽑아 홀드아웃 오염을 피했다.
_DECOMPOSE_SYSTEM = (
    "You are a compound-question detector. Decide if the user's question contains "
    "MULTIPLE INDEPENDENT sub-questions that can each be answered separately. "
    "A question is compound if it contains two or more independent requests "
    "(different topics, different actions). A question that asks multiple aspects "
    "of the SAME topic is NOT compound.\n"
    'Respond with JSON only: {"decompose": "yes"} | {"decompose": "no"} | {"decompose": "uncertain"}\n'
    "\n"
    "Examples:\n"
    "Question: 환불 정책은 어떻게 되고 배송 정책은 어때?\n"
    'JSON: {"decompose": "yes"}\n'
    "Question: 온보딩 절차랑 리텐션 정책이랑 KPI 정의를 정리해줘\n"
    'JSON: {"decompose": "yes"}\n'
    "Question: 아크메 환불 정책이 어떻게 돼?\n"
    'JSON: {"decompose": "no"}\n'
    "Question: Nova와 아크메는 무슨 관계야?\n"
    'JSON: {"decompose": "no"}'
)

# ── §5.2 split_question prompt ───────────────────────────────────────────────
_SPLIT_SYSTEM_TMPL = (
    "You are a question-splitter for a company knowledge assistant. "
    "Split the compound Korean/English question into at most {k} independent sub-questions.\n"
    "Rules:\n"
    "1. Each sub-question must be FULLY self-contained (answerable without the others).\n"
    "2. Keep phrasing close to the original. Do not add information not in the original.\n"
    "3. If the question cannot be split into 2 or more independent parts, return an empty list `[]`.\n"
    '4. Respond with JSON only: {{"sub_questions": ["<sub-q 1>", "<sub-q 2>", ...]}}\n'
    "\n"
    "Examples:\n"
    "Question: 환불 정책은 어떻게 되고 배송 정책은 어때?\n"
    'JSON: {{"sub_questions": ["환불 정책은 어떻게 돼?", "배송 정책은 어때?"]}}\n'
    "Question: 온보딩 절차랑 리텐션 정책이랑 KPI 정의를 정리해줘\n"
    'JSON: {{"sub_questions": ["온보딩 절차를 정리해줘.", "리텐션 정책을 정리해줘.", '
    '"KPI 정의를 정리해줘."]}}'
)

# ── §P2.2 split_question + depends_on prompt (Phase 2, flag on) ───────────────
_SPLIT_DEPS_SYSTEM_TMPL = (
    "You are a question-splitter for a company knowledge assistant. "
    "Split the compound Korean/English question into at most {k} sub-questions, and "
    "mark dependencies between them.\n"
    "Rules:\n"
    "1. Keep phrasing close to the original. Do not add information not in the original.\n"
    "2. If sub-question B can only be answered AFTER sub-question A (B uses A's result, "
    "e.g. 'look up X' then 'email that number to Y'), set B's \"depends_on\" to [A's index]. "
    'Independent sub-questions have "depends_on": [].\n'
    "3. A sub-question may reference ONLY earlier indices (depend only on ones before it).\n"
    "4. If the question cannot be split into 2 or more parts, return an empty list `[]`.\n"
    "5. Respond with JSON only: "
    '{{"sub_questions": [{{"text": "<sub-q>", "depends_on": [<earlier idx>, ...]}}, ...]}}'
)

# ── command-split variants (S3) — used only when command_split=True. The plain
# templates above stay byte-identical for the pure-question decompose path. These add
# ACTION COMMAND preservation so an LLM split never turns a command clause into a question
# or drops its recipient/target (finding F3 / BLOCKER F). The JSON key stays "sub_questions"
# so the parser is unchanged.
_SPLIT_SYSTEM_CMD_TMPL = (
    "You are a request-splitter for a company assistant. The input may MIX knowledge "
    "QUESTIONS and ACTION COMMANDS (send an email, sync a connector, draft a document, "
    "clean up a board, etc.). Split the compound Korean/English input into at most {k} "
    "independent sub-requests.\n"
    "Rules:\n"
    "1. Each sub-request must be FULLY self-contained (understandable without the others).\n"
    "2. Keep phrasing close to the original. Preserve every ACTION COMMAND verbatim as its "
    "own sub-request — never turn a command into a question, and never drop its "
    "recipient/target.\n"
    "3. If the input cannot be split into 2 or more independent parts, return `[]`.\n"
    '4. Respond with JSON only: {{"sub_questions": ["<sub-request 1>", "<sub-request 2>", ...]}}'
)

_SPLIT_DEPS_SYSTEM_CMD_TMPL = (
    "You are a request-splitter for a company assistant. The input may MIX knowledge "
    "QUESTIONS and ACTION COMMANDS. Split the compound Korean/English input into at most {k} "
    "sub-requests, and mark dependencies between them.\n"
    "Rules:\n"
    "1. Keep phrasing close to the original. Preserve every ACTION COMMAND verbatim as its "
    "own sub-request — never turn a command into a question, and never drop its "
    "recipient/target.\n"
    "2. If sub-request B can only be done AFTER sub-request A (B uses A's result, e.g. "
    "'look up X' then 'email that number to Y'), set B's \"depends_on\" to [A's index]. "
    'Independent sub-requests have "depends_on": [].\n'
    "3. A sub-request may reference ONLY earlier indices.\n"
    "4. If the input cannot be split into 2 or more parts, return `[]`.\n"
    "5. Respond with JSON only: "
    '{{"sub_questions": [{{"text": "<sub-request>", "depends_on": [<earlier idx>, ...]}}, ...]}}'
)

# ── §9 synthesis prompt ──────────────────────────────────────────────────────
# syn-v2 (prompt-lab Track A, 2026-07-19): 구 rule 3의 "source markers must survive"가
# 하위 답변의 죽은 인용 마커([1], 문서 [2] 참조)를 최종 답변까지 보존시켜 30건 중 25건이
# 마커를 노출했다(wiki_qa cite-v2 채택 방향과 정면 충돌). 마커 drop 명시로 25→5
# (paired McNemar 20:0, p<1e-5), 사실 보존 judge-pass 동등(29→28/30). action 무시(rule 5→
# FINAL RULE 승격)는 7→6/10으로 프롬프트 개입에 저항 — 잔여는 구조적 후속 과제.
# 근거: experiments/prompt-lab/analysis/qa-round-3.md
_SYNTHESIZE_SYSTEM = (
    "You are a company wiki assistant synthesizing answers to a compound question. "
    "You are given multiple grounded sub-answers, one per sub-question. Combine them "
    "into a single coherent answer. Rules:\n"
    "1. Only use facts from the provided sub-answers — do NOT generate new facts.\n"
    "2. Address EVERY sub-question. Never silently drop one. If a sub-answer states the "
    "information is missing, say so explicitly for that sub-question rather than omitting it.\n"
    "3. Preserve concrete facts verbatim — dates, numbers, names, IDs, and status values "
    "in the sub-answers must survive into the combined answer; do not abstract them away. "
    "However, DROP citation markers and reference notes ('[1]', '문서 [2] 참조', "
    "'(참고: [1])', '출처: [3]') — they are interface artifacts, not facts; the combined "
    "answer must contain none of them.\n"
    "4. Re-arrange and excerpt for clarity; do not invent or merge two distinct facts into one.\n"
    "5. Answer in the question's language (Korean if the question is Korean).\n"
    "FINAL RULE — if the original question also requests an ACTION (sending an email, "
    "scheduling, creating a task, messaging someone, etc.), IGNORE that action entirely: "
    "do not mention it, do not promise it ('보내드리겠습니다' is forbidden), do not say "
    "you cannot do it, and do not address the recipient. The action is handled separately "
    "by the system. Output ONLY the factual answer to the knowledge sub-questions. "
    "Re-check before answering: your output must contain no promise or mention of any action."
)


# ── §4.1 False-prefilter ─────────────────────────────────────────────────────


def _has_command_verb(compact: str) -> bool:
    from orthus.agentwork.service import _COMMAND_VERBS

    return any(v.replace(" ", "") in compact for v in _COMMAND_VERBS)


def prefilter_ext_tier() -> int:
    """활성 확장 티어(0=off). 설정값을 [0, MAX]로 clamp한다 — 오설정이 프리필터를
    조용히 전부 통과시키거나(과대) 음수로 깨지지(과소) 않게 한다."""
    tier = get_settings().decompose_prefilter_ext_tier
    return max(0, min(int(tier), _PREFILTER_EXT_MAX_TIER))


def _has_ext_signal(question: str, compact: str, tier: int) -> bool:
    """티어 1..tier의 확장 신호가 하나라도 있으면 True (누적, LLM 0회).

    **병렬 조사**(`_PARALLEL_PARTICLES`)는 관계어가 함께 있으면 발화하지 않는다
    (`_RELATIONAL_SUPPRESS`) — "A와 B는 무슨 관계야?" 같은 관계형 단일 질문을 병렬 나열로
    오검출하지 않기 위한 실측 기반 좁히기다. 어휘 신호(각각/비교/뭐고 …)는 억제 대상이 아니다.

    tier는 여기서 [0, MAX]로 clamp한다 — 어떤 호출자(설정/테스트/측정 하네스)가 범위 밖
    값을 넘겨도 조회 실패로 깨지거나 정의되지 않은 티어를 통과시키지 않는다."""
    lowered = question.lower()
    relational = _has_relational_term(lowered)

    def fires(token: str, haystack: str) -> bool:
        if relational and token in _PARALLEL_PARTICLES:
            return False
        return token in haystack

    for t in range(1, min(tier, _PREFILTER_EXT_MAX_TIER) + 1):
        compact_tokens, raw_tokens = _PREFILTER_EXT_TIERS[t]
        if any(fires(x, compact) for x in compact_tokens) or any(
            fires(x, lowered) for x in raw_tokens
        ):
            return True
        if t == _T4_TIER and _has_t4_go_signal(lowered):
            return True
    return False


def _has_connective_or_enum(question: str, *, ext_tier: int = 0) -> bool:
    """접속/열거 신호(LLM 0회). `ext_tier`는 **should_decompose 전용** 확장분이다.

    default 0은 기존 공유 토큰셋만 본다 — `command_split_signal`과
    `mail_has_orchestration_signal`이 이 default로 호출하므로 두 모집단은 불변이다.
    """
    compact = question.lower().replace(" ", "")
    # Check enumeration hints: "1." followed by "2." anywhere in original text
    has_numbered = "1." in question and "2." in question
    return (
        any(t.replace(" ", "") in compact for t in _CONNECTIVE_TOKENS)
        or any(t in question for t in _ENUM_TOKENS)
        or has_numbered
        or (ext_tier > 0 and _has_ext_signal(question, compact, ext_tier))
    )


def command_split_signal(question: str) -> bool:
    """command_split(명령/질문 상위 분할) 진입용 결정론 신호(BLOCKER F/G, LLM 0회).

    should_decompose 프리필터가 쓰는 접속/열거 신호(_has_connective_or_enum)를 그대로
    재사용한다 — 공유 토큰셋은 절대 수정하지 않는다(수정 시 should_decompose 모집단이 오염돼
    flag-off byte-identical이 깨진다). O4/E3 확장(`_PREFILTER_EXT_TIERS`)도 같은 이유로 여기
    적용하지 않는다: `ext_tier`를 넘기지 않아 default 0으로 호출하므로 command-split 모집단은
    확장 티어와 무관하게 불변이다."""
    return _has_connective_or_enum(question)


def _command_split_bypasses_should_decompose(question: str, command_split: bool) -> bool:
    """command_split이 should_decompose LLM 게이트를 우회할지 결정한다(review #3).

    command_split_signal(== _has_connective_or_enum)은 연결어/열거만으로 발동하고 명령-verb를
    요구하지 않는다. 따라서 실제 명령 절이 없는 순수 복합 질문("환불정책은 뭐고 배송은 어때")도
    command_split=True가 될 수 있는데, should_decompose를 무조건 우회하면 LLM이
    non-decomposable로 판정했을 질문을 강제로 fan-out해 불필요 LLM 호출 + synthesis 품질 저하가
    난다. 명령 detector는 결정론(keyword, LLM 0회)이므로, 실제 명령이 있을 때만 우회해 명령 분할은
    유지하고 순수 질문은 should_decompose 판정으로 되돌린다."""
    if not command_split:
        return False
    from orthus.agentwork import detect_assistant_command_action

    return detect_assistant_command_action(question) is not None


# ── §4 should_decompose ──────────────────────────────────────────────────────


def should_decompose(
    question: str, *, chat_model: ChatModel | None = None, ext_tier: int | None = None
) -> bool:
    """Two-stage compound-question detector.

    Stage 1: False-prefilter (no LLM). Immediately returns False when BOTH:
      - no _COMMAND_VERBS token, AND
      - no connective/enumeration hint (O4/E3 확장 티어 포함).
    Stage 2: LLM enum. Called only when the prefilter passes.
    uncertain → False (safe fall-through to single answer).

    `ext_tier`는 확장 프리필터 티어(0=off). None이면 설정
    (`ORTHUS_DECOMPOSE_PREFILTER_EXT_TIER`, default 0)을 따른다 — 명시 인자는 측정 하네스가
    티어를 스윕하기 위한 것이며, 프로덕션 경로는 설정값만 쓴다. 확장은 **이 함수에만** 걸리고
    `command_split_signal`/`mail_has_orchestration_signal` 모집단은 건드리지 않는다.
    """
    compact = question.lower().replace(" ", "")
    if not compact:
        return False

    tier = (
        prefilter_ext_tier() if ext_tier is None else max(0, min(ext_tier, _PREFILTER_EXT_MAX_TIER))
    )
    has_verb = _has_command_verb(compact)
    has_conn = _has_connective_or_enum(question, ext_tier=tier)
    if not has_verb and not has_conn:
        # §4.1: both conditions simultaneously false → immediately False
        return False

    # §4.2: LLM enum
    chat = chat_model or get_chat_model_for(TASK_DECOMPOSE)
    system = _DECOMPOSE_SYSTEM
    try:
        raw = chat.complete(system, f"Question: {question}", json_only=True)
        verdict = json.loads(raw).get("decompose", "uncertain")
    except Exception:
        verdict = "uncertain"

    return verdict == "yes"


# ── §5 split_question ────────────────────────────────────────────────────────


def split_question(
    question: str, *, k: int, chat_model: ChatModel | None = None, command_split: bool = False
) -> list[str]:
    """LLM 1회: compound question → sub-question text list (len 2 ≤ N ≤ k).

    Returns [] on parse failure or len < 2 — caller treats as single fall-through.
    `command_split=True` uses the command-preserving prompt (S3); False is byte-identical.
    """
    chat = chat_model or get_chat_model_for(TASK_DECOMPOSE)
    system = (_SPLIT_SYSTEM_CMD_TMPL if command_split else _SPLIT_SYSTEM_TMPL).format(k=k)
    try:
        raw = chat.complete(system, question, json_only=True)
        items = json.loads(raw).get("sub_questions", [])
    except Exception:
        return []

    if not isinstance(items, list):
        return []

    items = [str(s).strip() for s in items if str(s).strip()]
    if len(items) < 2 or len(items) > k:
        return []
    return items


# ── §P2.2 split_question_with_deps (Phase 2, flag on) ────────────────────────


def split_question_with_deps(
    question: str,
    *,
    k: int,
    chat_model: ChatModel | None = None,
    command_split: bool = False,
) -> list[tuple[str, list[int]]]:
    """LLM 1회: compound question → [(sub-question text, depends_on indices), ...].

    Backward-only enforced: each item's depends_on keeps only indices in [0, self_idx).
    forward/self/out-of-range indices are dropped (acyclic-by-construction, §P2.2.1).
    Accepts both the Phase 2 dict format ({text, depends_on}) and the Phase 1 str
    format (하위호환: str item → depends_on []). Returns [] on parse failure or
    filtered len < 2 — caller treats as single fall-through.
    """
    chat = chat_model or get_chat_model_for(TASK_DECOMPOSE)
    system = (_SPLIT_DEPS_SYSTEM_CMD_TMPL if command_split else _SPLIT_DEPS_SYSTEM_TMPL).format(k=k)
    try:
        raw = chat.complete(system, question, json_only=True)
        items = json.loads(raw).get("sub_questions", [])
    except Exception:
        return []

    if not isinstance(items, list):
        return []

    # First pass: extract (text, raw_depends) preserving order; drop blank/invalid text.
    parsed: list[tuple[str, list[int]]] = []
    for item in items:
        if isinstance(item, str):
            text, deps_raw = item.strip(), []
        elif isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            deps_raw = item.get("depends_on", [])
            if not isinstance(deps_raw, list):
                deps_raw = []
        else:
            continue
        if not text:
            continue
        parsed.append((text, deps_raw))

    if len(parsed) < 2 or len(parsed) > k:
        return []

    # Second pass: backward-only filter against final positional indices.
    result: list[tuple[str, list[int]]] = []
    for i, (text, deps_raw) in enumerate(parsed):
        deps = sorted({d for d in deps_raw if isinstance(d, int) and 0 <= d < i})
        result.append((text, deps))
    return result


def topo_levels(sub_questions: list[SubQuestion]) -> list[list[SubQuestion]]:
    """Decompose sub-questions into dependency waves (Kahn-style levels, §P2.3).

    Wave 0 = no unmet deps; wave N = deps all in earlier waves. Original order is
    preserved within a wave. backward-only depends_on makes a cycle structurally
    impossible; as a fail-safe, if no progress can be made (would-be cycle or a
    dep referencing an unknown id), the remaining nodes collapse into one wave.
    """
    known = {sq.id for sq in sub_questions}
    placed: set[UUID] = set()
    levels: list[list[SubQuestion]] = []
    remaining = list(sub_questions)
    while remaining:
        wave = [
            sq for sq in remaining if all(dep in placed for dep in sq.depends_on if dep in known)
        ]
        if not wave:  # fail-safe: cycle/dangling — collapse remaining into one wave
            wave = remaining
        placed.update(sq.id for sq in wave)
        levels.append(wave)
        remaining = [sq for sq in remaining if sq.id not in placed]
    return levels


def build_sub_questions(sub_pairs: list[tuple[str, list[int]]], *, scope: str) -> list[SubQuestion]:
    """Map index-based depends_on (from split_question_with_deps) to UUID edges."""
    ids = [uuid4() for _ in sub_pairs]
    out: list[SubQuestion] = []
    for i, (text, dep_idx) in enumerate(sub_pairs):
        depends_on = [ids[d] for d in dep_idx if 0 <= d < len(ids)]
        out.append(SubQuestion(id=ids[i], text=text, scope=scope, depends_on=depends_on))
    return out


# ── §2 / §P2.5 coordinate ────────────────────────────────────────────────────


def coordinate(
    sub_answers: list[SubAnswer],
    sub_questions: list[SubQuestion] | None = None,
    *,
    depends_enabled: bool = False,
) -> CoordinateResult:
    """Phase 1 (default / flag off): always done=True no-op (불변식 12/21).

    Phase 2 (depends_enabled=True, §P2.5): deterministic loop-continue. done=True
    when no sub-question is still pending; otherwise next_wave = ids of pending
    sub-questions whose depends_on are all already answered. No LLM (불변식 13/17).
    """
    if not depends_enabled or sub_questions is None:
        return CoordinateResult(done=True)

    answered = {sa.sub_question_id for sa in sub_answers}
    pending = [sq for sq in sub_questions if sq.id not in answered]
    if not pending:
        return CoordinateResult(done=True)
    ready = [sq.id for sq in pending if all(dep in answered for dep in sq.depends_on)]
    return CoordinateResult(done=False, next_wave=ready)


# ── §P2.4 context_from injection ─────────────────────────────────────────────

_STRUCTURED_CONTEXT_ROW_CAP = 5
# §P3B.3.1 / R19 — cumulative upstream-context length cap. At depth > 2 the
# read→act chain prepends an upstream body at each hop, so the injected context
# can grow. Each hop is PII-redacted (불변식 6/19); on top of that we bound the
# total injected text to a fixed budget so a deep chain can't blow the prompt.
_MAX_CONTEXT_CHARS = 4000


def _extract_body(routed: RoutedAnswer | None) -> str:
    """Grounded body text of a sub-answer for downstream context injection.

    wiki/graph → compiled-wiki answer; structured → compact columns/rows summary
    (capped). Returns "" when nothing groundable is present.
    """
    if routed is None:
        return ""
    if routed.wiki is not None and routed.wiki.answer:
        return routed.wiki.answer.strip()
    if routed.structured is not None:
        s = routed.structured
        if s.rows:
            cols = ", ".join(s.columns) if s.columns else ""
            shown = s.rows[:_STRUCTURED_CONTEXT_ROW_CAP]
            rows_txt = "; ".join(" | ".join(str(c) for c in row) for row in shown)
            head = f"[{cols}] " if cols else ""
            return f"{head}{rows_txt}".strip()
        if s.message:
            return s.message.strip()
    return ""


def resolve_context(
    sub_q: SubQuestion,
    sub_answers_by_id: dict[UUID, SubAnswer],
    sub_questions_by_id: dict[UUID, SubQuestion],
    *,
    max_context_chars: int | None = None,
) -> str | None:
    """Build the redacted upstream context injected into a dependent leaf (§P2.4).

    Only grounded upstream sub-answers contribute. Each body is PII-redacted
    (불변식 6/19) before injection. The upstream sub-question's scope is asserted
    equal to the dependent's scope (R15 — parent inheritance makes them structurally
    identical; a mismatch is a bug, raised and isolated as a leaf error by the caller).

    `max_context_chars` (§P3B.3.1 / R19) bounds the cumulative injected text for the
    full-DAG depth chain; it is None outside MA.8b so Phase 2 stays byte-identical
    (불변식 32).
    """
    parts: list[str] = []
    for dep_id in sub_q.depends_on:
        up = sub_answers_by_id.get(dep_id)
        up_sq = sub_questions_by_id.get(dep_id)
        if up is None or not up.grounded or up.routed is None:
            continue
        assert up_sq is not None and up_sq.scope == sub_q.scope, (
            "decompose context_from scope mismatch (R15)"
        )
        body = _extract_body(up.routed)
        if not body:
            continue
        parts.append(redact_pii_text(body))
    joined = "\n\n".join(p for p in parts if p)
    if not joined:
        return None
    # §P3B.3.1 / R19 — bound cumulative injected context (depth chain budget).
    if max_context_chars is not None and len(joined) > max_context_chars:
        joined = joined[:max_context_chars].rstrip()
    return joined


def _structured_unreliable(routed: RoutedAnswer | None) -> bool:
    """True when a structured sub-answer carries the SVC deterministic "이 결과는
    망가져 보인다" signal (게이트 실패 / 미실행 / 0행 / 결과 정수집합 == {0}).

    Why this exists: a zero-answer is a valid SQL that passed the gate and executed
    (`status="executed"`), so `grounded` is True today and the fabricated `0` flows
    into synthesis AND into the read→act dependency gate. E2 measured 11~18% of
    structured leaves inside the loop coming back as zero-answers. LLM 0회 — the
    same pure signal the cascade retries on.
    """
    if routed is None or routed.structured is None:
        return False
    return retry_signal(routed.structured) is not None


def is_command_leaf(text: str) -> bool:
    """리프가 **명령형(행동)**인지 — 결정론 키워드 감지기, LLM 0회.

    public: read→act 게이트(`_deps_unavailable`)와 라우터 계층의 command-preservation
    net(`api/routes/agent_work.py`)이 **같은 판정**을 써야 한다. 게이트가 보류한 리프를
    net이 "조용히 사라졌다"고 오인해 다시 큐잉하면 상태가 모순된다(실서버 QA 발견).

    KNOWN GAP (정직하게): `detect_assistant_command_action`은 `classify_subpart`(모호한
    중간 구간에서 LLM enum 1콜)보다 **좁다.** LLM은 action-intake로 분류하는데 키워드
    감지기는 놓치는 명령절이 있을 수 있고(예: "그거 정리해줘"), 그런 리프는 이 게이트를
    빠져나간다 — **구멍이 100% 닫히지는 않는다.**

    그래도 이 감지기를 쓰는 이유: (a) 대안이었던 "전면 차단 + fail-closed off"는 실질적으로
    구멍을 **100% 열어 둔** 상태였다(default off) — 좁은 감지기라도 **엄격히 낫다**;
    (b) 여기서 LLM 판정을 부르면 wave 루프마다 추가 LLM 콜이 붙고("실행 결정은 결정론
    코드가 한다"는 계약도 흐려진다); (c) 읽기 의존 리프를 잘못 막지 않으므로 정보 손실이
    없다. 같은 이유로 `_command_split_bypasses_should_decompose`도 이 감지기를 쓴다.
    감지기 커버리지를 넓히는 것은 키워드 세트를 넓히는 별도 후속 작업이다.
    """
    from orthus.agentwork import detect_assistant_command_action  # 지연 import(순환 회피)

    return detect_assistant_command_action(text) is not None


def _deps_unavailable(sub_q: SubQuestion, sub_answers_by_id: dict[UUID, SubAnswer]) -> bool:
    """True when any depends_on upstream is missing, errored, or ungrounded (§P2.6).

    A dependent leaf is skipped (not executed) when this holds — read→act must not
    run the action on a failed read.

    SVC: 이 독스트링이 이미 선언한 계약("failed read 위에서 action을 실행하지 않는다")이
    zero-answer에서는 지켜지지 않고 있었다 — zero-answer는 게이트를 통과하고
    `status="executed"`라서 `grounded`가 True다(E2 실측: 루프 안 structured 리프의
    11~18%). 그래서 **결정론 신호가 뜬 structured 상위 위에서는 명령형 의존 리프를
    실행하지 않는다.**

    범위:
    - **명령형** 의존 리프만 막는다(`is_command_leaf`, LLM 0회). 읽기 의존 리프는
      막지 않는다 — 정보 손실 없음.
    - 막힌 리프는 기존 `upstream_unavailable` 상태로 떨어진다(가시적 skip + SSE frame).
      조용히 사라지지 않는다.
    - zero-answer sub-answer **자체**는 여전히 `grounded`라 synthesize에 그대로 들어간다.
      즉 이 게이트는 사용자 답변에서 어떤 조각도 제거하지 않는다.

    `structured_zero_blocks_dependents`는 **kill switch**(default True)다 — 신규 기능
    플래그가 아니라 위 계약을 복원하는 버그 수정이며, 문제가 생기면 끌 수 있게 남겨 뒀다.
    """
    block_zero = get_settings().structured_zero_blocks_dependents
    for dep_id in sub_q.depends_on:
        up = sub_answers_by_id.get(dep_id)
        if up is None or up.error is not None or not up.grounded:
            return True
        # 감지기는 unreliable 상위가 실제로 있을 때만 부른다(불필요한 호출 회피).
        if block_zero and _structured_unreliable(up.routed) and is_command_leaf(sub_q.text):
            return True
    return False


# ── §9 synthesize ────────────────────────────────────────────────────────────


def _collect_grounded_sources(sub_answers: list[SubAnswer]) -> list[WikiSourceRef]:
    seen: set[str] = set()
    sources: list[WikiSourceRef] = []
    for sa in sub_answers:
        if not sa.grounded or sa.routed is None:
            continue
        wiki = sa.routed.wiki
        if wiki is None:
            continue
        for src in wiki.sources:
            key = f"{src.page_slug}:{src.kind}"
            if key not in seen:
                seen.add(key)
                sources.append(src)
    return sources


def synthesize(
    question: str,
    sub_answers: list[SubAnswer],
    sub_questions: list[SubQuestion],
    *,
    chat_model: ChatModel | None = None,
) -> SynthesizedBody | None:
    """LLM synthesis over grounded read sub-answers (wiki/graph/structured).

    Each grounded sub-answer contributes its body via `_extract_body` (wiki/graph →
    compiled-wiki answer; structured → columns/rows summary). action-intake
    sub-answers are not grounded, so they never enter `grounded` and stay excluded
    (§9). Returns None when no grounded leaf exists (early-exit).

    structured used to be dropped from the synthesis input, which made a knowledge+
    aggregation compound synthesize to a wiki-only body that then DENIED the
    aggregation ("…정보는 제공되지 않았습니다") even though the structured leaf had
    executed and returned rows. Feeding the structured summary in lets the count/list
    land in the combined answer.

    Latency: when exactly ONE grounded wiki/graph body remains (the command-split
    "명령+질문" shape — the command fragment is action-intake and never grounds),
    the body is returned verbatim with NO synthesis LLM call (deterministic
    passthrough; audit span carries passthrough=True). ≥2 bodies and single
    structured bodies keep the LLM path unchanged.
    """
    grounded = [sa for sa in sub_answers if sa.grounded and sa.routed is not None]
    if not grounded:
        return None

    # Build input for the synthesis LLM — every grounded read sub-answer, not just wiki.
    sq_text: dict[UUID, str] = {sq.id: sq.text for sq in sub_questions}
    parts: list[str] = []
    contributions: list[tuple[SubAnswer, str]] = []
    for i, sa in enumerate(grounded):
        body = _extract_body(sa.routed)
        if not body:
            continue
        contributions.append((sa, body))
        sub_q_text = sq_text.get(sa.sub_question_id, f"Sub-question {i + 1}")
        parts.append(f"[{i + 1}] Sub-question: {sub_q_text}\nAnswer: {body}")

    if not parts:
        return None

    # 결정론 passthrough (latency): grounded 본문이 정확히 1개인 wiki/graph 조각은 "합성"할
    # 대상이 없다 — LLM 재작성은 지연(~1 LLM 콜)만 더하고 grounded 본문에서 벗어날 위험만
    # 만든다. command-split의 대표 시나리오(명령 fragment는 action-intake로 미그라운드 +
    # 질문 leaf 1개)가 정확히 이 모양이라, 여기서 LLM 0회로 leaf 본문을 그대로 돌려준다
    # (원칙 1/7: 압축할 것이 없으면 LLM을 부르지 않는다). structured 단일 조각은 columns/rows
    # 요약을 산문으로 다듬는 가치가 있어 기존 LLM 경로를 유지한다. 2개 이상 grounded 본문은
    # 기존 합성 경로 그대로다(프롬프트 byte-identical).
    if len(contributions) == 1 and contributions[0][0].routed.mode in {"wiki", "graph"}:
        only_body = contributions[0][1]
        with audit("router.decompose.synthesize") as span:
            span.add_meta(n_grounded=len(grounded), passthrough=True)
        return SynthesizedBody(
            answer=only_body,
            sources=_collect_grounded_sources(grounded),
            warnings=[f"sub_answer_error:{sa.error}" for sa in sub_answers if sa.error],
        )

    sub_answers_text = "\n\n".join(parts)
    user_prompt = (
        f"Original compound question: {question}\n\n"
        f"Sub-answers:\n{sub_answers_text}\n\n"
        "Synthesized answer:"
    )

    chat = chat_model or get_chat_model_for(TASK_SYNTHESIZE)
    with audit("router.decompose.synthesize") as span:
        try:
            answer_text = chat.complete(_SYNTHESIZE_SYSTEM, user_prompt)
        except Exception as exc:
            span.add_meta(error=str(exc))
            return None
        span.add_meta(n_grounded=len(grounded))

    sources = _collect_grounded_sources(grounded)
    warnings: list[str] = []
    for sa in sub_answers:
        if sa.error:
            warnings.append(f"sub_answer_error:{sa.error}")

    return SynthesizedBody(answer=answer_text, sources=sources, warnings=warnings)


# ── §9 fan_out ───────────────────────────────────────────────────────────────


def _run_leaf(
    sub_q: SubQuestion,
    *,
    user_id: UUID,
    scope: str,
    project: str | None,
    chat_model: ChatModel | None,
    context_wiki_slug: str | None,
    actor_role: str,
    allow_agentic_in_leaf: bool,
    stream_key: str | None,
    leaf_index: int,
    upstream_context: str | None = None,
    context_mail_id: str | None = None,
    chat_session_id: UUID | None = None,
    command_split: bool = False,
) -> SubAnswer:
    """Execute one sub-question leaf and return a SubAnswer.

    Exceptions are caught and surfaced as error="leaf_error" so a single bad
    leaf never fails the entire fan-out (불변식 R3). PII stays off the error
    string (only goes to audit span). `upstream_context` (Phase 2 §P2.4) is the
    redacted read→act context from upstream sub-answers, injected into the leaf.
    `context_mail_id` (MA.3b) is the request-scoped mail this `/ask` is about; an
    email_send action sub-part reuses the mail's existing P7.1 reply draft.
    """
    leaf, action_family = classify_subpart(sub_q.text, command_split=command_split)
    has_ctx = bool(upstream_context)

    if leaf == "action-intake":
        # MA.4: action-intake → P3 policy gate only, no auto-execute from here
        from orthus.router import tools as _tools

        try:
            agent_work_id = None
            work_item = None  # S4: enrich-source AgentWork item (queue or reply-reuse)
            # MA.3b (D path): a reply to a specific mail (context_mail_id present +
            # email_send family) reuses the P7.1 reply draft already created at ingest,
            # instead of queuing a fresh intake. The lookup is owner-scoped/fail-closed.
            # Falls back to queue_agent_work when no such draft exists (P7.1 flag off /
            # mail not auto-drafted).
            # S5 #2: under a command-split fan-out, a self-composed email fragment must
            # NOT reuse an unrelated request-scoped mail's P7.1 reply draft (that mail is
            # incidental context, not the fragment's target). Skip reply-reuse so the
            # fragment queues its own held intake instead.
            if context_mail_id and action_family == "email_send" and not command_split:
                from orthus.agentwork.service import find_reply_draft_for_mail

                existing = find_reply_draft_for_mail(user_id, context_mail_id)
                if existing is not None:
                    agent_work_id = existing.work_id
                    work_item = existing
                    with audit("router.decompose.reply_reuse") as s:
                        s.add_meta(
                            sub_question_id=str(sub_q.id),
                            work_id=str(existing.work_id),
                        )
            if agent_work_id is None:
                item = _tools.queue_agent_work(
                    user_id,
                    sub_q.text,
                    actor_role=actor_role,
                    action_family=action_family,
                    context_wiki_slug=context_wiki_slug,
                    upstream_context=upstream_context,
                    # command-split (S2): link the fragment to the chat, isolate its idempotency
                    # by position, and hold its auto-execution for review (BLOCKER A/C). On a
                    # pure-question decompose (command_split=False) these are inert.
                    chat_session_id=chat_session_id,
                    leaf_index=leaf_index,
                    command_split_origin=command_split,
                    intake_source="decompose_fanout" if command_split else None,
                )
                agent_work_id = item.work_id if item else None
                work_item = item
        except Exception as exc:
            with audit("router.decompose.leaf.error") as s:
                s.add_meta(sub_question_id=str(sub_q.id), leaf=leaf, error=type(exc).__name__)
            return SubAnswer(sub_question_id=sub_q.id, error="leaf_error")

        # S4: back-inject chip fields from the queued/reused item so the FE renders an action
        # chip without a second fetch. item-less (queue suppressed → None) leaves them unset.
        # BADGE SoR is `state` (execution truth); recipient covers both payload shapes (Z4).
        sa = SubAnswer(
            sub_question_id=sub_q.id,
            grounded=False,
            agent_work_id=agent_work_id,
            context_injected=has_ctx,
        )
        # Gate the chip enrichment on command_split — NOT on `work_item is not None`. Only the
        # command-split FE consumes these fields; the pre-existing MA.4 decompose-action path
        # (command_split=False) also produces a non-None work_item, so enriching there would
        # change its serialized SubAnswer wire shape (flag-off byte-identical, review B1/C2).
        if command_split and work_item is not None:
            from orthus.agentwork.service import extract_command_recipient

            sa = sa.model_copy(
                update={
                    "state": work_item.state,
                    "policy_outcome": work_item.policy_outcome,
                    "reason_codes": list(work_item.reason_codes or []),
                    "title": work_item.title,
                    "recipient": extract_command_recipient(work_item.payload),
                }
            )
    else:
        # Knowledge/data leaf: call answer() with allow_decompose=False (depth 1 fixed)
        from orthus.router import answer as _answer

        # §P2.4: prepend upstream context to the question so the leaf can use it.
        leaf_question = (
            f"[관련 맥락]\n{upstream_context}\n\n[질문]\n{sub_q.text}" if has_ctx else sub_q.text
        )

        try:
            routed = _answer(
                user_id,
                leaf_question,
                scope=scope,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
                allow_decompose=False,
                allow_cache=False,  # leaf sub-answers are never cached (불변식 22 / one-off context)
                allow_agentic=allow_agentic_in_leaf,
                learn=False,
                record_gaps=False,
            )
        except Exception as exc:
            with audit("router.decompose.leaf.error") as s:
                s.add_meta(sub_question_id=str(sub_q.id), leaf=leaf, error=type(exc).__name__)
            return SubAnswer(sub_question_id=sub_q.id, error="leaf_error")

        # Determine grounding: wiki/graph modes with a non-empty body that the deterministic
        # gap detector did not flag as answerless (_UNGROUNDED_GAP_REASONS above).
        #
        # `"근거 없음"` alone is NOT a sufficient test: it is the no_hits sentinel
        # (wiki/qa.py::no_hits_answer), emitted ONLY when retrieval returned 0 hits. The far
        # more common failure is that 5 unrelated hits come back and the model refuses in
        # prose ("제공된 컨텍스트에는 …정보가 포함되어 있지 않습니다") — no sentinel, so those
        # refusals used to pass as grounded and were fed to synthesize() as evidence.
        # orthus already owns a deterministic refusal detector for exactly this
        # (wiki/gap.py::detect_gap → insufficient_grounding, 0 LLM calls); the decompose path
        # was the one consumer ignoring it. Reading it here means an answerless leaf is
        # excluded from synthesis input/citations and cannot satisfy a read→act dependency
        # (_deps_unavailable) — the action must not run on a failed read.
        grounded = False
        gap = None
        if routed.mode in {"wiki", "graph"} and routed.wiki is not None:
            wiki_answer = routed.wiki.answer or ""
            gap = routed.wiki.gap
            answerless = gap is not None and gap.reason in _UNGROUNDED_GAP_REASONS
            grounded = bool(wiki_answer) and "근거 없음" not in wiki_answer and not answerless
        elif routed.mode == "structured" and routed.structured is not None:
            grounded = routed.structured.status != "rejected"

        sa = SubAnswer(
            sub_question_id=sub_q.id,
            routed=routed,
            grounded=grounded,
            gap=gap,
            context_injected=has_ctx,
        )

    # SSE: publish sub_answer_ready frame from worker thread (불변식 15)
    if stream_key is not None:
        frame: dict = {
            "type": "sub_answer_ready",
            "index": leaf_index,
            "sub_question": sub_q.text,
            "grounded": sa.grounded,
        }
        if sa.agent_work_id:
            frame["agent_work_id"] = str(sa.agent_work_id)
        if sa.routed is not None:
            frame["routed"] = sa.routed.model_dump(mode="json")
        agent_stream.publish_threadsafe(stream_key, frame)

    return sa


def fan_out(
    sub_questions: list[SubQuestion],
    *,
    user_id: UUID,
    scope: str,
    project: str | None,
    chat_model: ChatModel | None,
    context_wiki_slug: str | None,
    actor_role: str,
    allow_agentic_in_leaf: bool,
    stream_key: str | None,
    max_concurrency: int,
    leaf_timeout_sec: int,
    context_from: dict[UUID, str] | None = None,
    index_of: dict[UUID, int] | None = None,
    context_mail_id: str | None = None,
    chat_session_id: UUID | None = None,
    command_split: bool = False,
) -> list[SubAnswer]:
    """Parallel fan-out over sub-questions using ThreadPoolExecutor.

    Each leaf runs in a copy of the current contextvars context so correlation_id
    and other ContextVars propagate correctly without cross-reset (불변식 15).
    Pool size is capped at max_concurrency to bound DB connection usage (§16).
    Individual leaf timeout surfaces as error="timeout"; never fails the whole call.

    Phase 2: `context_from` maps a sub-question id to its redacted upstream context
    (§P2.4); `index_of` maps a sub-question id to its stable SSE leaf index so a
    later-wave leaf keeps its original position (default: enumerate, Phase 1).
    """
    sub_answers: list[SubAnswer] = [SubAnswer(sub_question_id=sq.id) for sq in sub_questions]
    futures = {}

    # Not a `with` block: on the outer timeout below we want to return partial results
    # immediately without shutdown(wait=True) blocking on a hung leaf. The finally cancels
    # queued futures and detaches (wait=False) — a straggler thread finishes in the background,
    # its result discarded.
    executor = ThreadPoolExecutor(max_workers=max_concurrency)
    try:
        for i, sq in enumerate(sub_questions):
            ctx = copy_context()
            leaf_index = index_of[sq.id] if index_of is not None else i
            upstream_context = context_from.get(sq.id) if context_from is not None else None
            future = executor.submit(
                ctx.run,
                _run_leaf,
                sq,
                user_id=user_id,
                scope=scope,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
                actor_role=actor_role,
                allow_agentic_in_leaf=allow_agentic_in_leaf,
                stream_key=stream_key,
                leaf_index=leaf_index,
                upstream_context=upstream_context,
                context_mail_id=context_mail_id,
                chat_session_id=chat_session_id,
                command_split=command_split,
            )
            futures[future] = i

        completed: set[int] = set()
        try:
            for future in as_completed(futures, timeout=leaf_timeout_sec * len(sub_questions)):
                idx = futures[future]
                completed.add(idx)
                try:
                    sub_answers[idx] = future.result(timeout=0)  # result is already ready
                except TimeoutError:
                    sub_answers[idx] = SubAnswer(
                        sub_question_id=sub_questions[idx].id, error="timeout"
                    )
                except Exception as exc:
                    log.debug("decompose leaf %d error: %s", idx, exc)
                    sub_answers[idx] = SubAnswer(
                        sub_question_id=sub_questions[idx].id, error="leaf_error"
                    )
        except TimeoutError:
            # The overall as_completed budget elapsed before every leaf finished. Degrade to
            # partial results — mark the unfinished leaves as timeout — instead of letting the
            # TimeoutError propagate and 500 the whole decompose (synthesize tolerates per-leaf
            # errors). A slow/hung leaf must not take the entire orchestrate down.
            for i, sq in enumerate(sub_questions):
                if i not in completed:
                    sub_answers[i] = SubAnswer(sub_question_id=sq.id, error="timeout")
            log.debug(
                "decompose fan_out outer timeout: %d/%d leaves done",
                len(completed),
                len(sub_questions),
            )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    return sub_answers


# ── §P2.3 wave loop (Phase 2, depends_enabled) ───────────────────────────────


def _emit_skipped_frame(stream_key: str | None, *, index: int, text: str) -> None:
    if stream_key is None:
        return
    agent_stream.publish_threadsafe(
        stream_key,
        {
            "type": "sub_answer_ready",
            "index": index,
            "sub_question": text,
            "grounded": False,
            "error": "upstream_unavailable",
        },
    )


def _run_waves(
    sub_questions: list[SubQuestion],
    *,
    user_id: UUID,
    scope: str,
    project: str | None,
    chat_model: ChatModel | None,
    context_wiki_slug: str | None,
    actor_role: str,
    allow_agentic_in_leaf: bool,
    stream_key: str | None,
    max_concurrency: int,
    leaf_timeout_sec: int,
    max_depth: int,
    max_context_chars: int | None = None,
    context_mail_id: str | None = None,
    chat_session_id: UUID | None = None,
    command_split: bool = False,
) -> tuple[list[SubAnswer], list[str]]:
    """Execute sub-questions in dependency waves with context_from injection (§P2.3).

    Returns (sub_answers ordered like sub_questions, extra warnings). The wave
    loop is driven by the deterministic coordinate() (불변식 17). Depth over
    max_depth degrades to a flat Phase-1-style fan_out (deps ignored, no context).
    Upstream failure skips dependent leaves as error="upstream_unavailable" (§P2.6).
    """
    index_of = {sq.id: i for i, sq in enumerate(sub_questions)}
    sq_by_id = {sq.id: sq for sq in sub_questions}
    warnings: list[str] = []

    def _fan(targets, context_from):
        return fan_out(
            targets,
            user_id=user_id,
            scope=scope,
            project=project,
            chat_model=chat_model,
            context_wiki_slug=context_wiki_slug,
            actor_role=actor_role,
            allow_agentic_in_leaf=allow_agentic_in_leaf,
            stream_key=stream_key,
            max_concurrency=max_concurrency,
            leaf_timeout_sec=leaf_timeout_sec,
            context_from=context_from,
            index_of=index_of,
            context_mail_id=context_mail_id,
            chat_session_id=chat_session_id,
            command_split=command_split,
        )

    levels = topo_levels(sub_questions)
    if len(levels) > max_depth:
        # §P2.3: depth exceeded → flat degrade (deps ignored, no context injection)
        warnings.append("depth_exceeded")
        return _fan(sub_questions, None), warnings

    answers_by_id: dict[UUID, SubAnswer] = {}
    for wave in levels:
        runnable: list[SubQuestion] = []
        context_from: dict[UUID, str] = {}
        for sq in wave:
            if _deps_unavailable(sq, answers_by_id):
                # §P2.6: upstream failed/ungrounded → skip, do not execute the action
                answers_by_id[sq.id] = SubAnswer(
                    sub_question_id=sq.id, error="upstream_unavailable"
                )
                _emit_skipped_frame(stream_key, index=index_of[sq.id], text=sq.text)
                continue
            try:
                ctx = resolve_context(
                    sq, answers_by_id, sq_by_id, max_context_chars=max_context_chars
                )
            except AssertionError:
                # R15 scope mismatch — isolate as leaf error, never 500
                answers_by_id[sq.id] = SubAnswer(sub_question_id=sq.id, error="leaf_error")
                continue
            runnable.append(sq)
            if ctx:
                context_from[sq.id] = ctx

        for sa in _fan(runnable, context_from):
            answers_by_id[sa.sub_question_id] = sa

        coord = coordinate(list(answers_by_id.values()), sub_questions, depends_enabled=True)
        if coord.done:
            break

    ordered = [
        answers_by_id.get(sq.id) or SubAnswer(sub_question_id=sq.id, error="leaf_error")
        for sq in sub_questions
    ]
    return ordered, warnings


# ── MA.6c envelope enrichment ────────────────────────────────────────────────


def _enrich_parts(
    sub_answers: list[SubAnswer], sub_questions: list[SubQuestion]
) -> list[SubAnswer]:
    """Attach sub-question text + positional depends_on to each SubAnswer (MA.6c).

    `depends_on` on the wire is positional indices into `parts` (FE-friendly), not
    UUIDs. Leaf execution leaves these unset; the envelope fills them so the FE can
    show which part fed which (read→act) and the action leaf's own question text.
    """
    sq_by_id = {sq.id: sq for sq in sub_questions}
    index_of = {sq.id: i for i, sq in enumerate(sub_questions)}
    out: list[SubAnswer] = []
    for sa in sub_answers:
        sq = sq_by_id.get(sa.sub_question_id)
        if sq is None:
            out.append(sa)
            continue
        dep_idx = [index_of[d] for d in sq.depends_on if d in index_of]
        out.append(sa.model_copy(update={"text": sq.text, "depends_on": dep_idx}))
    return out


# S3: shown on the command-split fall-through (LLM split returned < 2 parts but the whole
# input was command-shaped, so it was queued as one command instead of silently lost, F3).
_CMD_FALLTHROUGH_WARNING = "decompose_command_lost_degrade"


def _maybe_queue_command_fallthrough(
    user_id: UUID,
    question: str,
    *,
    actor_role: str | None,
    context_wiki_slug: str | None,
    chat_session_id: UUID | None,
    command_split: bool,
) -> bool:
    """command-split split-failure fallback (S3, finding F3): when the LLM split returns < 2
    parts (or exceeds the leaf budget) but the whole input is command-shaped, queue it as ONE
    command so it is not silently lost. Uses queue_agent_work — respecting the
    _suppress_action_intake guard (불변식 29) — and is best-effort: a queue failure must never
    break the search answer the caller still returns. leaf_index=None makes the digest match the
    535 single-command path (no external-write suffix; command_split_origin still holds it)."""
    if not command_split:
        return False
    from orthus.agentwork.service import detect_assistant_command_action

    if detect_assistant_command_action(question) is None:
        return False
    from orthus.router import tools as _tools

    try:
        item = _tools.queue_agent_work(
            user_id,
            question,
            # Pass the caller's actor_role as-is (review Z5) — same as the normal fan-out leaf.
            # A None/low-privilege role must NOT be promoted to "owner": the policy gate then
            # denies auto_execute (fail-closed → draft_for_review/request_more_data), and
            # command_split_origin=True holds any auto_execute anyway.
            actor_role=actor_role,
            context_wiki_slug=context_wiki_slug,
            chat_session_id=chat_session_id,
            leaf_index=None,
            command_split_origin=True,
            intake_source="decompose_fanout",
        )
        return item is not None
    except Exception:
        return False


# ── answer_or_decompose ──────────────────────────────────────────────────────


def answer_or_decompose(
    user_id: UUID,
    question: str,
    *,
    scope: str,
    project: str | None,
    chat_model: ChatModel | None,
    context_wiki_slug: str | None,
    history,
    stream_id: str | None,
    actor_role: str | None = None,
    allow_agentic_in_leaf: bool,
    learn: bool,
    record_gaps: bool,
    context_mail_id: str | None = None,
    command_split: bool = False,
    chat_session_id: UUID | None = None,
) -> RoutedAnswer:
    """Entry point called from answer() when decompose is enabled.

    1. should_decompose() → False → fall through to single answer()
    2. split_question() → [] → fall through to single answer()
    3. fan_out() → coordinate() → synthesize() → RoutedAnswer(mode="decomposed")

    `command_split` (BLOCKER F/G, command-split feature): when True the caller has already
    determined — deterministically — that this is a compound "명령+질문" input that must be
    split so command fragments route to the P3 gate. In that mode the LLM should_decompose
    gate is bypassed (we go straight to split_question). `chat_session_id` is threaded (S2)
    through fan_out/_run_waves → _run_leaf → queue_agent_work so a command-split fragment's
    queued item is linked to this chat (only persisted on the command_split_origin path). Both
    default off so the pre-command-split path is unchanged.

    The decomposed path emits SSE frames to the orchestrator's live stream channel
    (GET /agent-work/chats/stream/{stream_id}) via publish_threadsafe() (불변식 15).
    The orchestrator (POST /agent-work/chats/{id}/orchestrate) is the only caller that
    sets a stream_id; /ask is search-only and never enters this path.
    """
    from orthus.router import answer as _answer  # avoid circular import at module level

    settings = get_settings()
    k = settings.ask_decompose_max_parts
    max_concurrency = settings.ask_decompose_max_concurrency
    leaf_timeout = settings.ask_decompose_leaf_timeout_sec
    stream_key = f"{user_id}:{stream_id}" if stream_id else None

    with audit("router.decompose") as span:
        # §4: should_decompose check. command_split bypasses the LLM gate ONLY when the input
        # actually carries a command clause (see _command_split_bypasses_should_decompose).
        bypass_should_decompose = _command_split_bypasses_should_decompose(question, command_split)
        if not bypass_should_decompose and not should_decompose(question, chat_model=chat_model):
            span.add_meta(decomposed=False, reason="prefilter_or_no")
            return _answer(
                user_id,
                question,
                scope=scope,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
                history=history,
                stream_id=stream_id,
                allow_agentic=allow_agentic_in_leaf,
                allow_decompose=False,  # depth 1 fixed
                allow_cache=False,  # fall-through; the outer answer() wrapper owns caching
                learn=learn,
                record_gaps=record_gaps,
            )

        # §5 / §P2.2: split_question — Phase 2 emits depends_on, Phase 1 a flat list.
        depends_enabled = settings.ask_decompose_depends_enabled
        # §P3B.3.1: depth > 2 only takes effect with the full-DAG flag on; otherwise
        # the wave loop caps at 2 and flat-degrades deeper chains (Phase 2 behavior).
        full_dag_enabled = settings.ask_decompose_full_dag_enabled
        max_depth = (
            settings.ask_decompose_max_depth
            if full_dag_enabled
            else min(settings.ask_decompose_max_depth, 2)
        )
        max_total_leaves = settings.ask_decompose_max_total_leaves

        sub_questions: list[SubQuestion] | None = None
        if depends_enabled:
            sub_pairs = split_question_with_deps(
                question, k=k, chat_model=chat_model, command_split=command_split
            )
            if len(sub_pairs) >= 2:
                sub_questions = build_sub_questions(sub_pairs, scope=scope)
        else:
            sub_texts = split_question(
                question, k=k, chat_model=chat_model, command_split=command_split
            )
            if len(sub_texts) >= 2:
                sub_questions = [SubQuestion(id=uuid4(), text=t, scope=scope) for t in sub_texts]

        if sub_questions is None:
            # F3: split failed. If command_split and the whole input is a command, queue it as
            # one command (not silently lost) and still return the search answer + a warning.
            queued = _maybe_queue_command_fallthrough(
                user_id,
                question,
                actor_role=actor_role,
                context_wiki_slug=context_wiki_slug,
                chat_session_id=chat_session_id,
                command_split=command_split,
            )
            span.add_meta(decomposed=False, reason="split_empty", command_queued=queued)
            routed = _answer(
                user_id,
                question,
                scope=scope,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
                history=history,
                stream_id=stream_id,
                allow_agentic=allow_agentic_in_leaf,
                allow_decompose=False,  # depth 1 fixed
                allow_cache=False,  # fall-through; the outer answer() wrapper owns caching
                learn=learn,
                record_gaps=record_gaps,
            )
            if queued:
                routed = routed.model_copy(
                    update={"warnings": [*routed.warnings, _CMD_FALLTHROUGH_WARNING]}
                )
            return routed

        # §P3B.3.1 / R18: depth×width cost guard. If the split produced more parts
        # than the cumulative-leaf budget, decompose is too expensive — degrade to a
        # single answer() rather than fan out (peak connections are already bounded by
        # max_concurrency; this bounds total work). Gated behind the full-DAG flag so
        # that with MA.8b off, Phase 1/2 stay byte-identical for ANY config, not just
        # the default K(5) ≤ 12 (불변식 32).
        if full_dag_enabled and len(sub_questions) > max_total_leaves:
            # F3: same command-preserving fall-through as split_empty (the command must not be
            # lost just because the split exceeded the leaf budget).
            queued = _maybe_queue_command_fallthrough(
                user_id,
                question,
                actor_role=actor_role,
                context_wiki_slug=context_wiki_slug,
                chat_session_id=chat_session_id,
                command_split=command_split,
            )
            span.add_meta(
                decomposed=False,
                reason="total_leaves_exceeded",
                n_parts=len(sub_questions),
                command_queued=queued,
            )
            routed = _answer(
                user_id,
                question,
                scope=scope,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
                history=history,
                stream_id=stream_id,
                allow_agentic=allow_agentic_in_leaf,
                allow_decompose=False,  # depth 1 fixed
                allow_cache=False,  # fall-through; the outer answer() wrapper owns caching
                learn=learn,
                record_gaps=record_gaps,
            )
            if queued:
                routed = routed.model_copy(
                    update={"warnings": [*routed.warnings, _CMD_FALLTHROUGH_WARNING]}
                )
            return routed

        span.add_meta(decomposed=True, n_parts=len(sub_questions), depends=depends_enabled)

        # Tier i: announce the fan-out size once so the live bubble can render
        # "하위 질문 k/N 완료" against the sub_answer_ready frames. Best-effort
        # (publish_threadsafe never raises); leaves themselves emit no phase frames.
        if stream_key is not None:
            agent_stream.publish_threadsafe(
                stream_key,
                {"type": "phase", "stage": "fanout", "total": len(sub_questions)},
            )

        # Execute: Phase 2 dependency wave loop, or Phase 1 flat fan_out.
        wave_warnings: list[str] = []
        if depends_enabled:
            sub_answers, wave_warnings = _run_waves(
                sub_questions,
                user_id=user_id,
                scope=scope,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
                actor_role=actor_role,
                allow_agentic_in_leaf=allow_agentic_in_leaf,
                stream_key=stream_key,
                max_concurrency=max_concurrency,
                leaf_timeout_sec=leaf_timeout,
                max_depth=max_depth,
                # §P3B.3.1 / R19: bound the depth-chain context only under full DAG;
                # None when off keeps Phase 2 byte-identical (불변식 32).
                max_context_chars=_MAX_CONTEXT_CHARS if full_dag_enabled else None,
                context_mail_id=context_mail_id,
                chat_session_id=chat_session_id,
                command_split=command_split,
            )
        else:
            # §9: fan_out
            sub_answers = fan_out(
                sub_questions,
                user_id=user_id,
                scope=scope,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
                actor_role=actor_role,
                allow_agentic_in_leaf=allow_agentic_in_leaf,
                stream_key=stream_key,
                max_concurrency=max_concurrency,
                leaf_timeout_sec=leaf_timeout,
                context_mail_id=context_mail_id,
                chat_session_id=chat_session_id,
                command_split=command_split,
            )
            # §2: coordinate — Phase 1 no-op, always done=True (불변식 21)
            coord = coordinate(sub_answers)
            assert coord.done, "coordinate() must return done=True when depends off"

        # MA.6c: enrich parts with sub-question text + positional depends_on so the
        # FE can render read→act relationships without re-deriving them.
        sub_answers = _enrich_parts(sub_answers, sub_questions)

        # §9: synthesize
        synthesized = synthesize(question, sub_answers, sub_questions, chat_model=chat_model)

        # SSE: synthesized + done frames
        if stream_key is not None:
            synth_frame: dict = {"type": "synthesized", "warnings": []}
            if synthesized is not None:
                synth_frame["synthesized_body"] = synthesized.model_dump(mode="json")
                synth_frame["warnings"] = synthesized.warnings
            agent_stream.publish_threadsafe(stream_key, synth_frame)
            agent_stream.publish_threadsafe(stream_key, {"type": "decompose_done"})

        all_warnings: list[str] = list(wave_warnings)
        for sa in sub_answers:
            if sa.error:
                all_warnings.append(f"sub_answer_error:{sa.error}")

        return RoutedAnswer(
            question=question,
            mode="decomposed",
            synthesized_body=synthesized,
            parts=sub_answers,
            warnings=all_warnings,
            stream_id=stream_id,
        )
