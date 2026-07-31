"""Auto-router: classify an NL question to a backend (docs/architecture-v2.md §1).

`structured` = counts / lists / filters / aggregations over company DBs
(티켓 / KPI / 팀원 / 계획); `wiki` = explanatory / knowledge / how / why questions.

The classifier is the chat model in json_only mode returning {"route": ...}. Parse
is robust: any unparseable / unexpected output falls back to "wiki" (the safe,
read-grounded default that never executes SQL)."""

from __future__ import annotations

import json
from typing import Literal, NamedTuple

from orthus.audit import audit
from orthus.models.base import ChatModel
from orthus.models.orchestration import TASK_INTENT, TASK_ROUTING, get_chat_model_for
from orthus.settings import get_settings

Route = Literal["structured", "wiki", "graph"]

# Command families the LLM intent enum may emit (agent_task is excluded — it is detected
# deterministically by an explicit delegation prefix, never inferred). Kept in sync with
# the keyword families in orthus/agentwork/service.py::_detect_command_family.
COMMAND_FAMILIES: frozenset[str] = frozenset(
    {
        "connector_sync",
        "email_send",
        "document_draft",
        "personal_board_cleanup",
        "central_wiki_task_cleanup",
    }
)

_SYSTEM = (
    "You are a query router for a company knowledge assistant. Decide which backend "
    "should answer the user's question:\n"
    "- 'structured': counts, lists, filters, or aggregations over company databases "
    "such as 티켓(tickets), KPI, 팀원(team members), 계획(plans). Anything asking "
    "'how many', '개수', '목록', '상태별', '담당자' belongs here.\n"
    "- 'graph': questions about the RELATIONSHIP between two things, what conflicts "
    "with a claim, or where a fact/evidence came from — e.g. 'A와 B는 무슨 관계', "
    "'어떻게 연결돼 있어', '무엇과 충돌하나', '근거가 어디서 나왔나'.\n"
    "- 'wiki': explanatory or knowledge questions — how / why / what-is / policy / "
    "definitions answered from written wiki pages.\n"
    'Respond with JSON only: {"route": "structured"}, {"route": "graph"}, or '
    '{"route": "wiki"}.'
)

# Relation/conflict/provenance signals → graph candidate (kg-impl §7.1). Matched on the
# space-collapsed lowercase question, ahead of _WIKI_TERMS so "어떻게 연결" / "무슨 관계" /
# "충돌하는 주장" reach the graph branch instead of falling to wiki. Terms are kept SPECIFIC
# and biased toward MULTI-WORD relational/conflict anchors. Bare generic fragments ("관계가"/
# "관계는"/"연결돼"/"이어져") over-capture ordinary explanatory wiki questions ("두 정책의
# 관계가 어떻게 정의되나") — pulling them into the graph branch costs an LLM bind + Neo4j probe
# before demoting back to wiki — so they are deliberately excluded. Explanatory phrasings that
# DO match a specific anchor ("이 모듈들이 어떻게 연결돼 있는지 설명해줘" matches "어떻게연결") may
# enter the graph candidate; that is acceptable — they demote safely to wiki on a bind/gate
# miss (bounded fail-open, the recall is worth the probe). Genuine relation/conflict/provenance
# questions also reach graph via the LLM `graph` enum when no rule fires. Entries are compact
# (no-space) only, since matching collapses spaces. graph here is only a CANDIDATE —
# router.answer confirms node/scope/kg_available + binding and demotes to wiki on any miss.
_GRAPH_TERMS = (
    "무슨관계",
    "어떤관계",
    "관계야",
    "어떻게연결",
    "근거가어디",
    "근거는어디",
    "어디서나온",
    "어디서나왔",
    # Conflict/contradiction anchors. ONLY the multi-word "…주장" forms are kept: bare
    # particle fragments ("과충돌"/"와상충"/"과모순") and the bare stem "모순되는" over-capture
    # ordinary wiki asks ("기존 동작과 충돌하지 않는지 설명해줘", "앞 문단과 모순되는 것 같은데
    # 정리해줘") and — given the v1 limitation that conflict subjects only resolve to claim
    # machine-slugs — would route a non-functional NL path that just pays a bind+probe before
    # demoting. NL conflict questions without "…주장" still reach graph via the LLM enum when
    # no wiki rule fires.
    "충돌하는주장",
    "상충하는주장",
    "모순되는주장",
    # K9.3a entity anchors — "X를 언급/공유한 페이지" 류. 한 named 개체를 중심으로 "그 개체를
    # 함께 가진 페이지들"을 묻는 cross-page 발견 질문(entity intent). SPECIFIC 다단어만 둔다:
    # 단독 "언급"/"엮인"은 일반 wiki 질문("이 정책을 언급한 부분 설명해줘")을 과포획하므로 제외하고,
    # "언급한/된 페이지·곳", "어디서/누가 언급", "엮인 페이지"만 앵커한다. graph는 CANDIDATE일
    # 뿐 — named 개체가 company kg_entities에 없으면 bind가 None을 내 wiki로 안전 demote한다.
    "언급한페이지",
    "언급된페이지",
    "언급한곳",
    "언급된곳",
    "어디서언급",
    "누가언급",
    "엮인페이지",
    # Entity-mention 어미 앵커 확장 — "X가 어디에 나오나/다뤄지나" 류. 그래프 사각지대 실측
    # (docs/routing-graph-follow-up.md §8)에서 entity-mention 골든 61문항이 위 K9.3a 앵커와
    # 하나도 매칭되지 않아 LLM enum에 의존했고, 국내 모델 배정 시 recall이 5% 미만으로 잘렸다.
    # 골든의 실제 표현 3개 군집(어디어디에 나오 / 다뤄지는 데가 어디 / 어떤 페이지들·맥락에서
    # 다뤄지)을 결정론 앵커로 고정한다 — recall이 모델 무관이 된다(entity 골든 61/61, holdout
    # 330 leak 0 실측). SPECIFIC 다단어 원칙 유지: 단독 "나오"/"다뤄"/"어디"는 과포획이라 금지.
    # 각 앵커는 골든에 실제 hit이 있어야 채택한다(골든 기여 0인 후보는 편익 없이 과포획 표면만
    # 넓히므로 제외): "나오는페이지"/"등장하는페이지"/"페이지에서다뤄"/"맥락에서다뤄"는 일반 wiki
    # 질문("결과가 잘 나오는 페이지 설정", "온보딩 페이지에서 다뤄지는 내용 요약")을 과포획해 제외,
    # "나온데가어디"/"나온데들이어디"는 골든·holdout 양쪽 hit 0(측정 근거 없음) + provenance는 이미
    # 위 "어디서나온" 앵커가 커버하므로 제외했다.
    # 군집 A/B는 장소 의문사 "+어디" 결합형이라 연결어미 "-는데"("이 정책이 다뤄지는데 설명해줘")를
    # 안 잡는다. 군집 C(페이지/맥락 계열)는 "+어디"가 없어 "이 페이지들에서 다뤄진 내용 정리해줘"
    # 류를 graph로 끌 수 있는데(수용하는 과포획), 페이지 열거 의미라 준-정답이고 bind 미스 시 wiki로
    # 안전 demote한다. 이 동작은 아래 회귀 테스트로 고정한다.
    "어디어디에나오",
    "다뤄지는데가어디",
    "다뤄지는데들이어디",
    "페이지들에서다뤄",
    "페이지나맥락에서",
    "페이지또는맥락에서",
)

_STRUCTURED_TERMS = (
    "목록",
    "리스트",
    "건수",
    "개수",
    "몇개",
    "몇 개",
    "상태별",
    "담당자별",
    "전화번호",
    "연락처",
    "이메일",
    "메일",
)

# 수량 표현(routing holdout C2, docs/routing-holdout-plan.md §13). 기존 규칙이 다 지나간
# 뒤에만 적용하는 **후순위 신호**다 — 기존 규칙(특히 wiki 용어)이 먼저 결정하게 두고, 그래도
# 미결정인 집계 질문만 여기서 structured로 잡는다. "몇 개/개수"는 이미 위에서 잡히지만
# "얼마나 되나요"류는 안 잡혀 LLM으로 떨어졌고, LLM은 이런 집계 리프의 절반을 wiki로
# 오라우팅했다. 측정한 계층 순서(기존 규칙 → 수량 → wiki 기본값)를 그대로 재현한다.
_QUANTITY_TERMS = (
    "얼마나되",
    "얼마나있",
    "얼마나많",
    "얼마나쌓",
    "총몇",
    "몇건",
    "몇명",
    "몇번",
    "수는얼마",
    "어느정도인가",
    "어느정도나",
)
_STRUCTURED_ENTITIES = ("직원", "팀원", "멤버", "구성원", "티켓", "kpi")
_WIKI_TERMS = (
    "뭐야",
    "무엇",
    "무슨내용",
    "무슨 내용",
    "어떤내용",
    "어떤 내용",
    "왜",
    "어떻게",
    "설명",
    "요약",
    "정리",
)


# --- C2 사각지대 정책 (routing holdout §13) — classify()와 classify_intent()가 공유한다.
# 두 함수 모두 규칙 사각지대에서 같은 read-route 결정을 내려야 하므로, 정책은 이 두 헬퍼에만
# 존재한다(한쪽에만 넣으면 갈라진다 — code review #2).


def _quantity_route(question: str) -> Route | None:
    """후순위 수량 신호 — 기존 규칙이 미결정일 때만. "얼마나 되나요"류 집계 질문을 structured로.

    "몇 개/개수"는 `_rule_based_route`가 잡지만 "얼마나 되나요"류는 안 잡혀 LLM으로 떨어졌고,
    LLM은 이런 집계 리프의 절반을 wiki로 오라우팅했다(§13). 규칙 다음, LLM 앞에 둔다.
    """
    compact = question.lower().replace(" ", "")
    if any(term in compact for term in _QUANTITY_TERMS):
        return "structured"
    return None


def _apply_wiki_default(route: Route) -> Route:
    """규칙 사각지대에서 LLM의 structured/wiki 판정을 wiki로 강제한다(graph는 예외).

    홀드아웃(§13): LLM 라우터는 wiki 질문의 절반을 structured로 오라우팅해 "무조건 wiki"
    상수보다도 낮았다(사각지대 정답 분포 222:68 wiki 편중). wiki 오라우팅은 "정보 없음"으로
    눈에 보이게 실패하는 반면 structured 오라우팅은 조용한 빈 답을 낸다.
      단 graph는 살린다: 홀드아웃은 structured vs wiki만 쟀고 graph 평면을 미측정했으므로,
      그 결론이 K8 conflict/관계 질문의 graph 라우팅을 죽이면 안 된다. 규칙 앵커 없는 NL
      conflict/관계 질문은 LLM `graph` enum으로만 graph에 도달하며(_GRAPH_TERMS 주석 참조),
      graph는 어차피 CANDIDATE라 bind/gate 미스 시 wiki로 안전하게 demote한다.
    """
    if get_settings().routing_wiki_default and route != "graph":
        return "wiki"
    return route


def classify(question: str, *, chat_model: ChatModel | None = None) -> Route:
    rule_route = _rule_based_route(question)
    if rule_route is not None:
        return rule_route
    q_route = _quantity_route(question)
    if q_route is not None:
        return q_route

    chat = chat_model or get_chat_model_for(TASK_ROUTING)
    with audit("router.classify") as span:
        raw = chat.complete(_SYSTEM, f"Question: {question}", json_only=True)
        try:
            route = json.loads(raw).get("route")
        except (json.JSONDecodeError, AttributeError, TypeError):
            route = None
        if route not in ("structured", "wiki", "graph"):
            route = "wiki"  # fail safe to the read-only grounded backend
        route = _apply_wiki_default(route)
        span.add_meta(route=route)
        return route


def _rule_based_route(question: str) -> Route | None:
    """Cheap deterministic routing for obvious metric/list/contact/relation asks."""
    compact = question.lower().replace(" ", "")
    has_structured_term = any(term.replace(" ", "") in compact for term in _STRUCTURED_TERMS)
    has_structured_entity = any(term in compact for term in _STRUCTURED_ENTITIES)
    if has_structured_term and has_structured_entity:
        return "structured"
    # Graph (relation/conflict/provenance) is checked ahead of _WIKI_TERMS so that a
    # relational question phrased with "어떻게 연결" etc. routes to graph, not wiki. It is
    # only a CANDIDATE — router.answer confirms node/scope/kg_available + param binding and
    # demotes back to wiki on any miss (kg-impl §7.1).
    if any(term.replace(" ", "") in compact for term in _GRAPH_TERMS):
        return "graph"
    if any(term.replace(" ", "") in compact for term in _WIKI_TERMS):
        return "wiki"
    if has_structured_term:
        return "structured"
    return None


# Intent enum widened to cover BOTH read backends and command families, used only for the
# ambiguous middle (no deterministic rule fired). The read half mirrors _SYSTEM; the command
# half tells the LLM to pick an action family ONLY when the user asks to DO something — a
# summarize/explain/list/define of existing content is a READ, never a command.
_INTENT_SYSTEM = (
    "You are an intent classifier for a company knowledge assistant. Decide what the user "
    "wants. There are two kinds of intent.\n\n"
    "READ — answer a question from existing knowledge:\n"
    "- 'structured': counts, lists, filters, aggregations over company DBs (티켓, KPI, 팀원, "
    "계획). 'how many', '개수', '목록', '상태별', '담당자'.\n"
    "- 'graph': the RELATIONSHIP between two things, what conflicts with a claim, or where a "
    "fact came from — 'A와 B는 무슨 관계', '어떻게 연결', '무엇과 충돌', '근거가 어디서'.\n"
    "- 'wiki': explanatory / knowledge — how / why / what-is / policy / definition, or asking "
    "to SUMMARIZE or EXPLAIN existing wiki content ('정리된 내용 요약해줘', '설명해줘').\n\n"
    "COMMAND — perform an action. Pick a command ONLY when the user asks to DO something, not "
    "when they ask ABOUT something:\n"
    "- 'connector_sync': sync/import a connector (Gmail, Drive, Notion, Slack, GitHub) — "
    "'gmail 동기화해줘', '노션 최신화 좀 해줘'.\n"
    "- 'email_send': send / draft / reply an email — 'X에게 메일 보내줘', '답장 초안 써줘'.\n"
    "- 'document_draft': draft / write a NEW document — '회의록 문서 작성해줘'.\n"
    "- 'personal_board_cleanup': clean up / tidy the personal board — '보드 정리해줘'.\n"
    "- 'central_wiki_task_cleanup': resolve / reflect / process wiki tasks — '위키 반영해줘', "
    "'위키 태스크 정리해줘'.\n\n"
    "If the user is asking to summarize, explain, list, or define existing content, that is a "
    "READ (wiki/structured/graph), NOT a command. When unsure, choose a READ route.\n"
    'Respond with JSON only: {"intent": "<one of: structured, wiki, graph, connector_sync, '
    'email_send, document_draft, personal_board_cleanup, central_wiki_task_cleanup>"}.'
)


class RouteIntent(NamedTuple):
    """Unified intent decision (docs/architecture-v2.md §1). `command_family` is non-None only
    for an actionable command (queued as agent_work); `route` is always a real read backend so
    a non-operator / suppressed caller can still get a grounded answer — 'wiki' is the safe
    read for a command (the command itself rides on `command_family`)."""

    command_family: str | None
    route: Route


def classify_intent(
    question: str,
    *,
    allow_commands: bool = True,
    chat_model: ChatModel | None = None,
    keyword_only: bool = False,
) -> RouteIntent:
    """One intent decision covering BOTH command-intake and read routing.

    Deterministic fast-path resolves the clear cases with NO LLM call (prod LLM is a codex
    subprocess — keep the hot path cheap); the single LLM enum is widened from the 3-way read
    route to a 7-way {read | command-family} label only for the ambiguous middle. Net LLM
    calls per /ask are unchanged vs the old detect()+classify() two-step.

    `allow_commands=False` makes this behave exactly like `classify()` (read route only) — used
    by the knowledge-only decompose worker (suppressed action-intake) and read-only callers.

    `keyword_only=True` (S3, command-split invariant 14): a command family is accepted ONLY from
    the deterministic keyword detector — a family that would come from the LLM enum ambiguous
    middle is demoted to a READ route. The deterministic keyword_family return below is NOT
    affected (that IS the path command-split relies on). Off by default: the pre-existing decompose
    action-intake leaf keeps its LLM-enum behavior until the follow-up seal.
    """
    if allow_commands:
        # 1+2. Deterministic command detection: an explicit delegation prefix (→ agent_task)
        # or a clear keyword family. detect_assistant_command_action() already runs
        # parse_chat_delegation first and applies the _INFO_QUERY_TERMS read-guard, so a
        # summarize/explain ASK returns None here and falls through to the read routes below.
        # Late import keeps the router→agentwork edge lazy (no module-load cycle).
        from orthus.agentwork.service import detect_assistant_command_action

        keyword_family = detect_assistant_command_action(question)
        if keyword_family is not None:
            return RouteIntent(command_family=keyword_family, route="wiki")
    # 3. Clear read route — deterministic rule fast-path. No LLM.
    rule_route = _rule_based_route(question)
    if rule_route is not None:
        return RouteIntent(command_family=None, route=rule_route)
    # 3b. C2 후순위 수량 신호 — classify()와 동일. "얼마나 되나요"류를 LLM 앞에서 structured로
    # 잡아, read-route 결정이 두 분류기에서 갈라지지 않게 한다(code review #2).
    q_route = _quantity_route(question)
    if q_route is not None:
        return RouteIntent(command_family=None, route=q_route)
    # 4. Ambiguous middle → ONE LLM call. Widened {read | command} enum when commands allowed.
    chat = chat_model or get_chat_model_for(TASK_INTENT)
    system = _INTENT_SYSTEM if allow_commands else _SYSTEM
    key = "intent" if allow_commands else "route"
    with audit("router.classify") as span:
        raw = chat.complete(system, f"Question: {question}", json_only=True)
        try:
            label = json.loads(raw).get(key)
        except (json.JSONDecodeError, AttributeError, TypeError):
            label = None
        if allow_commands and not keyword_only and label in COMMAND_FAMILIES:
            span.add_meta(route="wiki", classified_route="wiki", command_family=label)
            return RouteIntent(command_family=label, route="wiki")
        route: Route = label if label in ("structured", "wiki", "graph") else "wiki"
        # C2 사각지대 wiki 기본값 — classify()와 동일(graph는 살림). 두 분류기 일관성(code review #2).
        route = _apply_wiki_default(route)
        span.add_meta(route=route, classified_route=route)
        return RouteIntent(command_family=None, route=route)
