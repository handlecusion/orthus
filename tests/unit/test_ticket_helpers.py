"""orthus ticket agent-UX helpers: forgiving input, self-healing errors."""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from orthus.mcp.tickets import (
    TicketUXError,
    normalize_priority,
    normalize_status,
    parse_date_word,
    resolve_bucket,
    resolve_project,
    resolve_task,
    short_id,
)

TODAY = date(2026, 7, 10)


class FakeClient:
    def __init__(self, *, projects=None, buckets=None, tasks=None):
        self._projects = projects or []
        self._buckets = buckets or []
        self._tasks = tasks or []

    def board_projects(self):
        return self._projects

    def board_backlog_buckets(self):
        return self._buckets

    def board_tasks_list(self, *, id_prefix=None, limit=50, **_):
        return [
            t for t in self._tasks if str(t["task_id"]).replace("-", "").startswith(id_prefix or "")
        ][:limit]


def _project(name, kind="personal"):
    return {"project_id": str(uuid.uuid4()), "name": name, "kind": kind}


def _task(title):
    return {"task_id": str(uuid.uuid4()), "title": title}


def test_parse_date_word_variants():
    assert parse_date_word("today", today=TODAY) == "2026-07-10"
    assert parse_date_word("TOMORROW", today=TODAY) == "2026-07-11"
    assert parse_date_word("+3d", today=TODAY) == "2026-07-13"
    assert parse_date_word("2026-08-01", today=TODAY) == "2026-08-01"


def test_parse_date_word_error_carries_valid_and_example():
    with pytest.raises(TicketUXError) as err:
        parse_date_word("next friday", today=TODAY)
    assert err.value.valid == ["today", "tomorrow", "+3d", "YYYY-MM-DD"]
    assert "--date" in err.value.example


def test_normalize_priority_abbreviations():
    assert normalize_priority("urgent") == "urgent"
    assert normalize_priority("U") == "urgent"
    assert normalize_priority("p") == "priority"
    assert normalize_priority("n") == "normal"
    assert normalize_priority("l") == "low"
    with pytest.raises(TicketUXError) as err:
        normalize_priority("high")
    assert err.value.valid == ["urgent", "priority", "normal", "low"]


def test_normalize_status_prefix():
    assert normalize_status("open") == "open"
    assert normalize_status("arch") == "archived"
    with pytest.raises(TicketUXError):
        normalize_status("closed")


def test_resolve_project_case_insensitive_and_prefix():
    nova = _project("Nova", kind="company")
    fake = FakeClient(projects=[nova, _project("Orbit"), _project("사이드")])
    assert resolve_project(fake, "nova") == nova
    assert resolve_project(fake, "nov") == nova
    assert resolve_project(fake, nova["project_id"]) == nova


def test_resolve_project_exact_beats_prefix_and_ambiguous_lists_candidates():
    fake = FakeClient(projects=[_project("Nova"), _project("Nova2")])
    # A unique exact (case-insensitive) match wins even when it prefixes others.
    assert resolve_project(fake, "nova")["name"] == "Nova"
    with pytest.raises(TicketUXError) as err:
        resolve_project(fake, "sho")
    assert sorted(err.value.valid) == ["Nova", "Nova2"]


def test_resolve_project_not_found_names_available():
    fake = FakeClient(projects=[_project("Orbit")])
    with pytest.raises(TicketUXError) as err:
        resolve_project(fake, "ghost")
    assert err.value.valid == ["Orbit"]
    assert err.value.example == "orthus ticket projects"


def test_resolve_bucket_by_key():
    bucket = {"bucket_id": str(uuid.uuid4()), "key": "next_week", "label": "W"}
    fake = FakeClient(buckets=[bucket])
    assert resolve_bucket(fake, "NEXT_WEEK") == bucket
    with pytest.raises(TicketUXError) as err:
        resolve_bucket(fake, "nope")
    assert err.value.valid == ["next_week"]


def test_resolve_task_prefix_and_ambiguity():
    task = _task("고유")
    twin_a = {**_task("쌍둥이 A"), "task_id": "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
    twin_b = {**_task("쌍둥이 B"), "task_id": "11111111-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}
    fake = FakeClient(tasks=[task, twin_a, twin_b])

    assert resolve_task(fake, short_id(task["task_id"])) == task
    assert resolve_task(fake, task["task_id"]) == task  # full UUID with dashes

    with pytest.raises(TicketUXError) as err:
        resolve_task(fake, "1111")
    assert "ambiguous" in str(err.value)
    assert len(err.value.valid) == 2

    with pytest.raises(TicketUXError) as err:
        resolve_task(fake, "ffff9999")
    assert "not found" in str(err.value)

    with pytest.raises(TicketUXError) as err:
        resolve_task(fake, "3f")  # too short
    assert "4" in str(err.value)


def test_resolve_company_project_rejects_personal_channel():
    from orthus.mcp.tickets import resolve_company_project

    company = {**_project("Nova", kind="company"), "company_project_id": str(uuid.uuid4())}
    personal = _project("사이드")
    fake = FakeClient(projects=[company, personal])

    assert resolve_company_project(fake, "nova") == company
    with pytest.raises(TicketUXError) as err:
        resolve_company_project(fake, "사이드")
    assert err.value.valid == ["Nova"]
    assert "project-list" in err.value.example


def test_resolve_any_ticket_searches_board_then_kanban():
    from orthus.mcp.tickets import resolve_any_ticket

    board_task = {"task_id": "aaaa1111-0000-4000-8000-000000000001", "title": "내 보드 건"}
    kanban_row = {"row_id": "bbbb2222-0000-4000-8000-000000000002", "props": {"t1": "카드 건"}}
    schema = {
        "database_id": "db1",
        "title": "업무 보드",
        "properties": [{"id": "t1", "name": "이름", "type": "title", "options": []}],
    }

    class Fake(FakeClient):
        def __init__(self):
            super().__init__(
                projects=[
                    {
                        "project_id": str(uuid.uuid4()),
                        "name": "탕수육 레시피",
                        "kind": "company",
                        "company_project_id": str(uuid.uuid4()),
                    }
                ],
                tasks=[board_task],
            )

        def project_databases(self, project_id):
            return [schema]

        def database_bundle(self, database_id):
            return {"database": schema, "rows": [kanban_row]}

    fake = Fake()
    kind, _, obj = resolve_any_ticket(fake, "aaaa1111")
    assert kind == "board" and obj == board_task
    kind, resolved_schema, obj = resolve_any_ticket(fake, "bbbb2222")
    assert kind == "kanban" and obj == kanban_row
    assert resolved_schema["_project"]["name"] == "탕수육 레시피"
    # --project 지정 시 제목으로도 카드 지정 가능
    kind, _, obj = resolve_any_ticket(fake, "카드", project="탕수육")
    assert kind == "kanban" and obj == kanban_row
    with pytest.raises(TicketUXError) as err:
        resolve_any_ticket(fake, "ffff9999")
    assert "not found" in str(err.value)


def test_kanban_body_text_unwraps_blocknote_json():
    from orthus.cli import _kanban_body_text

    blocks = (
        '[{"type":"paragraph","content":[{"type":"text","text":"## 목적"}]},'
        '{"type":"paragraph","content":[{"type":"text","text":"비교 "},'
        '{"type":"text","text":"분석"}]}]'
    )
    assert _kanban_body_text(blocks) == "## 목적\n비교 분석"
    assert _kanban_body_text("plain text") == "plain text"


def test_resolve_option_maps_english_status_to_group():
    from orthus.mcp.tickets import resolve_option

    prop = {
        "name": "상태",
        "type": "status",
        "options": [
            {"id": "s1", "name": "시작 전", "group": "to_do"},
            {"id": "s2", "name": "진행 중", "group": "in_progress"},
            {"id": "s3", "name": "완료", "group": "complete"},
        ],
    }
    assert resolve_option(prop, "시작전") == "s1"
    assert resolve_option(prop, "open") == "s1"
    assert resolve_option(prop, "todo") == "s1"
    assert resolve_option(prop, "in_progress") == "s2"
    assert resolve_option(prop, "doing") == "s2"
    assert resolve_option(prop, "done") == "s3"
    with pytest.raises(TicketUXError) as err:
        resolve_option(prop, "blocked")
    assert err.value.note is not None  # NOTE가 자동 매핑 규칙을 가르친다


def test_resolve_priority_option_aliases():
    from orthus.mcp.tickets import resolve_priority_option

    board = {
        "properties": [
            {"id": "t", "name": "이름", "type": "title", "options": []},
            {
                "id": "pr",
                "name": "우선순위",
                "type": "select",
                "options": [
                    {"id": "p1", "name": "높음"},
                    {"id": "p2", "name": "보통"},
                    {"id": "p3", "name": "낮음"},
                ],
            },
        ]
    }
    prop, oid = resolve_priority_option(board, "높음")
    assert oid == "p1"
    assert resolve_priority_option(board, "urgent")[1] == "p1"  # 긴급 없음 → 높음 폴백
    assert resolve_priority_option(board, "u")[1] == "p1"
    assert resolve_priority_option(board, "normal")[1] == "p2"
    assert resolve_priority_option(board, "low")[1] == "p3"
    with pytest.raises(TicketUXError) as err:
        resolve_priority_option({"properties": [{"id": "t", "name": "이름", "type": "title"}]}, "u")
    assert "우선순위 속성이 없" in str(err.value)


def test_multi_board_project_resolution():
    from orthus.mcp.tickets import resolve_any_ticket, resolve_kanban_boards

    ch = {
        "project_id": str(uuid.uuid4()),
        "name": "아틀라스",
        "kind": "company",
        "company_project_id": str(uuid.uuid4()),
    }
    work = {
        "database_id": "db-work",
        "title": "업무 보드",
        "properties": [{"id": "t1", "name": "이름", "type": "title", "options": []}],
    }
    audition = {
        "database_id": "db-aud",
        "title": "오디션 트래커",
        "properties": [{"id": "t2", "name": "이름", "type": "title", "options": []}],
    }
    row_work = {"row_id": "aaaa1111-0000-4000-8000-000000000001", "props": {"t1": "대관 확인"}}
    row_aud = {"row_id": "bbbb2222-0000-4000-8000-000000000002", "props": {"t2": "배우 A"}}

    class Fake(FakeClient):
        def __init__(self):
            super().__init__(projects=[ch])

        def project_databases(self, project_id):
            return [work, audition]

        def database_bundle(self, database_id):
            if database_id == "db-work":
                return {"database": work, "rows": [row_work]}
            return {"database": audition, "rows": [row_aud]}

    fake = Fake()
    # 전 보드 나열 + 이름으로 좁히기
    assert [b["title"] for b in resolve_kanban_boards(fake, "아틀라스")] == [
        "업무 보드",
        "오디션 트래커",
    ]
    assert resolve_kanban_boards(fake, "아틀라스", board="오디션")[0]["title"] == "오디션 트래커"
    # 제목 검색이 두 번째 보드까지 닿는다
    kind, schema, obj = resolve_any_ticket(fake, "배우 A", project="아틀라스")
    assert kind == "kanban" and schema["title"] == "오디션 트래커" and obj == row_aud
    # 없는 카드 → 보드 수를 알려주는 not-found
    with pytest.raises(TicketUXError) as err:
        resolve_any_ticket(fake, "없는 카드", project="아틀라스")
    assert "보드 2개" in str(err.value)
