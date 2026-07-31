"""Unit tests for the unified router intent classifier (router.classify_intent).

The deterministic fast-path (delegation prefix / keyword command / read rule) needs no
LLM; the ambiguous-middle branch is exercised with a scripted MockChat. Folds the old
detect()+classify() two-step into one decision (docs/architecture-v2.md §1).
"""

from __future__ import annotations

from orthus.models.adapters.mock import MockChat
from orthus.router.route import COMMAND_FAMILIES, RouteIntent, classify_intent
from orthus.router.tools import _suppress_action_intake, classify_subpart


def _no_llm() -> MockChat:
    # If the LLM branch were reached, MockChat's json_only default ({"sql": ...}) carries
    # no intent/route key, so classify_intent fail-safes to a wiki read.
    return MockChat()


def test_explicit_delegation_prefix_is_agent_task():
    assert classify_intent("위임: 리포 테스트 고쳐줘", chat_model=_no_llm()) == RouteIntent(
        "agent_task", "wiki"
    )


def test_clear_keyword_command_no_llm():
    assert (
        classify_intent("gmail 동기화 해줘", chat_model=_no_llm()).command_family == "connector_sync"
    )
    assert (
        classify_intent("보드 정리해줘", chat_model=_no_llm()).command_family
        == "personal_board_cleanup"
    )


def test_wiki_summary_read_is_not_a_command():
    # The reported regression: summarize/explain of wiki content is a READ, never a command.
    assert classify_intent(
        "아틀라스 제품 관련 회사 위키에 정리된 내용 요약해줘", chat_model=_no_llm()
    ) == RouteIntent(None, "wiki")
    assert classify_intent("회사 위키에 정리된 내용 설명해줘", chat_model=_no_llm()) == RouteIntent(
        None, "wiki"
    )


def test_structured_read_rule():
    assert classify_intent("팀원 목록 보여줘", chat_model=_no_llm()) == RouteIntent(None, "structured")


def test_ambiguous_middle_llm_picks_command():
    # No keyword/rule fires (loose phrasing keyword-detection misses) → the LLM decides.
    chat = MockChat(rules=[("intent", '{"intent": "connector_sync"}')])
    assert classify_intent("노션 최신화 좀", chat_model=chat).command_family == "connector_sync"


def test_ambiguous_middle_llm_picks_read():
    chat = MockChat(rules=[("intent", '{"intent": "wiki"}')])
    assert classify_intent("이거 관련해서 알아봐줘", chat_model=chat) == RouteIntent(None, "wiki")


def test_unparseable_llm_failsafe_to_wiki():
    assert classify_intent("이거 관련해서 알아봐줘", chat_model=_no_llm()) == RouteIntent(None, "wiki")


def test_allow_commands_false_never_a_command():
    # Knowledge-only decompose worker path: a command-shaped question routes as a READ.
    intent = classify_intent("gmail 동기화 해줘", allow_commands=False, chat_model=_no_llm())
    assert intent.command_family is None


def test_command_families_in_sync_minus_agent_task():
    # Tripwire: the LLM enum set stays aligned with the keyword families, minus agent_task
    # (which is detected deterministically by an explicit prefix, never by the LLM).
    assert "agent_task" not in COMMAND_FAMILIES
    assert COMMAND_FAMILIES == frozenset(
        {
            "connector_sync",
            "email_send",
            "document_draft",
            "personal_board_cleanup",
            "central_wiki_task_cleanup",
        }
    )


def test_classify_subpart_routes_command_to_action_intake():
    assert classify_subpart("gmail 동기화 해줘") == ("action-intake", "connector_sync")


def test_classify_subpart_suppression_routes_command_as_read():
    # MA.8a: the knowledge-only worker must never spawn an action item — a command-shaped
    # sub-question routes as a READ (allow_commands=False) instead.
    token = _suppress_action_intake.set(True)
    try:
        leaf, family = classify_subpart("gmail 동기화 해줘")
    finally:
        _suppress_action_intake.reset(token)
    assert family is None
    assert leaf in ("structured", "graph", "wiki")
