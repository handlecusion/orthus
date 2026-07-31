"""Slice 2 inline agentic /ask meta read tools (inbox_summary + data_gaps).

Contract-critical seams without a live Bedrock, backend, or DB:
- meta tools are in-process Solar engine ONLY (cli reaches them via orthus-mcp later),
- they are read-only auxiliary context (NOT answer grounding — wiki page grounding
  invariant untouched; they never mutate the RoutedAnswer's wiki sources),
- callers compact + cap their backend results for the model context window,
- owner-scoped: an empty owner gets an honest 0-count / empty list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import orthus.router.agentic.loop as agentic_loop
import orthus.router.agentic.tools as agentic_tools
from orthus.schemas.canonical import (
    AgentWorkInboxAgentWorkBucket,
    AgentWorkInboxAgentWorkItem,
    AgentWorkInboxBuckets,
    AgentWorkInboxDataGapBucket,
    AgentWorkInboxPromotionBucket,
    AgentWorkInboxSummary,
    AgentWorkInboxWikiTaskBucket,
    DataGap,
)

_USER = UUID("cbd68a41-cdb7-4fd7-8ed1-605f7ecff93f")


class _RecordingAgentChat:
    def __init__(self, script):
        self._script = script
        self.tools_seen: list[dict] = []

    def run_tool_loop(self, *, system, question, tools, dispatch, on_event=None, max_turns=6):
        self.tools_seen = list(tools)
        self.system_seen = system
        return self._script(dispatch)


def _loop_settings(*, node_kind="company"):
    return SimpleNamespace(
        node_id="company",
        node_kind=node_kind,
        mail_send_enabled=False,
    )


def _summary() -> AgentWorkInboxSummary:
    return AgentWorkInboxSummary(
        generated_at=datetime(2026, 6, 30, tzinfo=UTC),
        node_id="company",
        node_kind="company",
        buckets=AgentWorkInboxBuckets(
            wiki_tasks=AgentWorkInboxWikiTaskBucket(count=0, items=[]),
            promote_staging=AgentWorkInboxPromotionBucket(count=0, supported=True, items=[]),
            data_gaps=AgentWorkInboxDataGapBucket(count=0, items=[]),
            agent_work=AgentWorkInboxAgentWorkBucket(
                count=2,
                by_outcome={"draft_for_review": 2},
                items=[
                    AgentWorkInboxAgentWorkItem(
                        work_id=UUID("11111111-1111-1111-1111-111111111111"),
                        title="메일 답장 초안",
                        source_kind="mail_reply",
                        action_family="email_send",
                        state="draft_for_review",
                        updated_at=datetime(2026, 6, 30, tzinfo=UTC),
                        href="/agent-work/x",
                    )
                ],
            ),
        ),
    )


def _gap(question: str, *, hit: int = 3, target: str | None = None) -> DataGap:
    now = datetime(2026, 6, 30, tzinfo=UTC)
    return DataGap(
        gap_id=UUID("22222222-2222-2222-2222-222222222222"),
        scope="company",
        reason="insufficient_grounding",
        question=question,
        suggested_target=target,
        suggestion_status="pending",
        hit_count=hit,
        status="open",
        source="feedback",
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )


# --- call_inbox_summary --------------------------------------------------------


def test_call_inbox_summary_compacts_and_caps(monkeypatch):
    monkeypatch.setattr(
        "orthus.agentwork.service.build_inbox_summary", lambda uid, settings=None: _summary()
    )
    out = agentic_tools.call_inbox_summary(_USER, settings=_loop_settings())
    assert out["node_kind"] == "company"
    assert out["agent_work"]["count"] == 2
    assert out["agent_work"]["by_outcome"] == {"draft_for_review": 2}
    assert out["agent_work"]["latest"] == ["메일 답장 초안"]
    rendered = agentic_tools.inbox_summary_result_for_model(out)
    assert "에이전트워크 2건" in rendered


# --- call_data_gaps ------------------------------------------------------------


def test_call_data_gaps_node_scope(monkeypatch):
    captured: dict = {}

    def _list_gaps(uid, *, status, limit):
        captured.update(uid=uid, status=status, limit=limit)
        return [_gap("정산 주기는?", target="회사 Notion '정산' 페이지")]

    monkeypatch.setattr("orthus.wiki.gap.list_gaps", _list_gaps)
    rows = agentic_tools.call_data_gaps(_USER, {})
    assert captured == {"uid": _USER, "status": "open", "limit": 20}
    assert rows[0]["question"] == "정산 주기는?"
    rendered = agentic_tools.data_gaps_result_for_model(rows)
    assert "정산 주기는?" in rendered and "회사 Notion" in rendered


def test_call_data_gaps_page_scope(monkeypatch):
    captured: dict = {}

    def _for_page(uid, slug, *, status, limit):
        captured.update(uid=uid, slug=slug, status=status, limit=limit)
        return [_gap("토픽 갭?")]

    monkeypatch.setattr("orthus.wiki.gap.list_gaps_for_wiki_page", _for_page)
    rows = agentic_tools.call_data_gaps(_USER, {"slug": "회사-개요"})
    assert captured["slug"] == "회사-개요"
    assert captured["status"] == "open"
    assert rows[0]["question"] == "토픽 갭?"


def test_data_gaps_empty_is_honest():
    assert "0건" in agentic_tools.data_gaps_result_for_model([])


# --- loop wiring ---------------------------------------------------------------


def test_loop_dispatch_inbox_and_gaps(monkeypatch):
    monkeypatch.setattr(agentic_loop, "get_settings", _loop_settings)
    monkeypatch.setattr(
        agentic_tools,
        "call_inbox_summary",
        lambda uid, settings=None: {
            "node_kind": "company",
            "wiki_tasks": {"count": 0, "latest": []},
            "promote_staging": {"count": 0, "supported": True, "latest": []},
            "data_gaps": {"count": 1, "latest": ["정산 주기는?"]},
            "agent_work": {"count": 0, "by_outcome": {}, "latest": []},
        },
    )
    monkeypatch.setattr(
        agentic_tools,
        "call_data_gaps",
        lambda uid, raw: [{"question": "정산 주기는?", "hit_count": 3, "suggested_target": None}],
    )

    seen: list[str] = []

    def script(dispatch):
        seen.append(dispatch("inbox_summary", {}))
        seen.append(dispatch("data_gaps", {}))
        return "확인했습니다."

    out = agentic_loop.run_agentic_answer(
        _USER, "내 할 일이랑 빈 정보 뭐 있어?", agent_chat=_RecordingAgentChat(script)
    )
    assert out.mode == "wiki"
    # meta tool results never become answer grounding sources.
    assert out.wiki.sources == []
    assert "데이터갭 1건" in seen[0]
    assert "정산 주기는?" in seen[1]


def test_loop_advertises_meta_tools_in_process(monkeypatch):
    monkeypatch.setattr(agentic_loop, "get_settings", _loop_settings)
    chat = _RecordingAgentChat(lambda dispatch: "done")
    agentic_loop.run_agentic_answer(_USER, "수신함", agent_chat=chat)
    names = {t["name"] for t in chat.tools_seen}
    assert {"inbox_summary", "data_gaps"} <= names
    assert "inbox_summary" in chat.system_seen


def test_system_prompt_meta_lines_gated():
    from datetime import date

    today = date(2026, 6, 30)
    with_meta = agentic_loop._system_prompt(today, meta_tools=True)
    assert "inbox_summary" in with_meta and "data_gaps" in with_meta
    bare = agentic_loop._system_prompt(today)
    assert "inbox_summary" not in bare and "data_gaps" not in bare
