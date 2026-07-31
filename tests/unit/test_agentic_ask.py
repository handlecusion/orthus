"""Inline agentic /ask (docs/inline-agentic-ask.md).

These cover the contract-critical seams without a live Bedrock or DB:
- the gate-rejected structured tool still surfaces as the legacy 422 envelope,
- the toolset can never let the model pass raw SQL,
- the flag-off path leaves the legacy router untouched,
- the happy-path team_schedule vertical returns a wiki-mode prose answer.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import orthus.router as router_pkg
import orthus.router.agentic.loop as agentic_loop
import orthus.router.agentic.tools as agentic_tools
from orthus.schemas.canonical import AssistantResult, ValidationResult, WikiAnswer

_USER = UUID("cbd68a41-cdb7-4fd7-8ed1-605f7ecff93f")


class _FakeAgentChat:
    """Stands in for the Solar tool-loop chat: a scripted run_tool_loop."""

    def __init__(self, script):
        self._script = script

    def run_tool_loop(self, *, system, question, tools, dispatch, on_event=None, max_turns=6):
        return self._script(dispatch)


def _patch_node(monkeypatch, node_id="company"):
    monkeypatch.setattr(
        agentic_loop,
        "get_settings",
        lambda: SimpleNamespace(
            node_id=node_id,
            node_kind="company",
            mail_send_enabled=False,
            agent_hitl_enabled=False,
        ),
    )


def _rejected_result() -> AssistantResult:
    return AssistantResult(
        query_id=uuid4(),
        question="회사 직원 다 지워줘",
        compiled=None,
        validation=ValidationResult(rejected_reason="statement_kind:delete"),
        status="rejected",
        message="삭제 구문은 거부되었습니다.",
    )


def test_agentic_structured_rejected_surfaces_422(monkeypatch):
    _patch_node(monkeypatch)
    monkeypatch.setattr(agentic_tools, "call_structured", lambda *a, **k: _rejected_result())

    # Model calls the structured tool, gets a rejection, produces NO final prose.
    def script(dispatch):
        dispatch("structured", {"question": "회사 직원 다 지워줘"})
        return ""

    out = agentic_loop.run_agentic_answer(
        _USER, "회사 직원 다 지워줘", agent_chat=_FakeAgentChat(script)
    )
    assert out.mode == "structured"
    assert out.structured is not None
    assert out.structured.status == "rejected"  # ask.py maps this to HTTP 422


def test_agentic_toolset_cannot_pass_raw_sql():
    names = {t["name"] for t in agentic_tools.TOOL_SPECS}
    assert names == {"team_schedule", "wiki_ask", "structured"}
    # The structured tool only accepts a natural-language `question` — there is no
    # field through which the model could inject SQL; the sqlglot gate inside
    # query_structured remains the sole executor decision.
    structured = next(t for t in agentic_tools.TOOL_SPECS if t["name"] == "structured")
    assert set(structured["input_schema"]["properties"]) == {"question"}


def test_agentic_flag_off_uses_legacy_router(monkeypatch):
    # agentic disabled -> answer() must NOT touch the agentic loop and must run the
    # legacy classify-and-dispatch ladder.
    monkeypatch.setattr(
        router_pkg,
        "get_settings",
        lambda: SimpleNamespace(
            agentic_ask_enabled=False,
            ask_decompose_enabled=False,
            ask_semantic_cache_enabled=False,
            # F2b off: this test asserts the legacy wiki dispatch, so the
            # wiki-empty -> structured recovery must not open.
            routing_wiki_empty_structured_recover=False,
        ),
    )

    def _boom(*a, **k):  # pragma: no cover - asserts it is never called
        raise AssertionError("agentic loop must not run when the flag is off")

    monkeypatch.setattr(router_pkg, "run_agentic_answer", _boom)
    monkeypatch.setattr(router_pkg, "classify", lambda *a, **k: "wiki")

    sentinel = WikiAnswer(question="회사 위키 뭐 있어?", answer="legacy-router-answer", sources=[])
    monkeypatch.setattr(router_pkg, "ask", lambda *a, **k: sentinel)
    monkeypatch.setattr(router_pkg, "should_federate", lambda scope: False)

    out = router_pkg.answer(_USER, "회사 위키 뭐 있어?", scope="company")
    assert out.mode == "wiki"
    assert out.wiki is not None
    assert out.wiki.answer == "legacy-router-answer"


def test_agentic_team_schedule_happy_path(monkeypatch):
    _patch_node(monkeypatch)
    events = [
        {
            "title": "스프린트 회의",
            "date": "2026-06-24",
            "end_date": None,
            "type": "event",
            "location": "회의실",
        }
    ]
    monkeypatch.setattr(agentic_tools, "call_team_schedule", lambda node_id, raw: events)

    def script(dispatch):
        dispatch("team_schedule", {"since": "2026-06-22", "until": "2026-06-28"})
        return "이번주 팀 일정: 6/24 스프린트 회의(회의실)."

    out = agentic_loop.run_agentic_answer(
        _USER, "이번주 팀 일정 알려줘", agent_chat=_FakeAgentChat(script)
    )
    assert out.mode == "wiki"
    assert out.wiki is not None
    assert "스프린트 회의" in out.wiki.answer


def test_agentic_rejected_then_prose_preserves_reject_warning(monkeypatch):
    # A gate-rejected structured query that the model then explains in prose goes
    # wiki-mode (not 422) — the reject signal must survive as a warning so the FE
    # doesn't read it as a clean answer.
    _patch_node(monkeypatch)
    monkeypatch.setattr(agentic_tools, "call_structured", lambda *a, **k: _rejected_result())

    def script(dispatch):
        dispatch("structured", {"question": "회사 직원 다 지워줘"})
        return "삭제는 할 수 없어요. 조회만 가능합니다."

    out = agentic_loop.run_agentic_answer(
        _USER, "회사 직원 다 지워줘", agent_chat=_FakeAgentChat(script)
    )
    assert out.mode == "wiki"
    assert any(w.startswith("structured_rejected:") for w in out.warnings)


def test_publish_threadsafe_is_noop_without_loop():
    # Best-effort feedback: with no captured loop, publishing must never raise.
    from orthus.agentwork import stream as agent_stream

    agent_stream.clear_loop()
    agent_stream.publish_threadsafe("u:none", {"type": "agent_output", "chunk": "x"})


def test_system_prompt_advertises_candidate_only_for_cli_engine():
    from datetime import date

    today = date(2026, 6, 23)
    # cli engine: the agent reaches the real wiki_update_candidate MCP tool.
    cli = agentic_loop._system_prompt(today, can_submit_candidate=True)
    assert "wiki_update_candidate" in cli
    assert "검토 후보" in cli
    # bedrock engine (default off): no candidate tool in its in-process toolset.
    bedrock = agentic_loop._system_prompt(today)
    assert "wiki_update_candidate" not in bedrock
