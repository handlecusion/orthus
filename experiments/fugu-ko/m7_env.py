"""M7 — Agentic Loop Bench: deterministic tool environment (fixture world).

Every tool is a deterministic stub over a FICTIONAL company ("다온컴퍼니") fixture
set — no DB, no embedding, no nested LLM. Rationale (prereg §4): the bench measures
the LOOP (plan → call → observe → recover → finish), so tool backends must be
noise-free and end states must be assertable without a judge. Production `wiki_ask`
nests a chat LLM internally and would confound the measurement; it is decomposed
here into deterministic `wiki_search` + `wiki_page`. Fixture facts are fictional so
parametric guessing cannot fake a completion.

Tool schema mirrors the production `orthus/router/agentic/tools.py` spec format
({name, description, input_schema}); descriptions are copied from production where
the same tool exists and written in the same style otherwise.

One `Env` instance per (model, task) run. Write tools mutate Env state; the runner
snapshots `env.end_state()` for the scorer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

TODAY = "2026-07-23"

# --------------------------------------------------------------------------- #
# Fixture world (fictional — authored blind, frozen at prereg).
# --------------------------------------------------------------------------- #
TEAM: list[dict] = [
    {
        "name": "서지안",
        "title": "제품총괄",
        "department": "프로덕트",
        "email": "jian.seo@daon.kr",
        "projects": ["팔레트"],
    },
    {
        "name": "문하람",
        "title": "백엔드 리드",
        "department": "개발",
        "email": "haram.moon@daon.kr",
        "projects": ["팔레트", "온새미로"],
    },
    {
        "name": "채윤슬",
        "title": "디자인 리드",
        "department": "디자인",
        "email": "yunseul.chae@daon.kr",
        "projects": ["무지개다리"],
    },
    {
        "name": "강도윤",
        "title": "데이터 엔지니어",
        "department": "개발",
        "email": "doyoon.kang@daon.kr",
        "projects": ["온새미로"],
    },
    {
        "name": "임소이",
        "title": "마케팅 매니저",
        "department": "마케팅",
        "email": "soi.lim@daon.kr",
        "projects": ["무지개다리"],
    },
    {
        "name": "백시우",
        "title": "인프라 엔지니어",
        "department": "개발",
        "email": "siwoo.baek@daon.kr",
        "projects": ["팔레트"],
    },
]

PROJECTS: list[dict] = [
    {"name": "팔레트", "summary": "결제·정산 SaaS 제품"},
    {"name": "온새미로", "summary": "데이터 수집·파이프라인 플랫폼"},
    {"name": "무지개다리", "summary": "브랜드·마케팅 사이트"},
]

BOARD: list[dict] = [
    {
        "id": "b1",
        "title": "팔레트 결제 모듈 v2",
        "status": "In Progress",
        "assignee": "문하람",
        "priority": "P1",
        "due": "2026-07-30",
        "project": "팔레트",
    },
    {
        "id": "b2",
        "title": "온새미로 수집기 재시도 큐",
        "status": "Todo",
        "assignee": "강도윤",
        "priority": "P2",
        "due": None,
        "project": "온새미로",
    },
    {
        "id": "b3",
        "title": "무지개다리 리브랜딩 시안",
        "status": "Review",
        "assignee": "채윤슬",
        "priority": "P2",
        "due": None,
        "project": "무지개다리",
    },
    {
        "id": "b4",
        "title": "팔레트 로그인 개편",
        "status": "Done",
        "assignee": "백시우",
        "priority": "P3",
        "due": None,
        "project": "팔레트",
    },
    {
        "id": "b5",
        "title": "온새미로 대시보드 지표 정의",
        "status": "Todo",
        "assignee": "강도윤",
        "priority": "P2",
        "due": "2026-08-05",
        "project": "온새미로",
    },
]

CALENDAR: list[dict] = [
    {"date": "2026-07-27", "title": "전사 주간회의"},
    {"date": "2026-07-29", "title": "팔레트 결제 v2 QA 시작"},
    {"date": "2026-07-31", "title": "페이버스 정기 미팅"},
    {"date": "2026-08-20", "title": "팔레트 결제 모듈 v2 출시"},
]

# keywords = deterministic search index (a query matches a page iff any keyword is a
# substring of the query). partners/pg-contract deliberately lacks "페이버스" in its
# index (realistic index gap; V1 additionally hard-blocks 페이버스-queries).
WIKI: list[dict] = [
    {
        "slug": "projects/pallete-overview",
        "title": "팔레트 프로젝트 개요",
        "keywords": ["팔레트", "개요", "정산", "제품"],
        "body": (
            "팔레트는 다온컴퍼니의 결제·정산 SaaS 제품이다. 제품총괄은 서지안. "
            "결제는 PG 결제대행(페이버스) 연동으로 처리한다. 정산 주기는 매월 10일."
        ),
    },
    {
        "slug": "projects/pallete-release-plan",
        "title": "팔레트 릴리스 플랜",
        "keywords": ["팔레트", "릴리스", "출시", "플랜", "일정"],
        "body": (
            "결제 모듈 v2 출시일: 2026-08-06 (예정). QA 시작: 2026-07-29. "
            "출시 후 2주 안정화 기간을 둔다."
        ),
    },
    {
        "slug": "meetings/2026-07-15-product-sync",
        "title": "2026-07-15 제품 싱크 회의록",
        "keywords": ["회의", "회의록", "제품", "싱크", "결정"],
        "body": (
            "결정사항: (1) 팔레트 결제 모듈 v2 출시일을 2026-08-20으로 연기 확정. "
            "(2) 온새미로 베타 시작일 2026-09-01 확정. 참석: 서지안, 문하람, 강도윤."
        ),
    },
    {
        "slug": "projects/onsaemiro-pipeline",
        "title": "온새미로 수집 파이프라인",
        "keywords": ["온새미로", "수집", "파이프라인", "재시도"],
        "body": (
            "수집 주기는 30분. 수집 실패 시 재시도 3회 후 대기열로 이동한다. "
            "파이프라인 담당: 강도윤."
        ),
    },
    {
        "slug": "ops/expense-policy",
        "title": "경비 정책",
        "keywords": ["경비", "회식", "출장", "법인카드", "한도", "일비"],
        "body": "회식비는 1인 5만원 한도. 출장 일비는 8만원. 법인카드 월 한도는 200만원.",
    },
    {
        "slug": "hr/onboarding",
        "title": "온보딩 가이드",
        "keywords": ["온보딩", "입사", "장비", "계정"],
        "body": "신규 입사자 장비 신청은 인프라 엔지니어 백시우에게 한다. 계정 발급은 3영업일 소요.",
    },
    {
        "slug": "partners/pg-contract",
        "title": "PG 결제대행 계약",
        "keywords": ["PG", "계약", "수수료", "결제대행", "갱신"],
        "body": "페이버스과의 결제대행 계약. 수수료율 2.9%. 계약 갱신일 2027-01-31.",
    },
]

# mail_search matches query tokens against subject+from only (production mail_search
# semantics: 제목/발신자). Bodies are ordered so the 40-char snippet cuts the key
# fact — reading the fact requires the mail_read hop.
MAIL: list[dict] = [
    {
        "id": "m1",
        "from": "김파트너 <partner@payverse.example>",
        "date": "2026-07-17",
        "subject": "페이버스 수수료 조정 제안",
        "body": (
            "안녕하세요, 페이버스 파트너십팀 김파트너입니다. 계약 조건 관련 제안을 드립니다. "
            "결제대행 수수료율을 현행 2.9%에서 2.6%로 조정하는 안을 검토 부탁드립니다. "
            "8월 1일까지 회신 주시면 감사하겠습니다."
        ),
    },
    {
        "id": "m2",
        "from": "서지안 <jian.seo@daon.kr>",
        "date": "2026-07-16",
        "subject": "팔레트 결제 v2 일정 확인",
        "body": "회의 결정대로 결제 모듈 v2 출시일은 2026-08-20 유지합니다. QA는 계획대로 진행해주세요.",
    },
    {
        "id": "m3",
        "from": "정나래 <narae.jung@gmail.com>",
        "date": "2026-07-19",
        "subject": "면접 일정 문의",
        "body": "안녕하세요, 백엔드 포지션 지원자 정나래입니다. 면접 가능 일정을 알려주시면 감사하겠습니다.",
    },
    {
        "id": "m4",
        "from": "강도윤 <doyoon.kang@daon.kr>",
        "date": "2026-07-18",
        "subject": "온새미로 수집기 장애 보고",
        "body": (
            "7/18 02:00부터 온새미로 수집이 12건 실패했습니다. 확인 결과 원인은 수집용 인증 "
            "토큰 만료였고, 임시로 수동 갱신 조치했습니다. 자동 갱신 구현이 필요합니다."
        ),
    },
]

DATA_GAPS: list[dict] = [
    {"question": "무지개다리 런칭일이 정해지지 않음", "hit_count": 3},
    {"question": "온새미로 베타 대상 고객 리스트 부재", "hit_count": 1},
]

_SNIPPET_LEN = 40

# --------------------------------------------------------------------------- #
# Tool specs — production format ({name, description, input_schema}).
# --------------------------------------------------------------------------- #
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
        "name": "wiki_search",
        "description": (
            "회사 위키를 키워드로 검색해 페이지 목록(slug·제목)을 받는다. 내용을 읽으려면 "
            "wiki_page 로 slug 를 조회한다. 결과가 없으면 다른 키워드로 다시 검색하라."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 키워드(한국어)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "wiki_page",
        "description": "위키 페이지 본문을 slug 로 조회한다. slug 는 wiki_search 결과의 slug 를 쓴다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "위키 페이지 slug"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "team_members",
        "description": (
            "회사 팀원 목록을 조회한다. 이름·직책·부서·이메일·소속 프로젝트를 본다. "
            "'우리 팀에 누구 있어?' '…담당이 누구야?' '…한테 메일 보낼 주소' 처럼 팀원/역할/"
            "연락처를 확인할 때 쓴다."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "board",
        "description": (
            "회사 칸반 보드를 조회한다. 카드별 제목·상태(Todo/In Progress/Review/Done)·"
            "담당자·우선순위·마감을 본다. '…작업 상태' 처럼 보드/태스크 진행 상태를 확인할 때 쓴다."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "projects",
        "description": "회사 프로젝트 목록(이름·한 줄 설명)을 조회한다.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "mail_search",
        "description": (
            "내 회사 메일함을 제목·발신자 텍스트로 검색한다. 결과는 메일 id·발신자·제목·"
            "앞부분 미리보기만 준다. 본문 전체는 mail_read 로 id 를 조회한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "제목/발신자 검색 키워드"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "mail_read",
        "description": "메일 본문 전체를 id 로 읽는다. id 는 mail_search 결과의 id 를 쓴다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "메일 id (예: m1)"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "data_gaps",
        "description": (
            "위키가 채우지 못한 미해결 데이터 갭(지식 백로그) 목록을 본다. 보조 컨텍스트지 "
            "답변 근거가 아니다."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "mail_compose",
        "description": (
            "사용자가 메일을 쓰거나 답장하라고 하면 이 도구로 발송 전 초안을 만든다. "
            "to(받는사람 이메일 주소), subject(제목), body(본문, 한국어 평문)를 채운다. "
            "이건 즉시 발송이 아니라 사용자가 검토 후 직접 '보내기'를 누르는 초안이므로, 한 "
            "번 호출해 초안을 만든 뒤 '초안을 작성했습니다'라고만 답하고 재확인을 묻지 마라. "
            "받는사람 주소를 모르면 먼저 team_members 나 mail_search 로 찾는다."
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
    },
    {
        "name": "board_add_task",
        "description": (
            "칸반 보드에 새 태스크 카드를 추가한다. title(제목)과 assignee(담당자 이름 — "
            "팀 디렉터리에 실존하는 이름)를 채운다. 담당자가 불확실하면 먼저 team_members 나 "
            "board 로 확인한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "태스크 제목"},
                "assignee": {"type": "string", "description": "담당자 이름(팀원 실명)"},
                "description": {"type": "string", "description": "태스크 설명(선택)"},
            },
            "required": ["title", "assignee"],
        },
    },
    {
        "name": "calendar_add",
        "description": (
            "팀 캘린더에 일정을 등록한다. title(일정 제목)과 date(YYYY-MM-DD)를 채운다. "
            "날짜가 다른 기록(회의록·보드·기존 일정)에 근거해야 하면 먼저 조회해 확인한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "일정 제목"},
                "date": {"type": "string", "description": "YYYY-MM-DD 날짜"},
            },
            "required": ["title", "date"],
        },
    },
    {
        "name": "wiki_update_candidate",
        "description": (
            "회사 위키 내용을 추가/수정/정정해야 하면 이 도구로 검토 후보를 제출한다"
            "(slug=대상 페이지, note=제안 내용). 즉시 발행이 아니라 검토 큐에 올라가는 "
            "후보이므로, 제출 후 '검토 후보로 제출했다(즉시 반영 아님)'고 사실대로 답한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "대상 위키 페이지 slug"},
                "note": {"type": "string", "description": "제안 내용(무엇을 어떻게 고칠지)"},
            },
            "required": ["slug", "note"],
        },
    },
]

TOOL_BY_NAME = {t["name"]: t for t in TOOL_SPECS}


def system_prompt() -> str:
    """Single fixed system prompt — production `_system_prompt` style, NOT tuned
    per model (prereg §7)."""
    return (
        f"오늘 날짜는 {TODAY} 이다. 너는 다온컴퍼니의 회사 지식·업무 비서다. "
        "아래 도구로만 사실을 확인하고 작업을 수행해 한국어로 간결하고 정확하게 답한다.\n"
        "- 조회: team_schedule(팀 일정), wiki_search/wiki_page(회사 위키), "
        "team_members(팀원·이메일), board(칸반 보드), projects(프로젝트), "
        "mail_search/mail_read(내 메일함), data_gaps(지식 백로그).\n"
        "- 작업: mail_compose(발송 전 메일 초안), board_add_task(보드 태스크 추가), "
        "calendar_add(캘린더 일정 등록), wiki_update_candidate(위키 수정 검토 후보).\n"
        "규칙: 추측하지 말 것. 이름·이메일·날짜·수치는 반드시 도구로 확인한 뒤 쓴다. "
        "도구 결과가 비어 있거나 오류면 다른 도구나 다른 검색어로 다시 시도하고, 그래도 "
        "없으면 없다고 사실대로 답한다. 요청이 초안 작성/태스크 추가/일정 등록이면 해당 "
        "작업 도구를 실제로 호출해 완료한 뒤 결과를 보고한다. 최종 답변은 도구 원본을 "
        "그대로 덤프하지 말고 요약해서 전달한다."
    )


# --------------------------------------------------------------------------- #
# Environment: dispatch + write-state.
# --------------------------------------------------------------------------- #
class ToolFormatError(Exception):
    """Unknown tool / missing required arg / wrong arg type — counted as a
    tool-call format failure (prereg §6)."""


@dataclass
class Env:
    """One per (model, task) run. `rig` keys (prereg-frozen, per task):
    - block_wiki_query_terms: [str] — wiki_search queries containing any term
      return 0 rows (index-gap rig, V1).
    - board_error_first_n: int — first N board() calls return a transient error (V2).
    - locked_wiki_slugs: [str] — wiki_page on these slugs always errors (V4).
    """

    rig: dict = field(default_factory=dict)
    mail_drafts: list[dict] = field(default_factory=list)
    added_tasks: list[dict] = field(default_factory=list)
    added_events: list[dict] = field(default_factory=list)
    wiki_candidates: list[dict] = field(default_factory=list)
    board_calls: int = 0

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _require(args: dict, tool: str, keys: list[str]) -> None:
        if not isinstance(args, dict):
            raise ToolFormatError(f"{tool}: arguments 가 객체(JSON object)가 아님")
        for k in keys:
            v = args.get(k)
            if v is None or (isinstance(v, str) and not v.strip()):
                raise ToolFormatError(f"{tool}: 필수 인자 '{k}' 누락")
            if not isinstance(v, str):
                raise ToolFormatError(f"{tool}: 인자 '{k}' 는 문자열이어야 함")

    # -- dispatch ------------------------------------------------------------
    def dispatch(self, name: str, args: dict) -> str:
        """Run one tool call. Raises ToolFormatError for schema violations;
        returns model-facing text otherwise (business errors are returned as
        '[도구 오류] …' text so the model can recover)."""
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            raise ToolFormatError(f"알 수 없는 도구: {name}")
        return handler(args if isinstance(args, dict) else {})

    def _t_team_schedule(self, args: dict) -> str:
        since = str(args.get("since") or "")[:10]
        until = str(args.get("until") or "")[:10]
        rows = [
            e
            for e in CALENDAR + self.added_events
            if (not since or e["date"] >= since) and (not until or e["date"] <= until)
        ]
        if not rows:
            return "해당 기간 팀 일정 없음."
        return "\n".join(
            f"- {e['date']} {e['title']}" for e in sorted(rows, key=lambda e: e["date"])
        )

    def _t_wiki_search(self, args: dict) -> str:
        self._require(args, "wiki_search", ["query"])
        q = args["query"].strip()
        for term in self.rig.get("block_wiki_query_terms", []):
            if term in q:
                return "검색 결과 없음 (0건)."
        scored = []
        for p in WIKI:
            score = sum(1 for kw in p["keywords"] if kw.lower() in q.lower())
            if score:
                scored.append((score, p))
        if not scored:
            return "검색 결과 없음 (0건)."
        scored.sort(key=lambda t: (-t[0], t[1]["slug"]))
        return "\n".join(f"- {p['slug']} — {p['title']}" for _, p in scored)

    def _t_wiki_page(self, args: dict) -> str:
        self._require(args, "wiki_page", ["slug"])
        slug = args["slug"].strip().strip("/")
        if slug in self.rig.get("locked_wiki_slugs", []):
            return "[도구 오류] 페이지가 편집 잠금 상태라 열 수 없음. 다른 근거를 확인하라."
        for p in WIKI:
            if p["slug"] == slug:
                return f"# {p['title']}\n{p['body']}"
        return f"[도구 오류] slug '{slug}' 페이지 없음. wiki_search 로 slug 를 확인하라."

    def _t_team_members(self, args: dict) -> str:
        return "\n".join(
            f"- {m['name']} · {m['title']} · {m['department']} · {m['email']} · "
            f"프로젝트: {', '.join(m['projects'])}"
            for m in TEAM
        )

    def _t_board(self, args: dict) -> str:
        self.board_calls += 1
        if self.board_calls <= int(self.rig.get("board_error_first_n", 0)):
            return "[도구 오류] 보드 백엔드 일시 오류(500). 잠시 후 다시 시도하라."
        lines = []
        for c in BOARD + self.added_tasks:
            bits = [f"[{c['status']}]", c["title"], f"담당={c['assignee']}"]
            if c.get("priority"):
                bits.append(f"우선={c['priority']}")
            if c.get("due"):
                bits.append(f"마감={c['due']}")
            if c.get("project"):
                bits.append(f"프로젝트={c['project']}")
            lines.append("- " + " ".join(bits))
        return "\n".join(lines)

    def _t_projects(self, args: dict) -> str:
        return "\n".join(f"- {p['name']} — {p['summary']}" for p in PROJECTS)

    def _t_mail_search(self, args: dict) -> str:
        self._require(args, "mail_search", ["query"])
        tokens = [t for t in re.split(r"\s+", args["query"].strip()) if len(t) >= 2]
        rows = []
        for m in MAIL:
            hay = f"{m['subject']} {m['from']}"
            if any(t in hay for t in tokens):
                rows.append(m)
        if not rows:
            return "검색 결과 없음 (메일 0건)."
        return "\n".join(
            f"- id={m['id']} {m['date']} {m['from']}: {m['subject']} | "
            f"미리보기: {m['body'][:_SNIPPET_LEN]}…"
            for m in rows
        )

    def _t_mail_read(self, args: dict) -> str:
        self._require(args, "mail_read", ["id"])
        mid = args["id"].strip()
        for m in MAIL:
            if m["id"] == mid:
                return (
                    f"보낸사람: {m['from']}\n날짜: {m['date']}\n제목: {m['subject']}\n\n{m['body']}"
                )
        return f"[도구 오류] 메일 id '{mid}' 없음. mail_search 결과의 id 를 쓰라."

    def _t_data_gaps(self, args: dict) -> str:
        return "\n".join(f"- ({g['hit_count']}회) {g['question']}" for g in DATA_GAPS)

    def _t_mail_compose(self, args: dict) -> str:
        self._require(args, "mail_compose", ["to", "subject", "body"])
        draft = {k: args[k].strip() for k in ("to", "subject", "body")}
        self.mail_drafts.append(draft)
        return (
            "초안 작성됨 (발송 전 검토 대기 — 사용자가 직접 보내기를 눌러야 발송된다): "
            f"to={draft['to']} 제목={draft['subject']}"
        )

    def _t_board_add_task(self, args: dict) -> str:
        self._require(args, "board_add_task", ["title", "assignee"])
        assignee = args["assignee"].strip()
        if assignee not in {m["name"] for m in TEAM}:
            return f"[도구 오류] 담당자 '{assignee}' 를 팀 디렉터리에서 찾을 수 없음. team_members 로 확인하라."
        card = {
            "title": args["title"].strip(),
            "assignee": assignee,
            "status": "Todo",
            "description": str(args.get("description") or "").strip(),
        }
        self.added_tasks.append(card)
        return f"태스크 추가됨: [Todo] {card['title']} 담당={assignee}"

    def _t_calendar_add(self, args: dict) -> str:
        self._require(args, "calendar_add", ["title", "date"])
        d = args["date"].strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            return f"[도구 오류] date '{d}' 형식 오류 — YYYY-MM-DD 로 다시 호출하라."
        ev = {"title": args["title"].strip(), "date": d}
        self.added_events.append(ev)
        return f"일정 등록됨: {d} {ev['title']}"

    def _t_wiki_update_candidate(self, args: dict) -> str:
        self._require(args, "wiki_update_candidate", ["slug", "note"])
        slug = args["slug"].strip().strip("/")
        if slug not in {p["slug"] for p in WIKI}:
            return f"[도구 오류] slug '{slug}' 페이지 없음. wiki_search 로 slug 를 확인하라."
        cand = {"slug": slug, "note": args["note"].strip()}
        self.wiki_candidates.append(cand)
        return f"검토 후보 제출됨 (즉시 반영 아님): {slug}"

    def end_state(self) -> dict:
        return {
            "mail_drafts": self.mail_drafts,
            "added_tasks": self.added_tasks,
            "added_events": self.added_events,
            "wiki_candidates": self.wiki_candidates,
        }


# --------------------------------------------------------------------------- #
# Deterministic completion checks (no judge).
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).lower()


def _contains_any(hay: str, alts: list[str]) -> bool:
    h = _norm(hay)
    return any(_norm(a) in h for a in alts)


def check_task(
    task: dict, final_text: str, end_state: dict, n_ok_tool_calls: int
) -> tuple[bool, list[str]]:
    """Evaluate the task's frozen checks. Returns (completed, failed_check_names)."""
    failed: list[str] = []
    if n_ok_tool_calls < int(task.get("min_successful_tool_calls", 1)):
        failed.append("min_tool_calls")
    for i, chk in enumerate(task["checks"]):
        kind = chk["type"]
        name = f"{kind}#{i}"
        if kind == "answer_contains_all":
            if not all(_contains_any(final_text, group) for group in chk["groups"]):
                failed.append(name)
        elif kind == "answer_not_regex":
            if re.search(chk["pattern"], final_text or ""):
                failed.append(name)
        elif kind == "mail_draft":
            ok = False
            for d in end_state.get("mail_drafts", []):
                if _norm(chk["to"]) not in _norm(d["to"]):
                    continue
                if not d["subject"].strip() or not d["body"].strip():
                    continue
                blob = d["subject"] + "\n" + d["body"]
                if chk.get("contains_any") and not _contains_any(blob, chk["contains_any"]):
                    continue
                ok = True
            if not ok:
                failed.append(name)
        elif kind == "board_task":
            ok = any(
                t["assignee"] == chk["assignee"]
                and _contains_any(
                    t["title"] + " " + t.get("description", ""), chk["title_contains_any"]
                )
                for t in end_state.get("added_tasks", [])
            )
            if not ok:
                failed.append(name)
        elif kind == "calendar_event":
            ok = any(
                e["date"] == chk["date"] and _contains_any(e["title"], chk["title_contains_any"])
                for e in end_state.get("added_events", [])
            )
            if not ok:
                failed.append(name)
        elif kind == "wiki_candidate":
            ok = any(
                c["slug"] == chk["slug"] and _contains_any(c["note"], chk["note_contains_any"])
                for c in end_state.get("wiki_candidates", [])
            )
            if not ok:
                failed.append(name)
        else:  # unknown check kind = authoring bug — fail loudly
            failed.append(f"unknown_check:{kind}")
    return (not failed, failed)


if __name__ == "__main__":  # smoke: deterministic dispatch sanity
    env = Env()
    print(env.dispatch("wiki_search", {"query": "경비 한도"}))
    print(env.dispatch("mail_search", {"query": "페이버스"}))
    print(json.dumps(env.end_state(), ensure_ascii=False))
