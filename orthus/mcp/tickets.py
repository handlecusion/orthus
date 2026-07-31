"""Shared agent-UX helpers for the `orthus ticket` CLI and MCP ticket tools.

The consumers are AI agents, so every resolver here is forgiving on input
(short id prefixes, project names, bucket keys, date words, priority
abbreviations) and precise on failure: a :class:`TicketUXError` always carries
the valid values and a corrected example command so the caller can self-heal
without another round trip.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from orthus.mcp.central import CentralClient

TICKET_PRIORITIES = ("urgent", "priority", "normal", "low")
TICKET_STATUSES = ("open", "done", "archived")
BUCKET_KEYS = ("next_week", "next_month", "next_quarter", "next_year", "someday", "never")

_RELATIVE_DATE_RE = re.compile(r"^\+(\d{1,3})d$")


class TicketUXError(ValueError):
    """Actionable ticket-command error: message + valid values + example fix.

    ``note`` carries one short teaching line (왜 이런 규칙인지 / 다른 쪽 저장소에선
    뭘 쓰는지) — CLI는 NOTE 행으로, MCP는 메시지 꼬리로 보여준다."""

    def __init__(
        self,
        message: str,
        *,
        valid: list[str] | None = None,
        example: str | None = None,
        note: str | None = None,
    ) -> None:
        super().__init__(message)
        self.valid = valid
        self.example = example
        self.note = note


def short_id(task_id: Any) -> str:
    return str(task_id).replace("-", "")[:8]


def parse_date_word(value: str, *, today: date | None = None) -> str:
    """`today` / `tomorrow` / `+Nd` / `YYYY-MM-DD` → ISO date string."""
    base = today or date.today()
    word = value.strip().lower()
    if word == "today":
        return base.isoformat()
    if word == "tomorrow":
        return (base + timedelta(days=1)).isoformat()
    relative = _RELATIVE_DATE_RE.match(word)
    if relative:
        return (base + timedelta(days=int(relative.group(1)))).isoformat()
    try:
        return date.fromisoformat(word).isoformat()
    except ValueError:
        raise TicketUXError(
            f"invalid date '{value}'",
            valid=["today", "tomorrow", "+3d", "YYYY-MM-DD"],
            example='orthus ticket create "제목" --date tomorrow',
        ) from None


def normalize_priority(value: str) -> str:
    """Full names plus unambiguous first-letter abbreviations (u/p/n/l)."""
    word = value.strip().lower()
    if word in TICKET_PRIORITIES:
        return word
    matches = [p for p in TICKET_PRIORITIES if p.startswith(word)] if word else []
    if len(matches) == 1:
        return matches[0]
    raise TicketUXError(
        f"invalid priority '{value}'",
        valid=list(TICKET_PRIORITIES),
        example='orthus ticket create "제목" --priority urgent',
    )


def normalize_status(value: str) -> str:
    word = value.strip().lower()
    if word in TICKET_STATUSES:
        return word
    matches = [s for s in TICKET_STATUSES if s.startswith(word)] if word else []
    if len(matches) == 1:
        return matches[0]
    raise TicketUXError(
        f"invalid status '{value}'",
        valid=list(TICKET_STATUSES),
        example="orthus ticket list --status open",
    )


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def resolve_project(client: CentralClient, name_or_id: str) -> dict:
    """Channel name (case-insensitive; unique prefix/substring ok) or UUID → project."""
    query = name_or_id.strip()
    projects = list(client.board_projects() or [])
    if _is_uuid(query):
        for project in projects:
            if str(project.get("project_id")) == query.lower():
                return project
        raise TicketUXError(
            f"project id '{query}' not found on your board",
            valid=sorted(str(p.get("name")) for p in projects),
            example='orthus ticket create "제목" --project <이름>',
        )
    folded = query.casefold()
    exact = [p for p in projects if str(p.get("name", "")).casefold() == folded]
    if len(exact) == 1:
        return exact[0]
    prefix = [p for p in projects if str(p.get("name", "")).casefold().startswith(folded)]
    candidates = prefix or [p for p in projects if folded in str(p.get("name", "")).casefold()]
    if len(candidates) == 1:
        return candidates[0]
    names = sorted(str(p.get("name")) for p in (candidates or projects))
    if candidates:
        raise TicketUXError(
            f"project '{query}' is ambiguous ({len(candidates)} matches)",
            valid=names,
            example=f'orthus ticket create "제목" --project "{names[0]}"',
        )
    raise TicketUXError(
        f"project '{query}' not found on your board "
        "(company channels appear only for projects assigned to you)",
        valid=names,
        example="orthus ticket projects",
    )


def resolve_company_project(client: CentralClient, name_or_id: str) -> dict:
    """회사 채널만 허용하는 resolve — 프로젝트 전체 티켓 조회(project-list)용.

    개인 채널은 프로젝트 단위 집계가 없으므로 회사 채널(company_project_id 보유)로
    한정하고, 아니면 회사 채널 후보를 알려준다."""
    project = resolve_project(client, name_or_id)
    if project.get("kind") == "company" and project.get("company_project_id"):
        return project
    company_names = sorted(
        str(p.get("name")) for p in (client.board_projects() or []) if p.get("kind") == "company"
    )
    raise TicketUXError(
        f"'{project.get('name')}'은(는) 개인 채널 — 프로젝트 티켓 조회는 회사 채널만 가능",
        valid=company_names,
        example=f'orthus ticket project-list "{company_names[0]}"' if company_names else None,
    )


def resolve_bucket(client: CentralClient, key: str) -> dict:
    """Backlog bucket key (e.g. next_week) → bucket row."""
    folded = key.strip().casefold()
    buckets = list(client.board_backlog_buckets() or [])
    for bucket in buckets:
        if str(bucket.get("key", "")).casefold() == folded:
            return bucket
    raise TicketUXError(
        f"bucket '{key}' not found",
        valid=[str(b.get("key")) for b in buckets] or list(BUCKET_KEYS),
        example='orthus ticket create "제목" --bucket next_week',
    )


def resolve_kanban_boards(
    client: CentralClient, project_name: str, board: str | None = None
) -> list[dict]:
    """회사 프로젝트 이름 → 그 프로젝트의 데이터베이스(보드) 전부.

    board 이름을 주면 부분일치로 좁힌다(모호하면 후보 나열). 각 항목에
    `_project` 채널이 붙는다 — 프로젝트에 DB가 여러 개인 경우의 1급 진입점."""
    channel = resolve_company_project(client, project_name)
    databases = list(client.project_databases(str(channel["company_project_id"])) or [])
    if not databases:
        raise TicketUXError(
            f"'{channel.get('name')}' 프로젝트에 보드가 없습니다",
            example="웹 프로젝트 페이지에서 보드가 자동 생성될 때까지 한 번 열어주세요",
        )
    if board:
        folded = board.strip().casefold()
        matches = [d for d in databases if folded in str(d.get("title", "")).casefold()]
        if not matches:
            raise TicketUXError(
                f"보드 '{board}' 없음",
                valid=[str(d.get("title")) for d in databases],
                example=f'orthus ticket ls --project "{channel.get("name")}"',
                note="--board 없이 실행하면 이 프로젝트의 모든 보드를 볼 수 있다",
            )
        databases = matches
    return [{**d, "_project": channel} for d in databases]


def resolve_kanban_board(
    client: CentralClient, project_name: str, board: str | None = None
) -> dict:
    """단일 대상 작업(add 등)용 보드 하나 선택 — 기본 '업무 보드'.

    --board가 여러 개에 걸리거나, 이름 없이 보드가 여럿인데 '업무 보드'도 없으면
    후보를 나열한다. 반환값은 project_databases 항목(+`_project`)."""
    boards = resolve_kanban_boards(client, project_name, board=board)
    channel = boards[0]["_project"]
    if board:
        if len(boards) == 1:
            return boards[0]
        raise TicketUXError(
            f"보드 '{board}' 모호 ({len(boards)}건)",
            valid=[str(d.get("title")) for d in boards],
            example=f'orthus ticket add "제목" --project "{channel.get("name")}" --board "업무 보드"',
        )
    default = [d for d in boards if str(d.get("title", "")).strip() == "업무 보드"]
    picked = default[0] if default else (boards[0] if len(boards) == 1 else None)
    if picked is None:
        raise TicketUXError(
            f"'{channel.get('name')}'에 보드가 여러 개 — --board로 지정",
            valid=[str(d.get("title")) for d in boards],
            example=f'orthus ticket add "제목" --project "{channel.get("name")}" --board "업무 보드"',
            note="조회(ls)는 --board 없이도 모든 보드를 한 번에 보여준다",
        )
    return picked


def board_prop(
    board: dict, *, prop_type: str | None = None, name: str | None = None
) -> dict | None:
    """스키마에서 속성 찾기 — 타입(title/status/person) 또는 이름(casefold)으로."""
    for prop in board.get("properties") or []:
        if prop_type and prop.get("type") == prop_type:
            return prop
        if name and str(prop.get("name", "")).casefold() == name.casefold():
            return prop
    return None


def resolve_option(prop: dict, word: str) -> str:
    """status/select 속성의 옵션 이름 → 옵션 id. 공백 무시 + prefix 허용.

    '시작전'도 '시작 전'에 매칭되게 공백을 지워 비교하고, status 속성이면
    open/done/in_progress 같은 영어 상태어를 옵션의 group(to_do/in_progress/
    complete)으로 자동 매핑한다 — 내 보드(open|done)에 익숙한 에이전트가 칸반
    카드에도 같은 단어를 쓸 수 있게."""
    folded = word.strip().casefold().replace(" ", "")
    options = prop.get("options") or []
    exact = [o for o in options if str(o.get("name", "")).casefold().replace(" ", "") == folded]
    if len(exact) == 1:
        return str(exact[0]["id"])
    prefix = [
        o for o in options if str(o.get("name", "")).casefold().replace(" ", "").startswith(folded)
    ]
    if len(prefix) == 1:
        return str(prefix[0]["id"])
    if prop.get("type") == "status":
        group = _STATUS_GROUP_ALIASES.get(folded.replace("_", "").replace("-", ""))
        if group:
            grouped = [o for o in options if str(o.get("group", "")) == group]
            if grouped:
                return str(grouped[0]["id"])
    raise TicketUXError(
        f"'{prop.get('name')}' 옵션 '{word}' {'모호' if prefix else '없음'}",
        valid=[str(o.get("name")) for o in options],
        example=f'--status "{options[0].get("name")}"' if options else None,
        note="옵션 이름은 공백 무시 매칭(시작전=시작 전)이고, status는 open/todo·"
        "in_progress/doing·done/complete 영어도 컬럼 그룹으로 자동 매핑된다",
    )


# 칸반 status 옵션은 보드마다 이름이 달라도 group은 고정이다 — 내 보드 어휘(open/done)
# 와 흔한 영어 상태어를 group으로 이어준다.
_STATUS_GROUP_ALIASES = {
    "open": "to_do",
    "todo": "to_do",
    "backlog": "to_do",
    "inprogress": "in_progress",
    "progress": "in_progress",
    "doing": "in_progress",
    "wip": "in_progress",
    "done": "complete",
    "complete": "complete",
    "completed": "complete",
}

# 칸반 우선순위 select는 보드마다 옵션 이름이 다르다(높음/긴급/핫픽스…). 내 보드
# 어휘(urgent/priority/normal/low)를 흔한 한국어 옵션명 후보로 이어준다.
_PRIORITY_OPTION_ALIASES = {
    "urgent": ["긴급", "핫픽스", "비상", "높음"],
    "priority": ["높음", "우선", "중요"],
    "normal": ["보통", "중간", "일반"],
    "low": ["낮음"],
}


def resolve_priority_option(board: dict, word: str) -> tuple[dict, str]:
    """칸반 보드에서 우선순위 select 속성을 찾아 (속성, 옵션 id)로 푼다.

    --priority가 내 보드/칸반 어디서든 같은 뜻이 되게: 옵션 이름 직접 매칭을 먼저
    시도하고, urgent/priority/normal/low(및 u/p/n/l)는 한국어 옵션명 후보로 폴백."""
    prop = next(
        (
            p
            for p in board.get("properties") or []
            if p.get("type") == "select"
            and ("우선" in str(p.get("name", "")) or "priority" in str(p.get("name", "")).lower())
        ),
        None,
    )
    if prop is None:
        raise TicketUXError(
            "이 보드에 우선순위 속성이 없습니다",
            valid=[str(p.get("name")) for p in board.get("properties") or []],
            example="--prop <속성이름>=<값>",
            note="칸반 속성은 보드마다 다르다 — 위 목록에서 골라 --prop으로 지정",
        )
    try:
        return prop, resolve_option(prop, word)
    except TicketUXError:
        pass
    folded = word.strip().casefold()
    expanded = {"u": "urgent", "p": "priority", "n": "normal", "l": "low"}.get(folded, folded)
    for candidate in _PRIORITY_OPTION_ALIASES.get(expanded, []):
        try:
            return prop, resolve_option(prop, candidate)
        except TicketUXError:
            continue
    raise TicketUXError(
        f"'{prop.get('name')}' 옵션 '{word}' 없음",
        valid=[str(o.get("name")) for o in prop.get("options") or []],
        example=f"--priority {(prop.get('options') or [{}])[0].get('name', '')}",
        note="urgent/priority/normal/low(u/p/n/l)는 이 보드의 비슷한 옵션으로 자동 매핑을"
        " 시도한다 — 실패하면 위 실제 옵션 이름으로 지정",
    )


def resolve_member(members: list[dict], name: str) -> dict:
    """팀 멤버 이름 → 멤버 row. 대소문자 무시 + 유일 부분일치 허용."""
    folded = name.strip().casefold()
    exact = [m for m in members if str(m.get("name", "")).casefold() == folded]
    if len(exact) == 1:
        return exact[0]
    partial = [m for m in members if folded in str(m.get("name", "")).casefold()]
    if len(partial) == 1:
        return partial[0]
    raise TicketUXError(
        f"팀 멤버 '{name}' {'모호' if partial else '없음'}",
        valid=sorted(str(m.get("name")) for m in members),
        example='--assignee "이름"',
    )


def resolve_board_row(rows: list[dict], ref: str, *, title_prop_id: str | None) -> dict:
    """칸반 행 — id prefix(≥4자) 또는 제목 부분일치로 찾는다."""
    query = ref.strip()
    prefix = query.lower().replace("-", "")
    if len(prefix) >= 4 and all(c in "0123456789abcdef" for c in prefix):
        matches = [r for r in rows if str(r.get("row_id", "")).replace("-", "").startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise TicketUXError(
                f"카드 id '{ref}' 모호 ({len(matches)}건) — 더 길게",
                valid=[short_id(r["row_id"]) for r in matches],
            )
    if title_prop_id:
        folded = query.casefold()
        titled = [
            r
            for r in rows
            if folded in str((r.get("props") or {}).get(title_prop_id, "")).casefold()
        ]
        if len(titled) == 1:
            return titled[0]
        if len(titled) > 1:
            raise TicketUXError(
                f"'{ref}' 제목 일치 카드가 {len(titled)}건 — id prefix로 지정",
                valid=[
                    f"{short_id(r['row_id'])}  {(r.get('props') or {}).get(title_prop_id, '')}"
                    for r in titled
                ],
            )
    raise TicketUXError(
        f"카드 '{ref}'를 찾지 못했습니다",
        example='orthus ticket board "<프로젝트>"',
    )


def build_board_row_props(
    client: CentralClient,
    board: dict,
    *,
    title: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    priority: str | None = None,
    due: str | None = None,
    sets: list[str] | None = None,
    base: dict | None = None,
) -> dict:
    """칸반 행 props 빌드 — 서버 PATCH가 전체 교체라 base(기존 props) 위에 병합한다.

    status/select 값은 옵션 이름으로 받고(공백 무시), assignee는 팀 멤버 이름으로,
    --set 이름=값 은 임의 속성을 스키마 타입에 맞게 해석한다."""
    props = dict(base or {})
    if title is not None:
        title_prop = board_prop(board, prop_type="title")
        if title_prop is None:
            raise TicketUXError("이 보드에 제목 속성이 없습니다")
        props[str(title_prop["id"])] = title
    status_prop = board_prop(board, prop_type="status")
    if status is not None:
        if status_prop is None:
            raise TicketUXError("이 보드에 상태 속성이 없습니다")
        props[str(status_prop["id"])] = resolve_option(status_prop, status)
    elif base is None and status_prop is not None and status_prop.get("options"):
        # 신규 카드 기본 배치 = 첫 상태 컬럼(예: '시작 전').
        props[str(status_prop["id"])] = str(status_prop["options"][0]["id"])
    if assignee is not None:
        person_prop = board_prop(board, prop_type="person")
        if person_prop is None:
            raise TicketUXError("이 보드에 담당자 속성이 없습니다")
        member = resolve_member(list(client.team_members() or []), assignee)
        props[str(person_prop["id"])] = [str(member["member_id"])]
    if priority is not None:
        priority_prop, option_id = resolve_priority_option(board, priority)
        props[str(priority_prop["id"])] = option_id
    if due is not None:
        date_prop = board_prop(board, prop_type="date")
        if date_prop is None:
            raise TicketUXError("이 보드에 날짜 속성이 없습니다")
        props[str(date_prop["id"])] = parse_date_word(due)
    for pair in sets or []:
        if "=" not in pair:
            raise TicketUXError(
                f"--set 형식은 이름=값 이어야 합니다 ('{pair}')",
                example="--set 우선순위=높음",
            )
        name, _, raw = pair.partition("=")
        prop = board_prop(board, name=name.strip())
        if prop is None:
            raise TicketUXError(
                f"속성 '{name.strip()}'이(가) 이 보드에 없습니다",
                valid=[str(p.get("name")) for p in board.get("properties") or []],
            )
        prop_type = prop.get("type")
        if prop_type in ("status", "select"):
            props[str(prop["id"])] = resolve_option(prop, raw)
        elif prop_type == "multi_select":
            props[str(prop["id"])] = [resolve_option(prop, raw)]
        elif prop_type == "person":
            member = resolve_member(list(client.team_members() or []), raw)
            props[str(prop["id"])] = [str(member["member_id"])]
        elif prop_type == "date":
            props[str(prop["id"])] = parse_date_word(raw)
        else:
            props[str(prop["id"])] = raw
    return props


def kanban_snapshot(client: CentralClient, board: dict) -> dict:
    """칸반 보드를 상태 컬럼 단위 구조로 뽑는다 (CLI 렌더/MCP 반환 공용)."""
    bundle = client.database_bundle(str(board["database_id"])) or {}
    schema = bundle.get("database") or board
    rows = list(bundle.get("rows") or [])
    status_prop = board_prop(schema, prop_type="status")
    title_prop = board_prop(schema, prop_type="title")
    person_prop = board_prop(schema, prop_type="person")
    date_prop = board_prop(schema, prop_type="date")
    members = {str(m.get("member_id")): str(m.get("name")) for m in (client.team_members() or [])}

    def card(row: dict) -> dict:
        props = row.get("props") or {}
        assignees = (
            [members.get(str(mid), "?") for mid in (props.get(str(person_prop["id"])) or [])]
            if person_prop
            else []
        )
        return {
            "row_id": row.get("row_id"),
            "short_id": short_id(row.get("row_id")),
            "title": props.get(str(title_prop["id"])) if title_prop else None,
            "assignees": assignees,
            "due": props.get(str(date_prop["id"])) if date_prop else None,
        }

    columns = []
    seen: set[str] = set()
    if status_prop:
        sid = str(status_prop["id"])
        for option in status_prop.get("options") or []:
            oid = str(option["id"])
            cards = [card(r) for r in rows if str((r.get("props") or {}).get(sid, "")) == oid]
            seen.update(
                str(r.get("row_id"))
                for r in rows
                if str((r.get("props") or {}).get(sid, "")) == oid
            )
            columns.append({"status": option.get("name"), "count": len(cards), "cards": cards})
    orphans = [card(r) for r in rows if str(r.get("row_id")) not in seen]
    if orphans:
        columns.append({"status": "(상태 없음)", "count": len(orphans), "cards": orphans})
    return {
        "project": (board.get("_project") or {}).get("name"),
        "board": schema.get("title"),
        "total": len(rows),
        "columns": columns,
    }


def resolve_any_ticket(
    client: CentralClient, ref: str, *, project: str | None = None, board: str | None = None
) -> tuple[str, dict, dict]:
    """id(또는 --project 지정 시 제목)로 티켓을 찾는다 — 저장소를 몰라도 되는 진입점.

    반환: ("board", {}, task) — 내 보드 티켓
          ("kanban", board_schema(+_project/database_id), row) — 프로젝트 칸반 카드
    검색 순서: --project가 있으면 그 프로젝트의 모든 보드(--board로 좁힘). 없으면
    내 보드 → 담당 회사 프로젝트 칸반 전부. 겹치면 후보를 나열한다."""
    if project is not None:
        # 프로젝트에 DB(보드)가 여러 개일 수 있다 — 전부 검색하고, 겹치면 보드명 후보.
        found: list[tuple[dict, dict]] = []
        errors: list[TicketUXError] = []
        for candidate_board in resolve_kanban_boards(client, project, board=board):
            bundle = client.database_bundle(str(candidate_board["database_id"])) or {}
            schema = {
                **(bundle.get("database") or candidate_board),
                "_project": candidate_board.get("_project"),
            }
            title_prop = board_prop(schema, prop_type="title")
            try:
                row = resolve_board_row(
                    list(bundle.get("rows") or []),
                    ref,
                    title_prop_id=str(title_prop["id"]) if title_prop else None,
                )
            except TicketUXError as exc:
                errors.append(exc)
                continue
            found.append((schema, row))
        if len(found) == 1:
            schema, row = found[0]
            return ("kanban", schema, row)
        if len(found) > 1:
            raise TicketUXError(
                f"'{ref}'가 이 프로젝트의 보드 {len(found)}곳에서 일치 — --board로 좁혀라",
                valid=[f"{s.get('title')}: {short_id(r['row_id'])}" for s, r in found],
                example=f'orthus ticket set {short_id(found[0][1]["row_id"])} --board "{found[0][0].get("title")}" ...',
            )
        # 모호(다건 일치) 오류가 있으면 그게 더 정확한 안내다 — 그대로 올린다.
        for exc in errors:
            if exc.valid:
                raise exc
        raise TicketUXError(
            f"카드 '{ref}'를 이 프로젝트의 보드 {len(errors)}개에서 찾지 못했습니다",
            example=f'orthus ticket ls --project "{project}"',
            note="--board 없이 ls를 치면 보드별 카드가 다 보인다",
        )

    prefix = ref.strip().lower().replace("-", "")
    if len(prefix) < 4 or any(c not in "0123456789abcdef" for c in prefix):
        raise TicketUXError(
            f"invalid ticket id '{ref}' (need at least 4 leading hex chars)",
            example="orthus ticket ls",
        )
    candidates: list[tuple[str, dict, dict]] = []
    for task in client.board_tasks_list(id_prefix=prefix, limit=10) or []:
        candidates.append(("board", {}, task))
    for channel in client.board_projects() or []:
        if channel.get("kind") != "company" or not channel.get("company_project_id"):
            continue
        for db in client.project_databases(str(channel["company_project_id"])) or []:
            bundle = client.database_bundle(str(db["database_id"])) or {}
            schema = {**(bundle.get("database") or db), "_project": channel}
            for row in bundle.get("rows") or []:
                if str(row.get("row_id", "")).replace("-", "").startswith(prefix):
                    candidates.append(("kanban", schema, row))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise TicketUXError(
            f"ticket '{ref}' not found (내 보드 + 담당 프로젝트 칸반 검색)",
            example="orthus ticket ls  (칸반: orthus ticket ls --project <프로젝트>)",
        )

    def _label(item: tuple[str, dict, dict]) -> str:
        kind, schema, obj = item
        if kind == "board":
            return f"{short_id(obj['task_id'])}  [내 보드]  {obj.get('title', '')}"
        title_prop = board_prop(schema, prop_type="title")
        title = (obj.get("props") or {}).get(str(title_prop["id"])) if title_prop else ""
        project_name = (schema.get("_project") or {}).get("name")
        return f"{short_id(obj['row_id'])}  [{project_name}/{schema.get('title')}]  {title}"

    raise TicketUXError(
        f"'{ref}'가 {len(candidates)}건과 일치 — 더 긴 id나 --project로 좁혀라",
        valid=[_label(c) for c in candidates],
        example="orthus ticket set <더 긴 id> --status ...",
    )


def resolve_task(client: CentralClient, ref: str) -> dict:
    """Task id prefix (git-SHA style, ≥4 chars) or full UUID → the single task."""
    prefix = ref.strip().lower().replace("-", "")
    if len(prefix) < 4 or any(c not in "0123456789abcdef" for c in prefix):
        raise TicketUXError(
            f"invalid ticket id '{ref}' (need at least 4 leading hex chars)",
            example="orthus ticket list",
        )
    matches = list(client.board_tasks_list(id_prefix=prefix, limit=10) or [])
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise TicketUXError(
            f"ticket '{ref}' not found on your board",
            example="orthus ticket list --status open",
        )
    candidates = [f"{short_id(t['task_id'])}  {t.get('title', '')}" for t in matches]
    raise TicketUXError(
        f"ticket id '{ref}' is ambiguous ({len(matches)} matches); use more characters",
        valid=candidates,
        example=f"orthus ticket show {short_id(matches[0]['task_id'])}",
    )
