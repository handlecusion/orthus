"""``orthus-mcp`` FastMCP stdio server (P8.7b, docs/p8-central-consolidation.md §2).

One stdio server on the owner machine exposes **central** tools that call the
central knowledge endpoints over HTTPS with a knowledge-scoped collector token
(``orthus.mcp.central``): wiki search/page/ask, wiki update candidate,
agent-work list/get.

Central calls surface ``CentralError`` as a ``ToolError`` with a clear,
leak-free message (unreachable / 401 revoked / 403 missing scope / 429 rate
limited). The token never appears in any tool output.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from orthus.mcp.central import CentralClient, CentralError
from orthus.mcp.tickets import (
    TicketUXError,
    build_board_row_props,
    board_prop,
    kanban_snapshot,
    normalize_priority,
    normalize_status,
    parse_date_word,
    resolve_board_row,
    resolve_bucket,
    resolve_company_project,
    resolve_kanban_board,
    resolve_project,
    resolve_task,
)

SERVER_NAME = "orthus-mcp"

# P10 mail read tools: list rows stay light (no bodies) and a single body is
# bounded so a long thread cannot blow the agent context window.
MAIL_LIST_MAX_LIMIT = 30
MAIL_BODY_TRUNCATE_CHARS = 2000


def _ticket_tool_error(exc: TicketUXError) -> ToolError:
    """Keep the self-healing hints (valid values + corrected example) in the ToolError."""
    parts = [str(exc)]
    if exc.valid:
        parts.append("valid: " + " | ".join(exc.valid))
    if exc.example:
        parts.append("try: " + exc.example)
    if exc.note:
        parts.append("note: " + exc.note)
    return ToolError(" — ".join(parts))


def build_server() -> FastMCP:
    """Construct the FastMCP server with all registered tools (see EXPECTED_MCP_TOOLS).

    The central client is resolved lazily per call so the server starts even
    when ``ORTHUS_MCP_CENTRAL_URL``/token are not yet configured; a central tool
    then fails with a clear ToolError instead of crashing startup."""
    server = FastMCP(SERVER_NAME)

    def _central() -> CentralClient:
        try:
            return CentralClient()
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    # --- central tools ---------------------------------------------------------

    @server.tool(
        name="wiki_search",
        description="[central] Search compiled wiki pages (older/consolidated knowledge); "
        "returns slug/title/snippet/scope. NOT live state — 오늘 할 일 / 개인 보드 / 일정 같은 "
        "현재 상태는 ticket_list · personal_schedule_list · team_schedule 로 조회한다.",
    )
    def wiki_search(query: str, scope: str = "all", limit: int = 10) -> dict:
        try:
            return _central().wiki_search(query, scope=scope, limit=limit)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="wiki_page",
        description="[central] Read one compiled wiki page by slug.",
    )
    def wiki_page(slug: str) -> dict:
        try:
            return _central().wiki_page(slug)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="wiki_ask",
        description="[central] Grounded Q&A over compiled wiki pages (company + own personal, "
        "older/consolidated knowledge). NOT live state — 오늘 할 일 / 개인 보드 / 일정은 "
        "ticket_list · personal_schedule_list · team_schedule 를 써라.",
    )
    def wiki_ask(question: str, scope: str = "all", context_wiki_slug: str | None = None) -> dict:
        try:
            return _central().wiki_ask(question, scope=scope, context_wiki_slug=context_wiki_slug)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="structured",
        description="[central] 구조화 데이터(노션 행 등)에 자연어 집계/목록/표 질의. 검증 게이트가 서버에서 SELECT 전용 강제. 건수/합계/필터 목록에 사용.",
    )
    def structured(question: str, scope: str = "company", project: str | None = None) -> dict:
        try:
            return _central().structured(question, scope=scope, project=project)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="team_schedule",
        description="[central] Company team calendar — events shared by all team members in a "
        "date range (since/until = YYYY-MM-DD). Check teammates' existing schedule/availability "
        "before proposing meeting times in a reply.",
    )
    def team_schedule(since: str | None = None, until: str | None = None) -> dict:
        try:
            return {"events": _central().team_schedule(since, until)}
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="team_schedule_add",
        description="[central] Add a company team calendar event. title + event_date "
        "(YYYY-MM-DD) required. Optional: all_day, end_date (YYYY-MM-DD), start_time/end_time "
        "(HH:MM), location, event_type, member_ids (team member UUIDs from team_members), "
        "color, description.",
    )
    def team_schedule_add(
        title: str,
        event_date: str,
        all_day: bool = True,
        description: str | None = None,
        end_date: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        event_type: str = "event",
        location: str | None = None,
        member_ids: list[str] | None = None,
        color: str | None = None,
    ) -> dict:
        payload = {
            "title": title,
            "event_date": event_date,
            "all_day": all_day,
            "event_type": event_type,
            "member_ids": member_ids or [],
            "description": description,
            "end_date": end_date,
            "start_time": start_time,
            "end_time": end_time,
            "location": location,
            "color": color,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            return _central().team_schedule_add(payload)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="team_schedule_update",
        description="[central] Update a team calendar event by event_id. Only the fields you "
        "pass are changed (title, event_date, all_day, end_date, start_time, end_time, "
        "event_type, location, member_ids, color, description).",
    )
    def team_schedule_update(
        event_id: str,
        title: str | None = None,
        event_date: str | None = None,
        all_day: bool | None = None,
        description: str | None = None,
        end_date: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        event_type: str | None = None,
        location: str | None = None,
        member_ids: list[str] | None = None,
        color: str | None = None,
    ) -> dict:
        payload = {
            "title": title,
            "event_date": event_date,
            "all_day": all_day,
            "event_type": event_type,
            "member_ids": member_ids,
            "description": description,
            "end_date": end_date,
            "start_time": start_time,
            "end_time": end_time,
            "location": location,
            "color": color,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            return _central().team_schedule_update(event_id, payload)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="team_schedule_delete",
        description="[central] Delete a team calendar event by event_id.",
    )
    def team_schedule_delete(event_id: str) -> dict:
        try:
            return _central().team_schedule_delete(event_id)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="team_members_add",
        description="[central] Add a company team member. name required; optional title, "
        "department, email, phone.",
    )
    def team_members_add(
        name: str,
        title: str | None = None,
        department: str | None = None,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict:
        payload = {
            "name": name,
            "title": title,
            "department": department,
            "email": email,
            "phone": phone,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            return _central().team_members_add(payload)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="personal_schedule_list",
        description="[central] 내 개인 캘린더 일정(fixed events, 시간 블록) — 회의/약속처럼 "
        "시작·종료 시각이 있는 이벤트. since/until = YYYY-MM-DD. Owner-private. 이건 캘린더이지 "
        "할 일 목록(to-do)이 아니다 — '오늘 해야 할 일 / 내 보드'는 ticket_list를 써라. "
        "personal_schedule_update 전 event_id를 찾을 때도 사용.",
    )
    def personal_schedule_list(since: str | None = None, until: str | None = None) -> dict:
        try:
            return {"events": _central().personal_schedule_list(since, until)}
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="personal_schedule_add",
        description="[central] Add a personal schedule event (owner-private). title + starts_at "
        "+ ends_at required; starts_at/ends_at are ISO-8601 datetimes (e.g. "
        "2026-07-01T14:00:00+09:00). Optional project_id, source_label.",
    )
    def personal_schedule_add(
        title: str,
        starts_at: str,
        ends_at: str,
        project_id: str | None = None,
        source_label: str | None = None,
    ) -> dict:
        payload = {
            "title": title,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "project_id": project_id,
            "source_label": source_label,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            return _central().personal_schedule_add(payload)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="personal_schedule_update",
        description="[central] Update a personal schedule event by event_id. Only the fields you "
        "pass change (title, starts_at, ends_at as ISO-8601 datetimes, project_id).",
    )
    def personal_schedule_update(
        event_id: str,
        title: str | None = None,
        starts_at: str | None = None,
        ends_at: str | None = None,
        project_id: str | None = None,
    ) -> dict:
        payload = {
            "title": title,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "project_id": project_id,
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            return _central().personal_schedule_update(event_id, payload)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="ticket_list",
        description="[central] 내 개인 보드의 할 일 / 오늘 해야 할 일 — live personal board "
        "tasks (my board, to-do list, today's todos, agenda). '오늘 뭐 해야 돼', '내 보드', "
        "'내 스케줄 할 일', '오늘 할 일', 'my board / today's todos / to-do list' 는 이 도구로 "
        "답한다 — wiki_search/wiki_ask(오래되고 compiled된 문서)가 아니라 지금 보드의 live "
        "상태다. 시간 블록이 있는 캘린더 일정은 personal_schedule_list. status: "
        "open|done|archived|all (default open). project: 채널 이름(대소문자 무시). "
        "date: today|tomorrow|+3d|YYYY-MM-DD (해당 날짜만; '오늘 할 일'이면 date=today). "
        "티켓 id 앞 8자가 short id다.",
    )
    def ticket_list(
        status: str = "open",
        project: str | None = None,
        date: str | None = None,
        limit: int = 50,
    ) -> dict:
        try:
            client = _central()
            resolved_status = None if status == "all" else normalize_status(status)
            project_id = str(resolve_project(client, project)["project_id"]) if project else None
            day = parse_date_word(date) if date else None
            tickets = client.board_tasks_list(
                status=resolved_status,
                project_id=project_id,
                date_from=day,
                date_to=day,
                limit=limit,
            )
            return {"count": len(tickets), "tickets": tickets}
        except TicketUXError as exc:
            raise _ticket_tool_error(exc) from None
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="ticket_get",
        description="[central] 티켓 하나 + 댓글. ticket_id는 앞 4자 이상 prefix면 충분 "
        "(모호하면 후보를 알려준다).",
    )
    def ticket_get(ticket_id: str) -> dict:
        try:
            client = _central()
            task = resolve_task(client, ticket_id)
            comments = client.board_task_comments(str(task["task_id"])) or []
            return {"ticket": task, "comments": comments}
        except TicketUXError as exc:
            raise _ticket_tool_error(exc) from None
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="ticket_create",
        description="[central] 내 보드에 티켓 생성. 배치는 date(오늘 등 날짜 컬럼)와 "
        "bucket(next_week|next_month|next_quarter|next_year|someday|never) 중 정확히 하나 — "
        "둘 다 없으면 오늘. priority: urgent|priority|normal|low. project: 채널 이름 "
        "(회사 채널이면 팀 전체에게 보임, 담당 프로젝트만 가능). idempotency_key: 재시도 시 "
        "같은 UUID를 넘기면 중복 생성이 없다. 삭제 도구는 없다 — 상태 전이만 가능.",
    )
    def ticket_create(
        title: str,
        date: str | None = None,
        bucket: str | None = None,
        priority: str = "normal",
        project: str | None = None,
        due: str | None = None,
        note: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        try:
            client = _central()
            if date and bucket:
                raise TicketUXError("date and bucket are mutually exclusive (배치는 정확히 하나)")
            payload: dict[str, Any] = {"title": title, "priority": normalize_priority(priority)}
            if bucket:
                payload["backlog_bucket_id"] = str(resolve_bucket(client, bucket)["bucket_id"])
            else:
                payload["scheduled_date"] = parse_date_word(date or "today")
            if project:
                payload["project_id"] = str(resolve_project(client, project)["project_id"])
            if due:
                payload["due_date"] = parse_date_word(due)
            if idempotency_key:
                payload["task_id"] = idempotency_key
            task = client.board_task_create(payload)
            if note:
                task = client.board_task_update(str(task["task_id"]), {"note": note})
            return task
        except TicketUXError as exc:
            raise _ticket_tool_error(exc) from None
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="ticket_update",
        description="[central] 티켓 수정 — 넘긴 필드만 바뀐다. status: open|done|archived "
        "(archived도 open으로 되돌릴 수 있다). date 또는 bucket으로 배치 이동(동시 지정 불가). "
        "project는 채널 이름, 'none'이면 채널 해제. due 'none'이면 마감 해제. note ''이면 노트 삭제.",
    )
    def ticket_update(
        ticket_id: str,
        title: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        date: str | None = None,
        bucket: str | None = None,
        project: str | None = None,
        due: str | None = None,
        note: str | None = None,
    ) -> dict:
        try:
            client = _central()
            task = resolve_task(client, ticket_id)
            if date is not None and bucket is not None:
                raise TicketUXError("date and bucket are mutually exclusive (배치는 정확히 하나)")
            payload: dict[str, Any] = {}
            if title is not None:
                payload["title"] = title
            if status is not None:
                payload["status"] = normalize_status(status)
            if priority is not None:
                payload["priority"] = normalize_priority(priority)
            if date is not None:
                payload["scheduled_date"] = parse_date_word(date)
                payload["backlog_bucket_id"] = None
            if bucket is not None:
                payload["backlog_bucket_id"] = str(resolve_bucket(client, bucket)["bucket_id"])
                payload["scheduled_date"] = None
            if project is not None:
                payload["project_id"] = (
                    None
                    if project.strip().lower() == "none"
                    else str(resolve_project(client, project)["project_id"])
                )
            if due is not None:
                payload["due_date"] = (
                    None if due.strip().lower() == "none" else parse_date_word(due)
                )
            if note is not None:
                payload["note"] = note
            if not payload:
                raise TicketUXError("nothing to update (pass at least one field)")
            return client.board_task_update(str(task["task_id"]), payload)
        except TicketUXError as exc:
            raise _ticket_tool_error(exc) from None
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="ticket_comment",
        description="[central] 티켓에 댓글 추가 (4000자 이내). ticket_id는 prefix면 충분.",
    )
    def ticket_comment(ticket_id: str, body: str) -> dict:
        try:
            client = _central()
            task = resolve_task(client, ticket_id)
            return client.board_task_comment_add(str(task["task_id"]), body)
        except TicketUXError as exc:
            raise _ticket_tool_error(exc) from None
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="ticket_project_list",
        description="[central] 한 회사 프로젝트의 전 팀원 티켓(회사 공개, scope=company)을 "
        "프로젝트 단위로 조회. project는 본인 담당 회사 채널 이름. status: "
        "open|done|archived|all (default open). 팀원의 개인 채널 업무는 포함되지 않는다.",
    )
    def ticket_project_list(project: str, status: str = "open") -> dict:
        try:
            client = _central()
            channel = resolve_company_project(client, project)
            tasks = client.project_board_tasks(str(channel["company_project_id"])) or []
            if status != "all":
                wanted = normalize_status(status)
                tasks = [t for t in tasks if t.get("status") == wanted]
            return {"project": channel.get("name"), "count": len(tasks), "tickets": tasks}
        except TicketUXError as exc:
            raise _ticket_tool_error(exc) from None
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="ticket_board",
        description="[central] 프로젝트 칸반을 상태 컬럼별로 본다 — 보드가 여러 개면 전부"
        " 반환(board 이름으로 좁힘). project는 본인 담당 회사 프로젝트 이름. 카드"
        " short_id로 ticket_board_move를 호출할 수 있다.",
    )
    def ticket_board(project: str, board: str | None = None) -> dict:
        from orthus.mcp.tickets import resolve_kanban_boards

        try:
            client = _central()
            # 프로젝트에 보드가 여러 개일 수 있다 — board 미지정이면 전부 반환.
            boards = resolve_kanban_boards(client, project, board=board)
            snapshots = [kanban_snapshot(client, b) for b in boards]
            return {"project": snapshots[0]["project"], "boards": snapshots}
        except TicketUXError as exc:
            raise _ticket_tool_error(exc) from None
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="ticket_board_add",
        description="[central] 프로젝트 칸반에 카드 추가. status/select 값은 옵션 이름"
        "(공백 무시, 예: 시작전; open/done 등 영어는 컬럼 그룹 자동 매핑), assignee는 팀 멤버 이름,"
        " priority는 urgent/u 등도 보드 옵션으로 자동 매핑, sets는 ['우선순위=높음'] 형식,"
        " due는 today|+3d|YYYY-MM-DD, body는 markdown 본문. status 생략 시 첫 컬럼(시작 전).",
    )
    def ticket_board_add(
        project: str,
        title: str,
        status: str | None = None,
        assignee: str | None = None,
        priority: str | None = None,
        due: str | None = None,
        sets: list[str] | None = None,
        body: str | None = None,
        board: str | None = None,
    ) -> dict:
        try:
            client = _central()
            resolved = resolve_kanban_board(client, project, board=board)
            props = build_board_row_props(
                client,
                resolved,
                title=title,
                status=status,
                assignee=assignee,
                priority=priority,
                due=due,
                sets=sets,
            )
            payload: dict[str, Any] = {"props": props}
            if body:
                payload["body"] = body
            return client.database_row_create(str(resolved["database_id"]), payload)
        except TicketUXError as exc:
            raise _ticket_tool_error(exc) from None
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="ticket_board_move",
        description="[central] 칸반 카드 이동/갱신 — card는 short_id prefix(≥4자) 또는 제목"
        " 일부. 넘긴 필드만 바뀐다(status/assignee/due/sets). 삭제 도구는 없다.",
    )
    def ticket_board_move(
        project: str,
        card: str,
        status: str | None = None,
        assignee: str | None = None,
        priority: str | None = None,
        due: str | None = None,
        sets: list[str] | None = None,
        board: str | None = None,
    ) -> dict:
        try:
            client = _central()
            resolved = resolve_kanban_board(client, project, board=board)
            bundle = client.database_bundle(str(resolved["database_id"])) or {}
            schema = bundle.get("database") or resolved
            title_prop = board_prop(schema, prop_type="title")
            row = resolve_board_row(
                list(bundle.get("rows") or []),
                card,
                title_prop_id=str(title_prop["id"]) if title_prop else None,
            )
            props = build_board_row_props(
                client,
                schema,
                status=status,
                assignee=assignee,
                priority=priority,
                due=due,
                sets=sets,
                base=row.get("props") or {},
            )
            return client.database_row_update(
                str(resolved["database_id"]), str(row["row_id"]), {"props": props}
            )
        except TicketUXError as exc:
            raise _ticket_tool_error(exc) from None
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="wiki_update_candidate",
        description="[central] Submit an owner-scoped wiki update candidate (open_question task).",
    )
    def wiki_update_candidate(slug: str, note: str, evidence_urls: list[str] | None = None) -> dict:
        try:
            return _central().wiki_update_candidate(slug, note, evidence_urls or [])
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="submit_email_draft",
        description=(
            "[central] Propose an email as a REVIEW DRAFT (never sent directly). "
            "Routes through the deterministic policy gate → draft_for_review; the "
            "owner approves from Telegram. Use for 'email X about Y' requests. "
            "'recipient' should be an email address or a known team member name — "
            "extract it from the user's request yourself. Leave it blank ONLY when "
            "the user truly gave no recipient; the server then stops the draft as "
            "request_more_data and asks who to send to. 'instruction' is what the "
            "email should say (subject/body intent)."
        ),
    )
    def submit_email_draft(instruction: str, recipient: str = "") -> dict:
        # P10 spec §13 Finding 3: 구조화 kind=email_draft slot — NL 문자열을 서버
        # 분류기에 되태우지 않는다. recipient 추출 책임은 이 tool을 호출한 에이전트에
        # 있고, outcome은 서버의 결정론 P3 policy gate가 정한다(draft-only 불변).
        payload = {
            "kind": "email_draft",
            "recipient": recipient or None,
            "instruction": instruction,
        }
        try:
            return _central().gateway_submit_action(payload)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="delegate_task",
        description=(
            "[central] Delegate a task to a teammate through the deterministic "
            "policy gate (their enrolled agent daemon runs it). Use for 'ask X to "
            "…' / 'add backlog for X'. 'assignee' is a teammate email; blank means "
            "yourself. 'mode' is 'knowledge' (default) or 'code' (needs a repo path)."
        ),
    )
    def delegate_task(instruction: str, assignee: str = "", mode: str = "knowledge") -> dict:
        payload = {
            "kind": "delegate",
            "instruction": instruction,
            "assignee": assignee,
            "mode": mode,
            "runner": "codex",
        }
        try:
            return _central().gateway_submit_action(payload)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="agent_work_list",
        description="[central] List the caller's Agent Work items (optionally by state).",
    )
    def agent_work_list(state: str | None = None, limit: int = 50) -> list:
        try:
            return _central().agent_work_list(state=state, limit=limit)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="agent_work_get",
        description="[central] Get one Agent Work item by id.",
    )
    def agent_work_get(work_id: str) -> dict:
        try:
            return _central().agent_work_get(work_id)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="whoami",
        description="[central] Who am I — returns {user_id, email, role, node} for "
        "the calling token. Check role (owner/admin/member) before role-sensitive work.",
    )
    def whoami() -> dict:
        try:
            return _central().whoami()
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="kg_relations",
        description="[central] 지식 그래프에서 한 wiki 페이지의 관계를 조회한다. "
        "relation: neighbors(주변 1-2홉 지식 관계) | conflicts(이 페이지 주장들의 모순 — "
        "확정이 아닌 '충돌 가능성') | related(공유 엔티티로 엮인 다른 페이지) | path(이 페이지와 "
        "slug_b 페이지 사이 최단 지식 경로, slug_b 필수). slug 는 wiki 페이지 slug 다. 결과는 "
        "보조 컨텍스트이며 답의 근거는 wiki 본문(wiki_ask/wiki_page)을 인용하라.",
    )
    def kg_relations(
        slug: str, relation: str = "neighbors", slug_b: str | None = None, depth: int = 1
    ) -> dict:
        try:
            return _central().kg_query(relation=relation, slug=slug, slug_b=slug_b, depth=depth)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="entity_relations",
        description="[central] 한 사람/조직/시스템 이름(name)이 언급된 회사 지식을 조회한다 "
        "(누가/무엇이 어떤 페이지에서 언급됐는지). 관계를 과해석하지 말고 언급 근거 페이지를 "
        "인용하라. 회사 scope 지식만 본다.",
    )
    def entity_relations(name: str) -> dict:
        try:
            return _central().kg_query(relation="mentions", name=name)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="inbox_summary",
        description="[central] The caller's Agent Work inbox summary: per-bucket counts "
        "(wiki tasks, promote staging, data gaps, agent work) + latest items. Owner-scoped.",
    )
    def inbox_summary() -> dict:
        try:
            return _central().inbox_summary()
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="data_gaps",
        description="[central] Open data-gap backlog (questions the wiki could not ground). "
        "Pass slug to scope to one wiki page's gaps; omit for the node-scope backlog. Owner-scoped.",
    )
    def data_gaps(slug: str | None = None) -> Any:
        try:
            return _central().data_gaps(slug)
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="team_members",
        description="[central] Company team directory — list of teammates "
        "(name/role/email/projects). Use to resolve who is on the team, their role, "
        "or their email before assigning/contacting them.",
    )
    def team_members() -> dict:
        try:
            return {"members": _central().team_members()}
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="board",
        description="[central] Company kanban board (Nova 개발 로드맵) — cards with "
        "title/status/assignee/priority/due across the Todo/In Progress/Review/Done "
        "columns. Use to see roadmap/task status.",
    )
    def board() -> dict:
        try:
            return _central().board()
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="projects",
        description="[central] Company project directory — Notion db groups with their "
        "project tag and row count, plus the valid project enum. Use to see which "
        "projects exist.",
    )
    def projects() -> dict:
        try:
            return _central().projects()
        except CentralError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="mail_list",
        description="[central] 내 회사 메일함 최근 수신 목록. '내가 받은 메일', '최근 메일 "
        "10개' 류 질문에 사용 — gmail/outlook 연결을 안내하지 말 것. central에 이미 흡수된 "
        "회사 메일(nova/acme)을 owner 격리로 읽는다. mailbox=수신 메일함 주소 필터, "
        "query=제목/보낸이/본문 검색. 본문은 mail_get(message_id)로. limit 상한 30.",
    )
    def mail_list(limit: int = 10, mailbox: str | None = None, query: str | None = None) -> dict:
        capped = max(1, min(limit, MAIL_LIST_MAX_LIMIT))
        try:
            payload = _central().mail_inbox(limit=capped, search=query or None)
        except CentralError as exc:
            raise ToolError(str(exc)) from None
        wanted = (mailbox or "").strip().lower()
        rows: list[dict] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            owner = str(item.get("owner_addr") or "")
            if wanted and owner.strip().lower() != wanted:
                continue
            account_id = str(item.get("account_id") or "-")
            rows.append(
                {
                    "message_id": f"{item.get('backend')}:{account_id}:{item.get('external_id')}",
                    "from": item.get("from_addr"),
                    "subject": item.get("subject"),
                    "date": item.get("received_at") or item.get("sent_at"),
                    "mailbox": owner,
                    "read": item.get("read"),
                }
            )
            if len(rows) >= capped:
                break
        return {"count": len(rows), "mails": rows}

    @server.tool(
        name="mail_get",
        description="[central] 메일 한 통의 본문 조회. message_id는 mail_list가 돌려준 값을 "
        "그대로 사용. 본문은 2,000자에서 잘리고 truncated=true로 표시된다. 첨부는 파일명 "
        "목록만 제공한다(다운로드 미지원). 주의: 조회하면 그 메일은 메일함에서 읽음 "
        "처리된다 — 사용자가 안 읽은 척 유지를 원하면 조회 전에 알려라.",
    )
    def mail_get(message_id: str) -> dict:
        parts = (message_id or "").split(":", 2)
        if len(parts) != 3 or not parts[0] or not parts[2]:
            raise ToolError(
                "invalid message_id — mail_list가 반환한 message_id(backend:account:id)를 "
                "그대로 넘기세요"
            )
        backend, account_id, external_id = parts
        try:
            email = _central().mail_message(
                backend=backend,
                external_id=external_id,
                account_id=None if account_id in ("", "-", "None") else account_id,
            )
        except CentralError as exc:
            raise ToolError(str(exc)) from None
        body = str(email.get("body_text") or "") or str(email.get("body_html") or "")
        truncated = len(body) > MAIL_BODY_TRUNCATE_CHARS
        attachments = [
            str(att.get("filename") or "")
            for att in email.get("attachments") or []
            if isinstance(att, dict)
        ]
        return {
            "message_id": message_id,
            "from": email.get("from_addr"),
            "to": email.get("to_addr"),
            "subject": email.get("subject"),
            "date": email.get("received_at") or email.get("sent_at"),
            "mailbox": email.get("owner_addr"),
            "body": body[:MAIL_BODY_TRUNCATE_CHARS],
            "truncated": truncated,
            "attachments": attachments,
            "attachments_note": "첨부 다운로드는 미지원 — 파일명만 제공",
        }

    # --- HITL ask_user (env-gated: interactive /ask cli agent only) -----------
    # ORTHUS_MCP_ASK_USER is set SOLELY by agent_runner._provision_mcp for the inline
    # /ask cli agent, gated on agent_hitl_enabled. Delegation / gateway / the `orthus`
    # CLI never set it, so a headless dispatched agent never sees ask_user and can't
    # park a turn on a question nobody answers. Absent env → not registered, so the
    # default 38-tool smoke set (EXPECTED_MCP_TOOLS / test_mcp_server) is unchanged.
    # The tool makes NO central call — it's a pure structured signal; the /ask
    # adapter harvests the question from the tool_use input in the claude stream
    # (cli_agent.extract_ask_user), so this handler just tells the model to stop.
    if os.environ.get("ORTHUS_MCP_ASK_USER") == "1":

        @server.tool(
            name="ask_user",
            description="[local] 작업을 이어가려면 사용자에게 반드시 필요한 정보가 없을 때만"
            "(받는사람·대상·모호한 선택 등) 이 도구로 딱 한 번 되묻는다. 추측 금지. 호출하면 그"
            " 질문이 사용자에게 표시되고 이 턴은 답변 대기로 끝나니, 호출 뒤 추가 도구 호출 없이"
            " 짧게 마무리한다. input_type: text(자유입력)/choice(후보선택)/approval(예·아니오),"
            " options 는 choice·approval 일 때만.",
        )
        def ask_user(
            question: str,
            input_type: str = "text",
            options: list[str] | None = None,
        ) -> dict:
            return {
                "status": "asked",
                "note": "질문이 사용자에게 전달되었습니다. 추가 도구 호출 없이 이 턴을 마치세요.",
            }

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
