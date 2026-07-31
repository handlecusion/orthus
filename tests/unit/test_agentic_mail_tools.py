"""Inline agentic /ask mail tools (mail_search read + mail_compose draft).

Contract-critical seams without a live Bedrock, backend, or DB:
- mail tools are in-process Solar engine ONLY (cli reaches them via orthus-mcp later),
- mail_compose builds a NON-sent draft and never calls the send path,
- the model can never pick the sender identity (no from_addr in the schema),
- a built draft rides RoutedAnswer.mail_draft for the FE 보내기 card.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import orthus.router.agentic.loop as agentic_loop
import orthus.router.agentic.tools as agentic_tools
from orthus.schemas.canonical import CanonicalEmail, MailComposeDraft, MailInboxResponse

_USER = UUID("cbd68a41-cdb7-4fd7-8ed1-605f7ecff93f")


class _RecordingAgentChat:
    """Stands in for BedrockConverseChat; records the tool specs it was handed."""

    def __init__(self, script):
        self._script = script
        self.tools_seen: list[dict] = []

    def run_tool_loop(self, *, system, question, tools, dispatch, on_event=None, max_turns=6):
        self.tools_seen = list(tools)
        self.system_seen = system
        return self._script(dispatch)


def _loop_settings(*, send_enabled=True, node_kind="company"):
    return SimpleNamespace(
        node_id="company",
        node_kind=node_kind,
        mail_send_enabled=send_enabled,
    )


# --- tool spec shape -----------------------------------------------------------


def test_mail_compose_schema_has_no_sender_field():
    # The model fills to/subject/body only; from_addr is resolved server-side so
    # the LLM can never choose the sending identity.
    props = set(agentic_tools.MAIL_COMPOSE_SPEC["input_schema"]["properties"])
    assert props == {"to", "subject", "body"}
    assert "from_addr" not in props
    assert agentic_tools.MAIL_SEARCH_SPEC["input_schema"]["properties"].keys() == {"query"}


# --- mail_search (read) --------------------------------------------------------


def _inbound(subject: str) -> CanonicalEmail:
    return CanonicalEmail(
        backend="nova",
        external_id="m1",
        scope="company",
        direction="inbound",
        from_addr="champion-ops@nova.example",
        to_addr=["me@nova.example"],
        subject=subject,
        body_text="10TB 상향 건 승인합니다.",
        received_at=datetime(2026, 6, 27, 9, 0, tzinfo=UTC),
    )


def test_call_mail_search_is_owner_scoped_and_maps_rows(monkeypatch):
    captured: dict = {}

    async def _fake_inbox(settings, *, owner_id, search, limit, **kw):
        captured.update(owner_id=owner_id, search=search, limit=limit)
        return MailInboxResponse(
            items=[_inbound("Re: AI 챔피언 컴퓨팅 자원 10TB")],
            total=1,
            unread=1,
            backends=[],
            send_enabled=True,
        )

    monkeypatch.setattr(agentic_tools, "list_unified_inbox", _fake_inbox)

    rows = agentic_tools.call_mail_search(
        _USER, {"query": "AI 챔피언 컴퓨팅 자원 10TB"}, settings=_loop_settings()
    )
    # owner isolation: the inbox fan-out is scoped to this user.
    assert captured["owner_id"] == _USER
    assert "10TB" in captured["search"]
    assert len(rows) == 1
    assert rows[0]["subject"].startswith("Re:")
    assert rows[0]["direction"] == "inbound"

    rendered = agentic_tools.mail_search_result_for_model(rows)
    assert "수신" in rendered and "Re: AI 챔피언" in rendered


def test_mail_search_empty_query_skips_backend(monkeypatch):
    def _boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("empty query must not hit the backend")

    monkeypatch.setattr(agentic_tools, "list_unified_inbox", _boom)
    assert agentic_tools.call_mail_search(_USER, {"query": "  "}, settings=_loop_settings()) == []
    assert "0건" in agentic_tools.mail_search_result_for_model([])


# --- mail_compose (draft, no send) ---------------------------------------------


def test_build_mail_compose_draft_resolves_sender_and_does_not_send(monkeypatch):
    # from_addr is resolved server-side; the function has no send side effect.
    monkeypatch.setattr(
        agentic_tools, "resolve_compose_from_addr", lambda settings, owner_id: "me@acme.example"
    )
    draft = agentic_tools.build_mail_compose_draft(
        _USER,
        {"to": "champion-ops@nova.example", "subject": "Re: 10TB", "body": "넵 확인했습니다."},
        settings=_loop_settings(),
    )
    assert isinstance(draft, MailComposeDraft)
    assert draft.from_addr == "me@acme.example"
    assert draft.to == "champion-ops@nova.example"
    assert draft.subject == "Re: 10TB"
    assert draft.body == "넵 확인했습니다."


# --- loop wiring ---------------------------------------------------------------


def test_loop_mail_compose_attaches_draft(monkeypatch):
    monkeypatch.setattr(agentic_loop, "get_settings", _loop_settings)
    draft = MailComposeDraft(
        from_addr="me@acme.example", to="champion-ops@nova.example", subject="Re: 10TB", body="확인했습니다."
    )
    monkeypatch.setattr(agentic_tools, "build_mail_compose_draft", lambda *a, **k: draft)

    def script(dispatch):
        dispatch("mail_compose", {"to": "champion-ops@nova.example", "subject": "Re: 10TB", "body": "확인했습니다."})
        return "초안을 작성했습니다. 확인 후 보내기를 누르세요."

    out = agentic_loop.run_agentic_answer(
        _USER, "AI 챔피언 10TB 답장 써줘", agent_chat=_RecordingAgentChat(script)
    )
    assert out.mode == "wiki"
    assert out.mail_draft is not None
    assert out.mail_draft.to == "champion-ops@nova.example"


def test_loop_advertises_mail_tools_in_process(monkeypatch):
    # in-process 엔진 + send enabled + company node -> both mail tools advertised.
    monkeypatch.setattr(agentic_loop, "get_settings", _loop_settings)
    chat = _RecordingAgentChat(lambda dispatch: "done")
    agentic_loop.run_agentic_answer(_USER, "메일 확인", agent_chat=chat)
    names = {t["name"] for t in chat.tools_seen}
    assert {"mail_search", "mail_compose"} <= names
    assert "mail_search" in chat.system_seen


def test_loop_hides_compose_when_send_disabled(monkeypatch):
    # read works, but no live POST /mail/send -> no compose tool (dead button).
    monkeypatch.setattr(
        agentic_loop, "get_settings", lambda: _loop_settings(send_enabled=False)
    )
    chat = _RecordingAgentChat(lambda dispatch: "done")
    agentic_loop.run_agentic_answer(_USER, "메일 확인", agent_chat=chat)
    names = {t["name"] for t in chat.tools_seen}
    assert "mail_search" in names
    assert "mail_compose" not in names


def test_system_prompt_mail_lines_gated():
    from datetime import date

    today = date(2026, 6, 28)
    with_mail = agentic_loop._system_prompt(today, mail_search=True, mail_compose=True)
    assert "mail_search" in with_mail and "mail_compose" in with_mail
    bare = agentic_loop._system_prompt(today)
    assert "mail_search" not in bare and "mail_compose" not in bare
