"""Slice 3 — company directory/structure read tools (team_members/board/projects).

Two parity surfaces without a live central, Bedrock, or DB:
- CentralClient methods hit the right read endpoints (orthus-mcp surface),
- in-process Solar engine callables project compact/capped rows and drop PII,
- the loop advertises the three tools on the bedrock company node ONLY,
- dispatch routes each tool name to its renderer.

Design principle 7: these are auxiliary company-scope context, NOT a grounding
source. All returns are read-only and row-capped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import orthus.router.agentic.loop as agentic_loop
import orthus.router.agentic.tools as agentic_tools
from orthus.mcp.central import CentralClient, CentralConfig
from orthus.schemas.canonical import BoardCard

_USER = UUID("cbd68a41-cdb7-4fd7-8ed1-605f7ecff93f")


class _RecordingAgentChat:
    """Stands in for BedrockConverseChat; records the tool specs it was handed."""

    def __init__(self, script):
        self._script = script
        self.tools_seen: list[dict] = []
        self.system_seen = ""

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


# --- CentralClient methods (orthus-mcp surface) ---------------------------------


def _client_capturing(calls: list[tuple]) -> CentralClient:
    client = CentralClient(CentralConfig(base_url="https://central", token="dct_x"))

    def _fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"ok": True}

    client._request = _fake_request  # type: ignore[method-assign]
    return client


def test_central_client_methods_hit_read_endpoints():
    calls: list[tuple] = []
    client = _client_capturing(calls)
    client.team_members()
    client.board()
    client.projects()
    paths = [(m, p) for m, p, _ in calls]
    assert paths == [
        ("GET", "/dashboard/team-members"),
        ("GET", "/board"),
        ("GET", "/projects"),
    ]


# --- in-process Solar engine callables ----------------------------------------------


def test_call_team_members_projects_safe_fields_and_drops_inactive(monkeypatch):
    members = [
        SimpleNamespace(
            name="김대표",
            title="대표",
            department="경영",
            email="ceo@nova.example",
            active=True,
            phone="010-0000-0000",
            address="서울",
            bank_account="123-456",
        ),
        SimpleNamespace(
            name="퇴사자",
            title=None,
            department=None,
            email=None,
            active=False,
            phone=None,
            address=None,
            bank_account=None,
        ),
    ]
    monkeypatch.setattr(agentic_tools, "list_team_members", lambda node_id: members)
    rows = agentic_tools.call_team_members("company")
    # inactive dropped; only low-risk identity fields surfaced (no PII).
    assert len(rows) == 1
    assert set(rows[0].keys()) == {"name", "title", "department", "email"}
    assert "phone" not in rows[0] and "bank_account" not in rows[0]
    rendered = agentic_tools.team_members_result_for_model(rows)
    assert "김대표" in rendered and "대표" in rendered
    assert "팀원 정보 없음" in agentic_tools.team_members_result_for_model([])


def test_call_team_members_caps_rows(monkeypatch):
    many = [
        SimpleNamespace(name=f"m{i}", title=None, department=None, email=None, active=True)
        for i in range(200)
    ]
    monkeypatch.setattr(agentic_tools, "list_team_members", lambda node_id: many)
    rows = agentic_tools.call_team_members("company")
    assert len(rows) <= agentic_tools._TEAM_MEMBER_CAP


def test_call_board_projects_fields_and_caps(monkeypatch):
    cards = [
        BoardCard(
            row_id=UUID("11111111-1111-1111-1111-111111111111"),
            title="로드맵 카드",
            status="In Progress",
            assignee="김대표",
            priority="High",
            due_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        )
    ]
    monkeypatch.setattr(agentic_tools, "list_board", lambda user_id: cards)
    rows = agentic_tools.call_board(_USER)
    assert rows[0] == {
        "title": "로드맵 카드",
        "status": "In Progress",
        "assignee": "김대표",
        "priority": "High",
        "due": "2026-07-01",
    }
    rendered = agentic_tools.board_result_for_model(rows)
    assert "[In Progress]" in rendered and "로드맵 카드" in rendered
    assert "0건" in agentic_tools.board_result_for_model([])


def test_call_projects_projects_groups(monkeypatch):
    captured = agentic_tools.call_projects  # ensure attribute exists

    def _fake_projects():
        return {
            "projects": ["company", "nova"],
            "groups": [
                {
                    "db_name": "Nova 개발 로드맵",
                    "project": "nova",
                    "count": 12,
                    "source": "default",
                }
            ],
        }

    monkeypatch.setattr(agentic_tools, "call_projects", _fake_projects)
    result = agentic_tools.call_projects()
    rendered = agentic_tools.projects_result_for_model(result)
    assert "nova" in rendered and "12행" in rendered
    assert "0건" in agentic_tools.projects_result_for_model({"projects": [], "groups": []})
    assert captured is not None


# --- loop wiring ---------------------------------------------------------------


def test_loop_advertises_directory_tools_on_bedrock_company_only(monkeypatch):
    monkeypatch.setattr(agentic_loop, "get_settings", _loop_settings)
    chat = _RecordingAgentChat(lambda dispatch: "done")
    agentic_loop.run_agentic_answer(_USER, "팀에 누구 있어?", agent_chat=chat)
    names = {t["name"] for t in chat.tools_seen}
    assert {"team_members", "board", "projects"} <= names
    assert "team_members" in chat.system_seen


def test_loop_hides_directory_tools_on_personal_node(monkeypatch):
    monkeypatch.setattr(
        agentic_loop, "get_settings", lambda: _loop_settings(node_kind="personal")
    )
    chat = _RecordingAgentChat(lambda dispatch: "done")
    agentic_loop.run_agentic_answer(_USER, "팀에 누구 있어?", agent_chat=chat)
    names = {t["name"] for t in chat.tools_seen}
    assert not ({"team_members", "board", "projects"} & names)


def test_loop_dispatch_routes_directory_tools(monkeypatch):
    monkeypatch.setattr(agentic_loop, "get_settings", _loop_settings)
    monkeypatch.setattr(agentic_tools, "call_team_members", lambda node_id: [{"name": "김대표"}])
    monkeypatch.setattr(
        agentic_tools, "call_board", lambda user_id: [{"status": "Done", "title": "x"}]
    )
    monkeypatch.setattr(
        agentic_tools, "call_projects", lambda: {"projects": ["nova"], "groups": []}
    )
    seen: dict[str, str] = {}

    def script(dispatch):
        seen["team"] = dispatch("team_members", {})
        seen["board"] = dispatch("board", {})
        seen["projects"] = dispatch("projects", {})
        return "done"

    agentic_loop.run_agentic_answer(_USER, "회사 구조", agent_chat=_RecordingAgentChat(script))
    assert "김대표" in seen["team"]
    assert "[Done]" in seen["board"]
    assert "프로젝트 그룹 없음" in seen["projects"]


def test_system_prompt_directory_lines_gated():
    from datetime import date

    today = date(2026, 6, 30)
    with_dir = agentic_loop._system_prompt(today, company_directory=True)
    assert "team_members" in with_dir and "board" in with_dir and "projects" in with_dir
    bare = agentic_loop._system_prompt(today)
    assert "team_members" not in bare
