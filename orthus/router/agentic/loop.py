"""Inline agentic /ask orchestration.

`run_agentic_answer` drives a Solar function-calling loop (OpenAI-compatible
`tools`, `OpenAIChat.run_tool_loop`) whose tools wrap the existing
wiki/structured/calendar backends, and maps the outcome back onto the
`RoutedAnswer` envelope the FE + the 422 rule already understand. It owns the
`router.answer` audit span (so the legacy path's span is NOT also opened) and
publishes live frames to the per-request SSE stream as tools fire.

This runs in FastAPI's sync-endpoint threadpool thread (off the event loop), so
streaming frames go through `agent_stream.publish_threadsafe`, which marshals the
`asyncio.Queue` writes back onto the captured loop.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from orthus.agentwork import stream as agent_stream
from orthus.audit import audit
from orthus.models.base import ChatModel
from orthus.router.agentic import tools as agentic_tools
from orthus.schemas.canonical import (
    ChatTurn,
    MailComposeDraft,
    RoutedAnswer,
    UserInputOption,
    UserInputRequest,
    WikiAnswer,
)
from orthus.settings import get_settings


def _system_prompt(
    today: date,
    *,
    can_submit_candidate: bool = False,
    mail_search: bool = False,
    mail_compose: bool = False,
    kg_relations: bool = False,
    meta_tools: bool = False,
    company_directory: bool = False,
    ask_user: bool = False,
    ask_user_marker: bool = False,
) -> str:
    base = (
        f"오늘 날짜는 {today.isoformat()} 이다. 너는 회사 지식·일정 비서다. "
        "아래 도구로만 사실을 확인해 한국어로 간결하고 정확하게 답한다.\n"
        "- team_schedule: 팀 캘린더 일정 조회(날짜 범위). '이번주/오늘' 같은 표현은 "
        "오늘 날짜 기준으로 네가 직접 계산해 since/until 에 넣는다.\n"
        "- wiki_ask: 회사 위키 지식 질문(그라운딩된 답).\n"
        "- structured: 집계/목록/표 형태 데이터 질의(자연어만, SQL 직접 작성 금지).\n"
    )
    directory = ""
    if company_directory:
        directory = (
            "- team_members: 회사 팀원 목록(이름/직책/부서/이메일). 팀원·역할·연락처 질문에 쓴다.\n"
            "- board: 회사 칸반 보드(Nova 로드맵) 카드 상태/담당자/마감 조회.\n"
            "- projects: 회사 프로젝트 목록(노션 DB 그룹별 프로젝트·행수).\n"
        )
    candidate = ""
    if can_submit_candidate:
        candidate = (
            "- wiki_update_candidate: 사용자가 회사 위키 내용을 추가/수정/정정/보완해 달라고 하면 "
            "이 도구로 검토 후보를 제출한다(slug=대상 페이지, note=제안 내용, evidence_urls=근거 URL). "
            "이건 즉시 발행이 아니라 owner/admin 검토 큐에 올라가는 후보이므로, 다시 확인을 묻지 말고 "
            "바로 제출한 뒤 '검토 후보로 제출했다(즉시 반영 아님)'고 사실대로 답한다. 대상 slug가 "
            "불확실하면 wiki_search 로 먼저 찾는다.\n"
        )
    mail = ""
    if mail_search:
        mail += (
            "- mail_search: 내 회사 메일함을 제목/발신자로 검색해 수신·답장·스레드를 확인한다. "
            "'내가 쓴 …메일 답장 왔어?' 같은 메일함 확인 질문은 추측하지 말고 이 도구로 본다.\n"
        )
    if mail_compose:
        mail += (
            "- mail_compose: 메일 작성/답장 요청 시 발송 전 초안을 만든다(to/subject/body). "
            "이건 즉시 발송이 아니라 사용자가 검토 후 직접 '보내기'를 누르는 초안이다. 한 번 "
            "호출해 초안을 만든 뒤 '초안을 작성했습니다. 확인 후 보내기를 누르세요'라고만 답하고 "
            "재확인을 묻지 마라. 받는사람 주소를 모르면 먼저 mail_search 로 찾는다.\n"
        )
    kg = ""
    if kg_relations:
        kg = (
            "- kg_relations: 지식 그래프에서 한 wiki 페이지의 관계를 조회한다(relation="
            "neighbors/conflicts/related/path, slug 필요). 모순(conflicts)은 확정이 아니라 "
            "'충돌 가능성'으로 전달하고, 관계의 근거는 wiki_ask 로 확인해 인용한다.\n"
            "- entity_relations: 한 사람/조직 이름이 언급된 회사 지식을 조회한다(name). 관계를 "
            "과해석하지 말고 언급 근거 페이지를 인용한다.\n"
        )
    meta = ""
    if meta_tools:
        meta = (
            "- inbox_summary: 내 Agent Work 수신함 버킷별 건수/최근 항목 요약. '내 할 일/검토 "
            "큐?' 같은 질문에 쓴다.\n"
            "- data_gaps: 위키가 채우지 못한 미해결 데이터 갭 백로그(slug 주면 그 페이지 갭만). "
            "'어떤 정보가 비어 있어?' 같은 질문에 쓴다. 이 둘은 보조 컨텍스트지 답변 근거 자체는 "
            "아니다.\n"
        )
    ask = ""
    if ask_user:
        ask = (
            "- ask_user: 작업을 이어가려면 사용자에게 반드시 필요한 정보가 있을 때만 쓴다"
            "(받는사람·대상·모호한 선택 등). 추측하지 말고 딱 한 번만 물어라. 호출하면 턴이 "
            "끝나고 사용자 답변으로 새 턴이 시작되니, 호출 뒤 추가 도구 호출 없이 짧게 마친다.\n"
        )
    if ask_user_marker:
        # cli engine has no in-process ask_user tool — it signals a question by
        # ending its final answer with a marker line the adapter parses out. Be
        # forceful + few-shot: the natural instinct is to ask in prose, but only the
        # marker turns it into a structured, parked, button-rendered question.
        ask = (
            "사용자에게 되묻기(중요): 작업을 이어가려면 반드시 필요한 정보가 없을 때"
            "(받는사람·대상·모호한 선택 등), 그 질문을 **본문 산문으로만 묻지 말고** 반드시 "
            "최종 답변 맨 끝에 아래 마커 한 줄을 그대로 붙여서 물어라. 마커가 있어야만 사용자에게 "
            "선택 버튼으로 표시되고 답변 대기 상태가 된다:\n"
            '[[ASK_USER]]{"question":"물을 내용","input_type":"text|choice|approval",'
            '"options":["선택1","선택2"]}\n'
            "input_type 은 자유 입력이면 text, 후보 중 고르면 choice, 예/아니오 승인이면 approval "
            "로 하고 options 는 choice/approval 일 때만 넣는다. 질문은 한 번만, 되물을 필요가 "
            "없으면 마커를 붙이지 않는다.\n"
            "예) 사용자: '팀원한테 회의 요약 메일 보내줘' (받는사람 불명) → 최종 답변: "
            "받는 사람을 알려주세요.\n"
            '[[ASK_USER]]{"question":"누구에게 보낼까요?","input_type":"text"}\n'
        )
    rules = (
        "규칙: 추측하지 말 것. 일정·지식·수치·메일은 반드시 해당 도구로 확인한 뒤 답한다. "
        "도구 결과가 비어 있으면 비어 있다고 사실대로 답한다. 최종 답변은 사용자 질문 "
        "언어(한국어)로, 도구 원본을 그대로 덤프하지 말고 요약해서 전달한다."
    )
    return base + directory + candidate + mail + kg + meta + ask + rules


def _build_ask_user_request(
    raw: dict,
    *,
    question: str,
    scope: str,
    project: str | None,
    context_wiki_slug: str | None,
) -> UserInputRequest:
    """Normalize a raw ask_user payload (in-process tool input) into
    a UserInputRequest carrying the resume context the route needs to rebuild the
    turn. options→choice inference matches both engines."""
    q = str(raw.get("question") or "").strip()
    raw_options = raw.get("options")
    options = [
        UserInputOption(value=str(o))
        for o in (raw_options if isinstance(raw_options, list) else [])
        if str(o).strip()
    ]
    input_type = str(raw.get("input_type") or "").strip()
    if input_type not in {"text", "choice", "approval"}:
        # Unspecified/invalid: infer choice from options, else free text.
        input_type = "choice" if options else "text"
    return UserInputRequest(
        question=q or "추가 정보가 필요합니다.",
        input_type=input_type,  # type: ignore[arg-type]
        options=options,
        origin="agentic",
        status="waiting",
        resume={
            "original_question": question,
            "scope": scope,
            "project": project,
            "context_wiki_slug": context_wiki_slug,
        },
    )


def run_agentic_answer(
    user_id: UUID,
    question: str,
    *,
    scope: str = "all",
    project: str | None = None,
    chat_model: ChatModel | None = None,
    context_wiki_slug: str | None = None,
    history: list[ChatTurn] | None = None,
    stream_id: str | None = None,
    agent_chat,
) -> RoutedAnswer:
    settings = get_settings()
    node_id = settings.node_id
    stream_key = f"{user_id}:{stream_id}" if stream_id else None

    structured_results = []  # list[AssistantResult] in call order
    wiki_answers = []  # list[WikiAnswer] in call order
    mail_drafts: list[MailComposeDraft] = []  # NON-sent compose drafts in call order
    user_input_requests: list[UserInputRequest] = []  # HITL questions in call order
    tools_used: list[str] = []

    def on_event(frame: dict) -> None:
        if stream_key is not None:
            agent_stream.publish_threadsafe(stream_key, frame)

    def emit(text: str) -> None:
        on_event({"type": "agent_output", "chunk": text})

    def dispatch(name: str, raw_input: dict) -> str:
        tools_used.append(name)
        if name == "team_schedule":
            emit("[도구: 팀 일정 조회]\n")
            events = agentic_tools.call_team_schedule(node_id, raw_input)
            if not events:
                return "해당 기간 팀 일정 없음."
            lines = [
                f"- {e['date']}{('~' + e['end_date']) if e.get('end_date') else ''} "
                f"{e['title']}" + (f" @{e['location']}" if e.get("location") else "")
                for e in events
            ]
            return "\n".join(lines)
        if name == "wiki_ask":
            emit("[도구: 위키 조회]\n")
            wa = agentic_tools.call_wiki_ask(
                user_id,
                raw_input,
                scope=scope,
                project=project,
                chat_model=chat_model,
                context_wiki_slug=context_wiki_slug,
                history=history,
            )
            wiki_answers.append(wa)
            return wa.answer or "위키에서 근거를 찾지 못함."
        if name == "structured":
            emit("[도구: 데이터 질의]\n")
            res = agentic_tools.call_structured(
                user_id, raw_input, scope=scope, project=project, chat_model=chat_model
            )
            structured_results.append(res)
            return agentic_tools.structured_result_for_model(res)
        if name == "team_members":
            emit("[도구: 팀원 목록]\n")
            return agentic_tools.team_members_result_for_model(
                agentic_tools.call_team_members(node_id)
            )
        if name == "board":
            emit("[도구: 보드 조회]\n")
            return agentic_tools.board_result_for_model(agentic_tools.call_board(user_id))
        if name == "projects":
            emit("[도구: 프로젝트 목록]\n")
            return agentic_tools.projects_result_for_model(agentic_tools.call_projects())
        if name == "mail_search":
            emit("[도구: 메일 검색]\n")
            rows = agentic_tools.call_mail_search(user_id, raw_input, settings=settings)
            return agentic_tools.mail_search_result_for_model(rows)
        if name == "mail_compose":
            emit("[도구: 메일 초안 작성]\n")
            draft = agentic_tools.build_mail_compose_draft(user_id, raw_input, settings=settings)
            mail_drafts.append(draft)
            return (
                "초안 작성됨 (발송 전 검토 대기 — 사용자가 직접 보내기를 눌러야 발송된다): "
                f"from={draft.from_addr or '(미설정)'} to={draft.to or '(미입력)'} "
                f"제목={draft.subject or '(없음)'}"
            )
        if name == "kg_relations":
            emit("[도구: 관계 조회]\n")
            res = agentic_tools.call_kg_relations(user_id, raw_input)
            return agentic_tools.kg_relation_result_for_model(res)
        if name == "entity_relations":
            emit("[도구: 엔티티 언급 조회]\n")
            res = agentic_tools.call_entity_relations(user_id, raw_input)
            return agentic_tools.kg_relation_result_for_model(res)
        if name == "inbox_summary":
            emit("[도구: 수신함 요약]\n")
            summary = agentic_tools.call_inbox_summary(user_id, settings=settings)
            return agentic_tools.inbox_summary_result_for_model(summary)
        if name == "data_gaps":
            emit("[도구: 데이터 갭 조회]\n")
            rows = agentic_tools.call_data_gaps(user_id, raw_input)
            return agentic_tools.data_gaps_result_for_model(rows)
        if name == "ask_user":
            # HITL: end the turn with a structured question (mail_compose pattern —
            # record it, tell the model to stop). The route persists it as a
            # user_input_request message and resumes on the answer.
            emit("[사용자에게 질문]\n")
            user_input_requests.append(
                _build_ask_user_request(
                    raw_input,
                    question=question,
                    scope=scope,
                    project=project,
                    context_wiki_slug=context_wiki_slug,
                )
            )
            return "질문을 사용자에게 전달했다. 추가 도구 호출 없이 짧게 턴을 마쳐라."
        return f"[알 수 없는 도구: {name}]"

    # The engine is the in-process Solar function-calling loop (registry.
    # get_agent_chat_model): the model orchestrates, the deterministic tool
    # backends own correctness. The in-process toolset has no candidate tool
    # (external agents reach wiki_update_candidate via orthus-mcp), so don't
    # advertise it here.
    inprocess = True
    can_submit_candidate = not inprocess
    # mail_compose additionally requires the send surface to exist (company node
    # + send kill switch) so the draft's 보내기 button has a live POST /mail/send.
    mail_search_on = inprocess
    mail_compose_on = inprocess and settings.mail_send_enabled and settings.node_kind == "company"
    # KG relation tools are gated on KG availability so a node with KG off never
    # advertises a tool that always returns supported:false.
    from orthus.kg.client import kg_available, kg_enabled

    kg_relations_on = inprocess and kg_enabled() and kg_available()
    # inbox_summary / data_gaps: low-risk + owner-scoped, so always advertised —
    # no extra send/role gate.
    meta_tools_on = inprocess
    # Company directory/structure reads (team_members/board/projects) are
    # company-scope data, so advertise only on the company node. Low-risk
    # read-only, no extra flag.
    company_directory_on = inprocess and settings.node_kind == "company"
    # HITL ask_user: an in-process tool (dispatch records the question), fail-closed
    # on ORTHUS_AGENT_HITL_ENABLED (getattr default False keeps older test settings
    # stubs without the field working).
    hitl_on = getattr(settings, "agent_hitl_enabled", False)
    ask_user_tool_on = inprocess and hitl_on
    ask_user_marker_on = (not inprocess) and hitl_on
    tool_specs = list(agentic_tools.TOOL_SPECS)
    if mail_search_on:
        tool_specs.append(agentic_tools.MAIL_SEARCH_SPEC)
    if mail_compose_on:
        tool_specs.append(agentic_tools.MAIL_COMPOSE_SPEC)
    if kg_relations_on:
        tool_specs.append(agentic_tools.KG_RELATIONS_SPEC)
        tool_specs.append(agentic_tools.ENTITY_RELATIONS_SPEC)
    if meta_tools_on:
        tool_specs.append(agentic_tools.INBOX_SUMMARY_SPEC)
        tool_specs.append(agentic_tools.DATA_GAPS_SPEC)
    if company_directory_on:
        tool_specs.append(agentic_tools.TEAM_MEMBERS_SPEC)
        tool_specs.append(agentic_tools.BOARD_SPEC)
        tool_specs.append(agentic_tools.PROJECTS_SPEC)
    if ask_user_tool_on:
        tool_specs.append(agentic_tools.ASK_USER_SPEC)

    with audit("router.answer") as span:
        span.add_meta(mode="agentic", classified_route="agentic", agentic=True)
        try:
            final_text = agent_chat.run_tool_loop(
                system=_system_prompt(
                    date.today(),
                    can_submit_candidate=can_submit_candidate,
                    mail_search=mail_search_on,
                    mail_compose=mail_compose_on,
                    kg_relations=kg_relations_on,
                    meta_tools=meta_tools_on,
                    company_directory=company_directory_on,
                    ask_user=ask_user_tool_on,
                    ask_user_marker=ask_user_marker_on,
                ),
                question=question,
                tools=tool_specs,
                dispatch=dispatch,
                on_event=on_event,
            )
        finally:
            if stream_key is not None:
                agent_stream.publish_threadsafe(stream_key, {"type": "turn_complete"})
        final_text = (final_text or "").strip()
        span.add_meta(tools_used=list(tools_used))

        # cli engine: the ask_user request rides out on a final-answer marker the
        # adapter already parsed + stripped (self.pending_user_input). Honor it only
        # when the cli HITL instruction was advertised (flag on) — flag off never
        # emits the marker, so this is inert. The adapter strips the marker from
        # final_text, so the visible answer is clean either way.
        if ask_user_marker_on and not user_input_requests:
            cli_pending = getattr(agent_chat, "pending_user_input", None)
            if isinstance(cli_pending, dict) and str(cli_pending.get("question") or "").strip():
                user_input_requests.append(
                    _build_ask_user_request(
                        cli_pending,
                        question=question,
                        scope=scope,
                        project=project,
                        context_wiki_slug=context_wiki_slug,
                    )
                )

        # HITL: the model asked the chat owner for required info — the turn ends as a
        # question (highest priority, before any answer/reject shaping). The route
        # persists it as a user_input_request message and resumes on the answer.
        if user_input_requests:
            span.add_meta(mode="user_input", user_input_requested=True)
            return RoutedAnswer(
                question=question,
                mode="user_input",
                user_input_request=user_input_requests[-1],
                stream_id=stream_id,
            )

        # 422-preserving case: the whole answer is a bare gate-rejected structured
        # query (no wiki grounding, no synthesized prose). Surface it as the legacy
        # structured-rejected envelope so ask.py still returns 422 + compiled SQL.
        rejected = [r for r in structured_results if r.status == "rejected"]
        if rejected and not wiki_answers and not final_text:
            span.add_meta(mode="structured", served="structured_rejected")
            return RoutedAnswer(
                question=question,
                mode="structured",
                structured=rejected[-1],
                stream_id=stream_id,
            )

        # in-process engine: sources from the wiki_ask tool. (외부 어댑터 사용 시:
        # wiki_answers is empty (tools are MCP-side), so use the citations the cli
        # adapter harvested from claude's wiki MCP tool_result blocks.
        cli_sources = list(getattr(agent_chat, "last_citations", None) or [])
        sources = [s for wa in wiki_answers for s in wa.sources] or cli_sources
        if not final_text:
            final_text = "도구로 확인했지만 답할 근거를 찾지 못했습니다."
        # A structured tool was gate-rejected but the served answer is prose (the model
        # called other tools / wrote a summary), so it did NOT take the bare-422 path.
        # Surface the rejection as a warning so the reject signal isn't silently lost.
        warnings = list(wiki_answers and wiki_answers[-1].warnings or [])
        for r in rejected:
            reason = r.message or getattr(r.validation, "rejected_reason", None) or "rejected"
            warnings.append(f"structured_rejected:{reason}")
        wiki = WikiAnswer(question=question, answer=final_text, sources=sources, warnings=warnings)
        return RoutedAnswer(
            question=question,
            mode="wiki",
            wiki=wiki,
            warnings=warnings,
            stream_id=stream_id,
            mail_draft=mail_drafts[-1] if mail_drafts else None,
        )
