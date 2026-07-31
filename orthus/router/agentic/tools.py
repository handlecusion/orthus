"""In-process tool adapters for the inline agentic /ask loop.

Each tool wraps an EXISTING backend function directly (no HTTP, no MCP
round-trip — the loop runs inside the central API process). The Solar
Converse `toolSpec` schemas live alongside the callables so the loop and the
spec can never drift.

Grounding/gate invariants are upheld by delegation, not re-implemented here:
- `wiki_ask` -> `orthus.wiki.qa.ask` (compiled-wiki grounding only).
- `structured` -> `orthus.structured.query.query_structured` (the sqlglot
  5-reject gate runs INSIDE; the model passes NL, never SQL, and a rejected
  query comes back as `status="rejected"` text — the model never executes it).
- `team_schedule` -> `orthus.dashboard.list_calendar` (read-only calendar rows).
"""

from __future__ import annotations

import asyncio
from datetime import date
from uuid import UUID

from sqlalchemy import func, select

from orthus.board import list_board
from orthus.connectors.account_config import account_settings
from orthus.connectors.project_map import PROJECTS
from orthus.dashboard import list_calendar, list_team_members
from orthus.db import session
from orthus.mail import list_unified_inbox
from orthus.mail.backends import _mail_account_rows, _scalar_setting
from orthus.models.base import ChatModel
from orthus.schemas.canonical import (
    AssistantResult,
    ChatTurn,
    MailComposeDraft,
    MailInboxResponse,
    WikiAnswer,
)
from orthus.settings import Settings, get_settings
from orthus.structured.query import query_structured
from orthus.tables import notion_rows, project_overrides
from orthus.wiki.qa import ask as wiki_ask_backend

# Caps so a wide company directory/board can't blow the model context window.
_TEAM_MEMBER_CAP = 60
_BOARD_CARD_CAP = 60
_PROJECT_GROUP_CAP = 60

# Cap rows handed back to the model so a wide structured result can't blow the
# context window; the authoritative AssistantResult (full rows) still rides the
# RoutedAnswer when the structured branch is the served answer.
_STRUCTURED_ROW_CAP = 20

# Tool spec list ({name, description, input_schema}) — input_schema is a plain JSON Schema
# translated to the OpenAI-compatible `tools` format by run_tool_loop.
TOOL_SPECS: list[dict] = [
    {
        "name": "team_schedule",
        "description": (
            "회사 팀 캘린더에서 일정을 조회한다. since/until 은 YYYY-MM-DD 날짜 "
            "범위(둘 다 선택). '이번주' 같은 표현은 호출 전에 오늘 날짜 기준으로 "
            "직접 계산해 넣어라."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "YYYY-MM-DD 시작일(포함)"},
                "until": {"type": "string", "description": "YYYY-MM-DD 종료일(포함)"},
            },
            "required": [],
        },
    },
    {
        "name": "wiki_ask",
        "description": (
            "회사 위키(컴파일된 지식)에 자연어로 질문해 그라운딩된 답을 받는다. "
            "사실·정책·맥락 질문에 사용한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "위키에 물어볼 자연어 질문"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "structured",
        "description": (
            "구조화 데이터(노션 행 저장소)에 자연어 질의로 집계/목록/표를 얻는다. "
            "건수·합계·필터 목록처럼 표 형태 답이 필요할 때 사용한다. 검증 게이트가 "
            "내부에서 SELECT 전용으로 강제되므로 SQL 을 직접 쓰지 말고 자연어만 넘겨라."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "구조화 질의 자연어"},
            },
            "required": ["question"],
        },
    },
]


# --- company directory/structure (read-only, low-risk) -------------------------
# team_members / board / projects expose company-scope read context (NOT a
# grounding source — design principle 7 keeps grounding on compiled wiki). Each
# wraps an existing backend in-process and caps rows. No arguments: the company
# directory is small and fully node-scoped.

TEAM_MEMBERS_SPEC: dict = {
    "name": "team_members",
    "description": (
        "회사 팀원 목록을 조회한다. 이름·직책·부서·이메일·소속 프로젝트를 본다. "
        "'우리 팀에 누구 있어?' '…담당이 누구야?' '…한테 메일 보낼 주소' 처럼 팀원/역할/"
        "연락처를 확인할 때 쓴다."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

BOARD_SPEC: dict = {
    "name": "board",
    "description": (
        "회사 칸반 보드(Nova 개발 로드맵)를 조회한다. 카드별 제목·상태(Todo/In "
        "Progress/Review/Done)·담당자·우선순위·마감을 본다. '로드맵 어디까지 됐어?' "
        "'…작업 상태' 처럼 보드/태스크 진행 상태를 확인할 때 쓴다."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}

PROJECTS_SPEC: dict = {
    "name": "projects",
    "description": (
        "회사 프로젝트 목록을 조회한다. 노션 DB 그룹별 프로젝트 태그와 행 수, 유효한 "
        "프로젝트 enum을 본다. '어떤 프로젝트가 있어?' 처럼 프로젝트 구성을 확인할 때 쓴다."
    ),
    "input_schema": {"type": "object", "properties": {}, "required": []},
}


def call_team_members(node_id: str) -> list[dict]:
    """Company team directory; project only low-risk identity/role fields (no PII)."""
    members = list_team_members(node_id)
    out: list[dict] = []
    for m in members[:_TEAM_MEMBER_CAP]:
        if not m.active:
            continue
        out.append(
            {
                "name": m.name,
                "title": m.title,
                "department": m.department,
                "email": m.email,
            }
        )
    return out


def team_members_result_for_model(rows: list[dict]) -> str:
    """Compact, model-facing rendering of the team directory."""
    if not rows:
        return "팀원 정보 없음 (0명)."
    lines: list[str] = []
    for r in rows:
        bits = [r.get("name") or "?"]
        if r.get("title"):
            bits.append(r["title"])
        if r.get("department"):
            bits.append(r["department"])
        if r.get("email"):
            bits.append(r["email"])
        lines.append("- " + " · ".join(bits))
    return "\n".join(lines)


def call_board(user_id: UUID) -> list[dict]:
    """Company kanban cards; project the model-relevant fields, rows capped."""
    cards = list_board(user_id)
    out: list[dict] = []
    for c in cards[:_BOARD_CARD_CAP]:
        out.append(
            {
                "title": c.title,
                "status": c.status,
                "assignee": c.assignee,
                "priority": c.priority,
                "due": c.due_at.date().isoformat() if c.due_at else None,
            }
        )
    return out


def board_result_for_model(rows: list[dict]) -> str:
    """Compact, model-facing rendering of the kanban board."""
    if not rows:
        return "보드 카드 없음 (0건)."
    lines: list[str] = []
    for r in rows:
        bits = [f"[{r.get('status') or '?'}]", r.get("title") or "(제목 없음)"]
        if r.get("assignee"):
            bits.append(f"담당={r['assignee']}")
        if r.get("priority"):
            bits.append(f"우선={r['priority']}")
        if r.get("due"):
            bits.append(f"마감={r['due']}")
        lines.append("- " + " ".join(bits))
    return "\n".join(lines)


def call_projects() -> dict:
    """Project directory: Notion db_name groups + project tag + count (in-process)."""
    with session() as s:
        rows = s.execute(
            select(
                notion_rows.c.db_name,
                notion_rows.c.project,
                func.count().label("count"),
            )
            .group_by(notion_rows.c.db_name, notion_rows.c.project)
            .order_by(notion_rows.c.db_name, notion_rows.c.project)
        ).all()
        overridden = {r[0] for r in s.execute(select(project_overrides.c.db_name)).all()}
    groups = [
        {
            "db_name": db_name,
            "project": project,
            "count": count,
            "source": "override" if db_name in overridden else "default",
        }
        for db_name, project, count in rows[:_PROJECT_GROUP_CAP]
    ]
    return {"projects": list(PROJECTS), "groups": groups}


def projects_result_for_model(result: dict) -> str:
    """Compact, model-facing rendering of the project directory."""
    groups = result.get("groups") or []
    if not groups:
        return "프로젝트 그룹 없음 (0건)."
    lines = [f"유효 프로젝트: {', '.join(result.get('projects') or [])}"]
    for g in groups:
        lines.append(
            f"- {g.get('db_name') or '?'} → {g.get('project') or '?'} "
            f"({g.get('count')}행, {g.get('source')})"
        )
    return "\n".join(lines)


def _parse_date(value: object) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def call_team_schedule(node_id: str, raw_input: dict) -> list[dict]:
    """Read calendar events in [since, until]; serialize the model-relevant fields."""
    since = _parse_date(raw_input.get("since"))
    until = _parse_date(raw_input.get("until"))
    events = list_calendar(node_id, since, until)
    out: list[dict] = []
    for ev in events:
        out.append(
            {
                "title": ev.title,
                "date": ev.event_date.isoformat() if ev.event_date else None,
                "end_date": ev.end_date.isoformat() if ev.end_date else None,
                "type": ev.event_type,
                "location": ev.location,
            }
        )
    return out


def call_wiki_ask(
    user_id: UUID,
    raw_input: dict,
    *,
    scope: str,
    project: str | None,
    chat_model: ChatModel | None,
    context_wiki_slug: str | None,
    history: list[ChatTurn] | None,
) -> WikiAnswer:
    """Compiled-wiki grounded answer (learn=False — the loop must not mutate wiki)."""
    question = str(raw_input.get("question") or "").strip()
    return wiki_ask_backend(
        user_id,
        question,
        scope=scope,
        project=project,
        chat_model=chat_model,
        context_wiki_slug=context_wiki_slug,
        history=history,
        learn=False,
    )


def call_structured(
    user_id: UUID,
    raw_input: dict,
    *,
    scope: str,
    project: str | None,
    chat_model: ChatModel | None,
) -> AssistantResult:
    """NL->SQL through the validation gate (the gate is the sole executor decision)."""
    question = str(raw_input.get("question") or "").strip()
    return query_structured(user_id, question, scope=scope, project=project, chat_model=chat_model)


def structured_result_for_model(result: AssistantResult) -> str:
    """Compact, model-facing rendering of a structured result (rows capped)."""
    if result.status == "rejected":
        reason = result.message or getattr(result.validation, "rejected_reason", None) or "rejected"
        return f"[검증 게이트 거부] {reason}"
    rows = result.rows or []
    if not rows:
        return "결과 없음 (0행)"
    cols = result.columns or []
    shown = rows[:_STRUCTURED_ROW_CAP]
    header = " | ".join(cols) if cols else ""
    body = "\n".join(" | ".join("—" if c is None else str(c) for c in row) for row in shown)
    total = result.row_count if result.row_count is not None else len(rows)
    extra = f"\n… 외 {total - len(shown)}행 (총 {total}행)" if total > len(shown) else ""
    return f"{header}\n{body}{extra}".strip()


# --- mail (in-process engine) -------------------------------------
# These wrap the SAME read/send substrate the /mail FE uses. `mail_search` is a
# read-only proxy to the approved backends' inbox; `mail_compose` produces a NON-
# sent MailComposeDraft (the operator's 보내기 click is the only send path —
# absolute rule: no LLM-only execution). The cli engine reaches mail tools via
# orthus-mcp separately (fast-follow); they are not advertised there.

_MAIL_ROW_CAP = 12

MAIL_SEARCH_SPEC: dict = {
    "name": "mail_search",
    "description": (
        "내 회사 메일함(수신/발신)을 제목·발신자 텍스트로 검색한다. '…건으로 쓴 메일에 "
        "답장 왔어?' '…메일 온 거 있어?' 처럼 메일 수신·답장·스레드 확인에 쓴다. query 에 "
        "핵심 키워드(제목/상대 이름)를 넣어라. 메일 본문/일정/지식이 아니라 메일함 자체를 "
        "본다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "제목/발신자 검색 키워드"},
        },
        "required": ["query"],
    },
}

MAIL_COMPOSE_SPEC: dict = {
    "name": "mail_compose",
    "description": (
        "사용자가 메일을 쓰거나 답장하라고 하면 이 도구로 발송 전 초안을 만든다. "
        "to(받는사람 이메일 주소), subject(제목), body(본문, 한국어 평문)를 채운다. "
        "이건 즉시 발송이 아니라 사용자가 검토 후 직접 '보내기'를 누르는 초안이므로, 한 "
        "번 호출해 초안을 만든 뒤 '초안을 작성했습니다. 확인 후 보내기를 누르세요'라고만 "
        "답하고 재확인을 묻지 마라. 받는사람 주소를 모르면 먼저 mail_search 로 상대 주소를 "
        "찾는다. 보내는사람 주소는 서버가 자동으로 채운다(네가 정하지 않는다)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "받는사람 이메일 주소"},
            "subject": {"type": "string", "description": "메일 제목"},
            "body": {"type": "string", "description": "메일 본문(한국어 평문)"},
        },
        "required": ["to", "subject", "body"],
    },
}

# HITL: the model ends its turn by asking the chat owner one required-info question.
# Structurally the only "ask a human" tool — the orchestrator persists it as a
# user_input_request message and resumes on the answer (docs plan
# cryptic-meandering-gadget PR3). Advertised only when
# ORTHUS_AGENT_HITL_ENABLED is on.
ASK_USER_SPEC: dict = {
    "name": "ask_user",
    "description": (
        "작업을 계속하려면 사용자에게 반드시 필요한 정보가 있을 때만 이 도구를 호출한다"
        "(예: 받는사람/대상/모호한 선택). 추측하지 말고 한 번만 물어라. 호출하면 이 턴이 "
        "끝나고 사용자 답변으로 새 턴이 시작되므로, 호출 뒤 추가 도구 호출 없이 짧게 마친다. "
        "선택지가 있으면 options 에 넣고 input_type=choice, 예/아니오·승인류면 approval, "
        "그 외 자유 입력이면 text 로 둔다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "사용자에게 물을 질문(한국어)"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "선택지(있을 때만)",
            },
            "input_type": {"type": "string", "enum": ["text", "choice", "approval"]},
        },
        "required": ["question"],
    },
}


def call_mail_search(
    user_id: UUID, raw_input: dict, *, settings: Settings | None = None
) -> list[dict]:
    """Owner-isolated inbox search via the approved backends (read-only proxy).

    Runs in the loop's sync threadpool thread (off the event loop), so the async
    `list_unified_inbox` is bridged with `asyncio.run`. Owner-scoped: empty when
    no mail backend/account is configured for this owner (honest 0-row).
    """
    settings = settings or get_settings()
    query = str(raw_input.get("query") or "").strip()
    if not query:
        return []
    resp: MailInboxResponse = asyncio.run(
        list_unified_inbox(settings, owner_id=user_id, search=query, limit=_MAIL_ROW_CAP)
    )
    out: list[dict] = []
    for m in resp.items:
        ts = m.received_at or m.sent_at
        out.append(
            {
                "from": m.from_addr,
                "to": ", ".join(m.to_addr),
                "subject": m.subject,
                "direction": m.direction,
                "date": ts.isoformat() if ts else None,
                "read": m.read,
                "replied": m.replied,
                "snippet": (m.body_text or "")[:200],
            }
        )
    return out


def mail_search_result_for_model(rows: list[dict]) -> str:
    """Compact, model-facing rendering of inbox search rows."""
    if not rows:
        return "검색 결과 없음 (메일 0건 — 메일함 미설정이거나 해당 메일 없음)."
    lines: list[str] = []
    for r in rows:
        direction = "수신" if r.get("direction") == "inbound" else "발신"
        replied = " [이미 답장함]" if r.get("replied") else ""
        lines.append(
            f"- {r.get('date') or '?'} [{direction}] {r.get('from') or '?'} → "
            f"{r.get('to') or '?'}: {r.get('subject') or '(제목 없음)'}{replied}"
        )
    return "\n".join(lines)


def resolve_compose_from_addr(settings: Settings, owner_id: UUID) -> str:
    """The owner's send-from mailbox: first owned mail account row, else env owner.

    The model never supplies the sender identity; it is resolved server-side to a
    mailbox the owner owns. The actual send (`POST /mail/send`) re-validates
    ownership, so a stale/edited from_addr fails closed there.
    """
    for row in _mail_account_rows(settings, owner_id):
        owner_addr = _scalar_setting(account_settings(row).get("owner_addr")).strip()
        if owner_addr:
            return owner_addr
    return (settings.mail_acme_owner or settings.mail_nova_owner or "").strip()


def build_mail_compose_draft(
    user_id: UUID, raw_input: dict, *, settings: Settings | None = None
) -> MailComposeDraft:
    """Build a NON-sent draft (no send side effect). from_addr resolved server-side."""
    settings = settings or get_settings()
    return MailComposeDraft(
        from_addr=resolve_compose_from_addr(settings, user_id),
        to=str(raw_input.get("to") or "").strip(),
        subject=str(raw_input.get("subject") or "").strip(),
        body=str(raw_input.get("body") or "").strip(),
    )


# --- KG relations (in-process; 외부 에이전트는 orthus-mcp kg_relations 경유) ------
# 지식 그래프 read-only 게이트(`run_kg_template`)를 in-process로 호출한다. owner-scope/
# redaction/read-only 불변식은 게이트가 보유하며(orthus/kg/gate.py) 여기서 재구현하지 않는다.
# 답의 근거는 여전히 compiled wiki(wiki_ask)이고, 이 도구는 보조 관계 컨텍스트다(원칙 7).

_KG_NODE_CAP = 20

KG_RELATIONS_SPEC: dict = {
    "name": "kg_relations",
    "description": (
        "지식 그래프에서 한 wiki 페이지의 관계를 조회한다. relation: neighbors(주변 1-2홉 "
        "관계) | conflicts(이 페이지 주장들의 모순 — 확정이 아닌 '충돌 가능성') | related(공유 "
        "엔티티로 엮인 다른 페이지) | path(이 페이지와 slug_b 페이지 사이 최단 지식 경로, "
        "slug_b 필수). slug 는 wiki 페이지 slug. 결과는 보조 관계 컨텍스트이며 답의 근거는 "
        "wiki_ask 를 인용하라."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "기준 wiki 페이지 slug"},
            "relation": {
                "type": "string",
                "enum": ["neighbors", "conflicts", "related", "path"],
                "description": "관계 종류(기본 neighbors)",
            },
            "slug_b": {"type": "string", "description": "path 일 때 도착 페이지 slug"},
        },
        "required": ["slug"],
    },
}

ENTITY_RELATIONS_SPEC: dict = {
    "name": "entity_relations",
    "description": (
        "한 사람/조직/시스템 이름(name)이 언급된 회사 지식을 조회한다(누가/무엇이 어떤 "
        "페이지에서 언급됐는지). 관계를 과해석하지 말고 언급 근거 페이지를 인용하라. 회사 "
        "scope 지식만 본다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "사람/조직/시스템 이름"},
        },
        "required": ["name"],
    },
}


def call_kg_relations(user_id: UUID, raw_input: dict):
    """slug 기반 관계 질의 — run_kg_relation in-process. KgTemplateResult 반환."""
    from orthus.kg.relations import run_kg_relation

    relation = str(raw_input.get("relation") or "neighbors").strip() or "neighbors"
    slug = str(raw_input.get("slug") or "").strip() or None
    slug_b = str(raw_input.get("slug_b") or "").strip() or None
    return run_kg_relation(user_id=user_id, relation=relation, slug=slug, slug_b=slug_b)


def call_entity_relations(user_id: UUID, raw_input: dict):
    """name 기반 엔티티 언급 질의 — run_kg_relation(relation='mentions')."""
    from orthus.kg.relations import run_kg_relation

    name = str(raw_input.get("name") or "").strip() or None
    return run_kg_relation(user_id=user_id, relation="mentions", name=name)


def kg_relation_result_for_model(result) -> str:
    """KgTemplateResult → 모델용 compact 렌더(노드/관계 cap). 비-OK는 사유 한 줄."""
    from orthus.kg.gate import KgQueryStatus

    if result.status is not KgQueryStatus.OK:
        return f"[그래프 조회 결과 없음: {result.reject_reason or 'unavailable'}]"
    if not result.nodes:
        return "관계 없음 (그래프 연결 0건)."
    label_by_id = {n.id: (n.title or n.slug or n.id) for n in result.nodes}
    node_lines = [f"- {label_by_id[n.id]} ({n.label})" for n in result.nodes[:_KG_NODE_CAP]]
    out = "노드:\n" + "\n".join(node_lines)
    edge_lines = [
        f"- {label_by_id.get(e.src, e.src)} —[{e.rel}]→ {label_by_id.get(e.dst, e.dst)}"
        for e in result.edges[:_KG_NODE_CAP]
    ]
    if edge_lines:
        out += "\n관계:\n" + "\n".join(edge_lines)
    if result.truncated:
        out += "\n(일부만 표시 — 결과가 상한에서 잘림)"
    return out


# --- agent-work meta read tools (low-risk, owner-scoped) -----------------------
# `inbox_summary` and `data_gaps` are read-only auxiliary context — NOT answer
# grounding (the wiki page grounding invariant is untouched). They wrap the SAME
# owner-scoped backends the FE/orthus-mcp use, so an empty owner returns an honest
# 0-count summary / empty list. Results are capped for the model context window.

_INBOX_ITEM_CAP = 5
_DATA_GAPS_CAP = 20

INBOX_SUMMARY_SPEC: dict = {
    "name": "inbox_summary",
    "description": (
        "내 Agent Work 수신함을 요약한다. 위키 태스크·승격 스테이징·데이터 갭·에이전트 "
        "워크 버킷별 건수와 최근 항목을 본다. '내 할 일/검토 큐 뭐 있어?' 같은 질문에 쓴다. "
        "이건 보조 컨텍스트지 답변 근거 자체는 아니다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

DATA_GAPS_SPEC: dict = {
    "name": "data_gaps",
    "description": (
        "위키가 그라운딩하지 못한 미해결 데이터 갭(채워야 할 지식 백로그) 목록을 본다. "
        "slug 를 주면 그 위키 페이지에 묶인 갭만, 비우면 노드 스코프 전체 open 갭을 본다. "
        "'어떤 정보가 비어 있어?' 같은 질문에 쓴다. 보조 컨텍스트지 답변 근거가 아니다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "위키 페이지 slug(선택). 비우면 전체 백로그.",
            },
        },
        "required": [],
    },
}


def call_inbox_summary(user_id: UUID, *, settings: Settings | None = None) -> dict:
    """Owner-scoped Agent Work inbox summary, compacted for the model.

    In-process call to `build_inbox_summary` (no HTTP). Returns per-bucket counts
    plus capped latest titles so a busy inbox can't blow the context window.
    """
    # 지연 import: agentwork.service 는 무거운 의존 트리를 끌어와 tools.py import 시점에
    # 로딩 비용을 피한다(mail 백엔드 import 패턴과 동일 의도).
    from orthus.agentwork.service import build_inbox_summary

    summary = build_inbox_summary(user_id, settings=settings)
    b = summary.buckets

    def _titles(items: list, attr: str) -> list[str]:
        return [str(getattr(it, attr, "") or "") for it in items[:_INBOX_ITEM_CAP]]

    return {
        "node_kind": summary.node_kind,
        "wiki_tasks": {"count": b.wiki_tasks.count, "latest": _titles(b.wiki_tasks.items, "title")},
        "promote_staging": {
            "count": b.promote_staging.count,
            "supported": b.promote_staging.supported,
            "latest": _titles(b.promote_staging.items, "source_title"),
        },
        "data_gaps": {
            "count": b.data_gaps.count,
            "latest": _titles(b.data_gaps.items, "missing_topic"),
        },
        "agent_work": {
            "count": b.agent_work.count,
            "by_outcome": dict(b.agent_work.by_outcome),
            "latest": _titles(b.agent_work.items, "title"),
        },
    }


def call_data_gaps(user_id: UUID, raw_input: dict) -> list[dict]:
    """Owner-scoped open data-gap backlog, compacted for the model (capped rows).

    slug 가 있으면 그 위키 페이지에 묶인 open 갭만, 없으면 노드 스코프 전체 open 갭.
    """
    from orthus.wiki.gap import list_gaps, list_gaps_for_wiki_page

    slug = str(raw_input.get("slug") or "").strip()
    if slug:
        gaps = list_gaps_for_wiki_page(user_id, slug, status="open", limit=_DATA_GAPS_CAP)
    else:
        gaps = list_gaps(user_id, status="open", limit=_DATA_GAPS_CAP)
    return [
        {
            "question": g.question,
            "reason": g.reason,
            "hit_count": g.hit_count,
            "suggested_target": g.suggested_target,
            "context_wiki_slug": g.context_wiki_slug,
        }
        for g in gaps[:_DATA_GAPS_CAP]
    ]


def data_gaps_result_for_model(rows: list[dict]) -> str:
    """Compact, model-facing rendering of the data-gap backlog."""
    if not rows:
        return "미해결 데이터 갭 없음 (0건)."
    lines: list[str] = []
    for r in rows:
        target = f" → {r['suggested_target']}" if r.get("suggested_target") else ""
        lines.append(f"- ({r.get('hit_count', 0)}회) {r.get('question') or '?'}{target}")
    return "\n".join(lines)


def inbox_summary_result_for_model(summary: dict) -> str:
    """Compact, model-facing rendering of the inbox summary."""
    b = summary
    parts = [
        f"위키태스크 {b['wiki_tasks']['count']}건",
        f"승격 {b['promote_staging']['count']}건",
        f"데이터갭 {b['data_gaps']['count']}건",
        f"에이전트워크 {b['agent_work']['count']}건",
    ]
    head = "수신함 요약: " + ", ".join(parts)
    gap_titles = b["data_gaps"]["latest"]
    tail = ("\n최근 데이터갭: " + "; ".join(t for t in gap_titles if t)) if gap_titles else ""
    return head + tail
