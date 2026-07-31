"""P8.7b: orthus-mcp FastMCP tool registration + central error surfacing.

In-memory only (no network, no Postgres). Asserts the registered tool set
(27 tools incl. slice-1 kg_relations/entity_relations, slice-2 inbox_summary/
data_gaps, slice-3 team_members/board/projects, and team/personal schedule
write tools) and that a central tool with no central config surfaces a clear,
leak-free error instead of crashing — the stdio smoke (`make mcp-smoke`) covers
the process boundary.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from orthus.mcp.central import CENTRAL_URL_ENV, TOKEN_ENV, CentralError, resolve_config
from orthus.mcp.server import build_server

EXPECTED_TOOLS = {
    "wiki_search",
    "wiki_page",
    "wiki_ask",
    "structured",
    "wiki_update_candidate",
    "whoami",
    "agent_work_list",
    "agent_work_get",
    "submit_email_draft",
    "delegate_task",
    "mail_list",
    "mail_get",
    "kg_relations",
    "entity_relations",
    "inbox_summary",
    "data_gaps",
    "team_schedule",
    "team_schedule_add",
    "team_schedule_update",
    "team_schedule_delete",
    "team_members",
    "team_members_add",
    "board",
    "projects",
    "personal_schedule_list",
    "personal_schedule_add",
    "personal_schedule_update",
    "ticket_list",
    "ticket_get",
    "ticket_create",
    "ticket_update",
    "ticket_comment",
    "ticket_project_list",
    "ticket_board",
    "ticket_board_add",
    "ticket_board_move",
}


def test_server_registers_expected_tools():
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS
    assert len(names) == 36


def test_ask_user_tool_is_env_gated(monkeypatch):
    # HITL: ask_user is registered ONLY when ORTHUS_MCP_ASK_USER=1 (set solely by the
    # interactive /ask cli agent). Default build (no env) is the unchanged 36-tool set
    # — delegation / `orthus` CLI never see it, and the mcp-smoke set holds.
    monkeypatch.delenv("ORTHUS_MCP_ASK_USER", raising=False)
    names_off = {t.name for t in asyncio.run(build_server().list_tools())}
    assert "ask_user" not in names_off
    assert len(names_off) == 36

    monkeypatch.setenv("ORTHUS_MCP_ASK_USER", "1")
    names_on = {t.name for t in asyncio.run(build_server().list_tools())}
    assert "ask_user" in names_on
    assert names_on == EXPECTED_TOOLS | {"ask_user"}


def test_tool_descriptions_mark_central_and_local():
    server = build_server()
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    central = {
        "wiki_search",
        "wiki_page",
        "wiki_ask",
        "structured",
        "wiki_update_candidate",
        "whoami",
        "agent_work_list",
        "agent_work_get",
        "submit_email_draft",
        "delegate_task",
        "mail_list",
        "mail_get",
        "kg_relations",
        "entity_relations",
        "inbox_summary",
        "data_gaps",
        "team_schedule",
        "team_schedule_add",
        "team_schedule_update",
        "team_schedule_delete",
        "team_members",
        "team_members_add",
        "board",
        "projects",
        "personal_schedule_list",
        "personal_schedule_add",
        "personal_schedule_update",
        "ticket_list",
        "ticket_get",
        "ticket_create",
        "ticket_update",
        "ticket_comment",
        "ticket_project_list",
        "ticket_board",
        "ticket_board_add",
        "ticket_board_move",
        "ticket_board",
        "ticket_board_add",
        "ticket_board_move",
        "ticket_project_list",
        "ticket_board",
        "ticket_board_add",
        "ticket_board_move",
        "ticket_board",
        "ticket_board_add",
        "ticket_board_move",
    }
    for name in central:
        assert tools[name].description.startswith("[central]")


def test_ticket_list_is_discoverable_for_todo_intent():
    """Regression: an agent that asks "오늘 내 보드 할 일" discovers tools by
    keyword-matching name+description. ticket_list (the live personal board) must
    carry those anchors, or the agent falls back to the stale wiki corpus (the
    2026-07-14 misroute). Also assert the wiki tools steer toward the live tools."""
    import re

    server = build_server()
    tools = {t.name: t for t in asyncio.run(server.list_tools())}

    # The exact discovery regex an agent used in the field, minus the tool that
    # should now match it. ticket_list must be findable this way.
    intent = re.compile(r"personal|schedule|todo|to-do|daily|agenda|task|board", re.IGNORECASE)
    ticket_blob = f"{tools['ticket_list'].name} {tools['ticket_list'].description}"
    assert intent.search(ticket_blob), "ticket_list is invisible to todo/board discovery"

    # ticket_list clearly claims the "오늘 할 일 / live board" intent and points
    # away from wiki; the wiki tools point back at the live tools.
    tl = tools["ticket_list"].description
    assert "오늘" in tl and "보드" in tl and "live" in tl.lower()
    for wiki in ("wiki_search", "wiki_ask"):
        assert "ticket_list" in tools[wiki].description
    # personal_schedule_list is the calendar, not the to-do board.
    assert "ticket_list" in tools["personal_schedule_list"].description


def test_central_tool_without_config_surfaces_tool_error(monkeypatch):
    """A central tool with no ORTHUS_MCP_CENTRAL_URL fails with a clear ToolError
    (not a stack trace), and the token never appears in the message."""
    from mcp.server.fastmcp.exceptions import ToolError

    monkeypatch.delenv(CENTRAL_URL_ENV, raising=False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    server = build_server()
    raised: list[Exception] = []
    try:
        asyncio.run(server.call_tool("wiki_search", {"query": "x"}))
    except ToolError as exc:  # FastMCP may re-wrap; capture either way
        raised.append(exc)
    except Exception as exc:  # noqa: BLE001
        raised.append(exc)
    assert raised, "expected an error when central is unconfigured"
    message = str(raised[0])
    assert CENTRAL_URL_ENV in message


def test_central_client_inbox_summary_and_data_gaps_paths():
    """The two slice-2 CentralClient methods hit the right paths (no network).

    `data_gaps()` routes to /gaps (status=open) by default and to the page
    endpoint when a slug is given; `inbox_summary()` hits /agent-work/inbox-summary."""
    import httpx

    from orthus.mcp.central import CentralClient, CentralConfig

    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"ok": True})

    client = CentralClient(CentralConfig(base_url="https://central.example", token="dct_x"))
    transport = httpx.MockTransport(handler)

    # Patch the per-request httpx.Client to use the mock transport.
    real_client = httpx.Client

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    httpx.Client = _patched  # type: ignore[assignment]
    try:
        client.inbox_summary()
        client.data_gaps()
        client.data_gaps("회사-개요")
    finally:
        httpx.Client = real_client  # type: ignore[assignment]

    assert seen[0] == ("GET", "/agent-work/inbox-summary", {})
    assert seen[1] == ("GET", "/gaps", {"status": "open"})
    assert seen[2][:2] == ("GET", "/wiki/pages/회사-개요/data-gaps")


def test_submit_email_draft_sends_structured_slot(monkeypatch):
    """P10 spec §13 Finding 3: submit_email_draft는 NL 문자열(kind=command)이 아니라
    구조화 kind=email_draft slot(recipient/instruction)을 보낸다 — 서버 분류기 의존
    제거. recipient 빈칸은 None으로 보내 서버가 request_more_data로 멈추게 한다."""
    import httpx

    monkeypatch.setenv(CENTRAL_URL_ENV, "https://central.example")
    monkeypatch.setenv(TOKEN_ENV, "dct_x")

    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen.append((request.method, request.url.path, _json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    httpx.Client = _patched  # type: ignore[assignment]
    try:
        server = build_server()
        asyncio.run(
            server.call_tool(
                "submit_email_draft",
                {"recipient": "bob@example.com", "instruction": "회의 요약 공유"},
            )
        )
        asyncio.run(server.call_tool("submit_email_draft", {"instruction": "회의 요약 공유"}))
    finally:
        httpx.Client = real_client  # type: ignore[assignment]

    assert seen[0] == (
        "POST",
        "/collector/gateway/actions",
        {"kind": "email_draft", "recipient": "bob@example.com", "instruction": "회의 요약 공유"},
    )
    assert seen[1] == (
        "POST",
        "/collector/gateway/actions",
        {"kind": "email_draft", "recipient": None, "instruction": "회의 요약 공유"},
    )


def test_resolve_config_requires_url(monkeypatch):
    monkeypatch.delenv(CENTRAL_URL_ENV, raising=False)
    monkeypatch.setenv(TOKEN_ENV, "dct_token")
    try:
        resolve_config()
    except CentralError as exc:
        assert CENTRAL_URL_ENV in str(exc)
    else:
        raise AssertionError("expected CentralError when central URL missing")


def test_stdio_smoke_script_lists_all_tools():
    """End-to-end stdio: spawn `python -m orthus.mcp`, MCP-initialize, list tools.

    This is the in-suite mirror of `make mcp-smoke`; it exercises the real
    process boundary, not just in-memory registration."""
    import subprocess
    import sys
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "scripts/mcp/stdio_smoke.py"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "36 tools over stdio" in completed.stdout
