#!/usr/bin/env python
"""Spawn ``orthus-mcp`` over stdio, MCP-initialize, list tools, assert the full set.

Used by ``make mcp-smoke``. Exits 0 on success, non-zero otherwise. No network
is required: tool registration is static and listing does not call central.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    # central
    "wiki_search",
    "wiki_page",
    "wiki_ask",
    "structured",
    "team_schedule",
    "team_schedule_add",
    "team_schedule_update",
    "team_schedule_delete",
    "team_members",
    "team_members_add",
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
    "board",
    "projects",
    # local
}


async def _run() -> int:
    params = StdioServerParameters(command=sys.executable, args=["-m", "orthus.mcp"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            names = {tool.name for tool in result.tools}
    missing = EXPECTED_TOOLS - names
    extra = names - EXPECTED_TOOLS
    if missing or extra:
        print(f"mcp-smoke FAIL: missing={sorted(missing)} unexpected={sorted(extra)}")
        return 1
    print(f"mcp-smoke OK: orthus-mcp exposed {len(names)} tools over stdio")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
