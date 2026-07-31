"""회사 계획(weekly_entries.plan_items)에서 담당자 지정 → 그 사람 개인 보드 주간플랜에
'회사 부여' 주간 목표(personal_weekly_objective)로 자동 생성(materialize)되는 흐름.

회사 대시보드 /dashboard/plans에서 주간 계획 항목에 담당자(assignee_member_id)를 지정하면,
그 멤버로 매핑되는 계정의 GET /personal-board/weekly-plan 응답 objectives에 해당 항목이
source_kind='company_plan' 목표로 들어와야 한다. 제목·프로젝트는 회사 소유(수정/삭제 불가)
지만 요일 분배(day_allocations)·완료는 개인이 소유해 재동기화에도 보존된다. 회사쪽에서
담당이 빠지면(unassign) 다음 동기화에서 stale 제거된다. 멤버 매핑은 team_members.user_id
직접 링크가 없어도 로그인 이메일 매칭으로 풀린다(resolve_member_id 백필).
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from orthus import dashboard as d
from orthus.api.main import app
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import (
    auth_identities,
    dashboard_projects,
    personal_board_workspaces,
    personal_weekly_objectives,
    team_members,
    users,
    weekly_entries,
)

client = TestClient(app)

NODE = "company"
EMAIL = "2fe@acme.example"
WEEK = "2026-06-07"  # 일요일 시작 주차(회사·개인 동일 기준)


@pytest.fixture
def company_member(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """회사 노드 + owner-scope. team_members는 이메일만 있고 user_id 링크는 비워 둔다
    (이메일 매칭 백필 경로까지 검증)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", NODE)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    member_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=user_id,
                provider="dev",
                provider_subject=str(user_id),
                email=EMAIL,
                email_verified=True,
            )
        )
        s.execute(
            insert(team_members).values(
                member_id=member_id,
                node_id=NODE,
                name="박기획",
                email=EMAIL,  # user_id는 일부러 비워 둠
                sort_order=1,
                active=True,
            )
        )
        s.commit()
    return member_id


def _hdr(uid: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(uid)}


def _seed_company_plan(project_name: str, items: list[dict]) -> uuid.UUID:
    project_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(
                project_id=project_id, node_id=NODE, name=project_name, sort_order=0, active=True
            )
        )
        s.execute(
            insert(weekly_entries).values(
                entry_id=uuid.uuid4(),
                node_id=NODE,
                project_id=project_id,
                week_start=WEEK,
                plan_items=items,
                retro_items=[],
            )
        )
        s.commit()
    return project_id


def _set_plan_items(project_id: uuid.UUID, items: list[dict]) -> None:
    with session() as s:
        s.execute(
            update(weekly_entries)
            .where(
                weekly_entries.c.node_id == NODE,
                weekly_entries.c.project_id == project_id,
                weekly_entries.c.week_start == date.fromisoformat(WEEK),
            )
            .values(plan_items=items)
        )
        s.commit()


def _weekly(uid: uuid.UUID, week: str = WEEK) -> dict:
    return client.get(f"/personal-board/weekly-plan?week_start={week}", headers=_hdr(uid)).json()


def _company_objectives(body: dict) -> list[dict]:
    return [o for o in body["objectives"] if o.get("source_kind") == "company_plan"]


def test_assigned_company_plan_item_materializes_as_objective(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    _seed_company_plan(
        "아틀라스",
        [
            {
                "id": "p1",
                "text": "오디션 일정 확정",
                "done": False,
                "assignee_member_id": str(company_member),
            },
            {"id": "p2", "text": "남의 일", "done": False, "assignee_member_id": str(uuid.uuid4())},
        ],
    )

    # 보드 bootstrap 없이 주간플랜만 직접 열어도 동작해야 한다(이메일 매칭 백필).
    body = _weekly(user_id)
    company = _company_objectives(body)
    titles = [o["title"] for o in company]
    assert "오디션 일정 확정" in titles
    assert "남의 일" not in titles  # 다른 멤버에게 부여된 항목은 안 들어온다
    mine = next(o for o in company if o["title"] == "오디션 일정 확정")
    assert mine["completed"] is False
    assert mine["day_allocations"] == []  # 새로 생긴 목표는 요일 분배가 비어 있다
    # 읽기전용 box는 폐기됐다 — 목표로만 들어온다.
    assert body["assigned_company_items"] == []


def test_company_done_seeds_completed_on_create(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    _seed_company_plan(
        "노바",
        [
            {
                "id": "p1",
                "text": "완료된 일",
                "done": True,
                "assignee_member_id": str(company_member),
            }
        ],
    )
    company = _company_objectives(_weekly(user_id))
    assert len(company) == 1
    assert company[0]["completed"] is True


def test_unassigned_items_do_not_materialize(user_id: uuid.UUID, company_member: uuid.UUID) -> None:
    _seed_company_plan(
        "플레이스",
        [{"id": "p1", "text": "담당 없음", "done": False, "assignee_member_id": None}],
    )
    assert _company_objectives(_weekly(user_id)) == []


def test_other_week_items_do_not_leak(user_id: uuid.UUID, company_member: uuid.UUID) -> None:
    _seed_company_plan(
        "타임",
        [
            {
                "id": "p1",
                "text": "다른 주 업무",
                "done": False,
                "assignee_member_id": str(company_member),
            }
        ],
    )
    # 다른 주(2026-06-14)를 조회하면 안 나온다.
    assert _company_objectives(_weekly(user_id, "2026-06-14")) == []


def test_resync_preserves_day_allocations_and_completion(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    """개인이 요일 분배 + 완료를 설정한 뒤 다시 열어도(재동기화) 보존돼야 한다 —
    회사가 소유하는 건 제목뿐이고 분배·완료는 개인 소유다."""
    _seed_company_plan(
        "ML",
        [
            {
                "id": "p1",
                "text": "GPU 가동률",
                "done": False,
                "assignee_member_id": str(company_member),
            }
        ],
    )
    obj = _company_objectives(_weekly(user_id))[0]
    oid = obj["objective_id"]

    # 개인이 월요일(weekday=1)에 120분 분배 + 완료 토글.
    allocations = [{"weekday": 1, "minutes": 120, "order": 0, "note": "오전 집중"}]
    r1 = client.patch(
        f"/personal-board/weekly-plan/objectives/{oid}",
        json={"day_allocations": allocations, "completed": True},
        headers=_hdr(user_id),
    )
    assert r1.status_code == 200, r1.text

    # 다시 열면 재동기화가 돌지만 분배·완료는 살아 있어야 한다.
    again = _company_objectives(_weekly(user_id))[0]
    assert again["completed"] is True
    assert len(again["day_allocations"]) == 1
    assert again["day_allocations"][0]["weekday"] == 1
    assert again["day_allocations"][0]["minutes"] == 120


def test_company_title_change_resyncs_but_keeps_allocations(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    project_id = _seed_company_plan(
        "아틀라스2",
        [
            {
                "id": "p1",
                "text": "초안 제목",
                "done": False,
                "assignee_member_id": str(company_member),
            }
        ],
    )
    obj = _company_objectives(_weekly(user_id))[0]
    oid = obj["objective_id"]
    client.patch(
        f"/personal-board/weekly-plan/objectives/{oid}",
        json={"day_allocations": [{"weekday": 2, "minutes": 60, "order": 0, "note": None}]},
        headers=_hdr(user_id),
    )

    # 회사가 제목을 바꾸면 재동기화에서 제목만 갱신, 분배는 보존.
    _set_plan_items(
        project_id,
        [
            {
                "id": "p1",
                "text": "수정된 제목",
                "done": False,
                "assignee_member_id": str(company_member),
            }
        ],
    )
    again = _company_objectives(_weekly(user_id))[0]
    assert again["objective_id"] == oid  # 같은 목표(멱등)
    assert again["title"] == "수정된 제목"
    assert again["day_allocations"][0]["weekday"] == 2


def test_unassign_removes_company_objective(user_id: uuid.UUID, company_member: uuid.UUID) -> None:
    project_id = _seed_company_plan(
        "해제테스트",
        [
            {
                "id": "p1",
                "text": "곧 해제될 일",
                "done": False,
                "assignee_member_id": str(company_member),
            }
        ],
    )
    assert len(_company_objectives(_weekly(user_id))) == 1

    # 회사쪽에서 담당 해제 → 다음 동기화에서 stale 제거.
    _set_plan_items(
        project_id,
        [{"id": "p1", "text": "곧 해제될 일", "done": False, "assignee_member_id": None}],
    )
    assert _company_objectives(_weekly(user_id)) == []


def test_company_objective_title_and_delete_blocked(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    _seed_company_plan(
        "가드",
        [
            {
                "id": "p1",
                "text": "회사 소유 제목",
                "done": False,
                "assignee_member_id": str(company_member),
            }
        ],
    )
    obj = _company_objectives(_weekly(user_id))[0]
    oid = obj["objective_id"]

    # 제목 변경은 422로 막힌다.
    r_title = client.patch(
        f"/personal-board/weekly-plan/objectives/{oid}",
        json={"title": "내 맘대로 제목"},
        headers=_hdr(user_id),
    )
    assert r_title.status_code == 422

    # 삭제도 422로 막힌다.
    r_del = client.delete(f"/personal-board/weekly-plan/objectives/{oid}", headers=_hdr(user_id))
    assert r_del.status_code == 422

    # 완료 토글은 허용.
    r_done = client.patch(
        f"/personal-board/weekly-plan/objectives/{oid}",
        json={"completed": True},
        headers=_hdr(user_id),
    )
    assert r_done.status_code == 200, r_done.text


def test_manual_objective_still_editable_and_deletable(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    """가드는 회사 부여 목표에만 적용된다 — 수동 목표는 그대로 수정/삭제 가능."""
    created = client.post(
        "/personal-board/weekly-plan/objectives",
        json={"week_start": WEEK, "title": "내가 만든 목표"},
        headers=_hdr(user_id),
    )
    assert created.status_code == 201, created.text
    oid = created.json()["objective_id"]
    assert created.json()["source_kind"] == "manual"

    r_title = client.patch(
        f"/personal-board/weekly-plan/objectives/{oid}",
        json={"title": "바꾼 제목"},
        headers=_hdr(user_id),
    )
    assert r_title.status_code == 200, r_title.text
    r_del = client.delete(f"/personal-board/weekly-plan/objectives/{oid}", headers=_hdr(user_id))
    assert r_del.status_code == 204


@pytest.fixture
def company_member_space_emails(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """team_members.email이 공백으로 구분된 멤버(예: 김하늘 데모 row). user_id 링크는
    비어 있어 이메일 매칭 백필 경로로만 풀려야 한다. resolve_member_id가 콤마뿐 아니라
    공백 구분자도 split해야 매칭된다(회귀 방지)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", NODE)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    member_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=user_id,
                provider="dev",
                provider_subject=str(user_id),
                email=EMAIL,
                email_verified=True,
            )
        )
        s.execute(
            insert(team_members).values(
                member_id=member_id,
                node_id=NODE,
                name="김하늘",
                # 공백 구분(콤마 없음) — prod에서 매칭 실패 원인
                email=f"other@nova.example {EMAIL} third@gmail.com",
                sort_order=2,
                active=True,
            )
        )
        s.commit()
    return member_id


def test_space_separated_member_email_resolves(
    user_id: uuid.UUID, company_member_space_emails: uuid.UUID
) -> None:
    _seed_company_plan(
        "ML2",
        [
            {
                "id": "p1",
                "text": "GPU 가동률 70프로",
                "done": False,
                "assignee_member_id": str(company_member_space_emails),
            }
        ],
    )
    titles = [o["title"] for o in _company_objectives(_weekly(user_id))]
    assert "GPU 가동률 70프로" in titles


def _add_subtask(uid: uuid.UUID, oid: str, title: str, weekday: int = 1) -> str:
    r = client.post(
        f"/personal-board/weekly-plan/objectives/{oid}/subtasks",
        json={"title": title, "weekday": weekday},
        headers=_hdr(uid),
    )
    assert r.status_code == 201, r.text
    return r.json()["subtask_id"]


def _toggle_subtask(uid: uuid.UUID, sid: str, done: bool) -> None:
    r = client.patch(
        f"/personal-board/weekly-plan/objective-subtasks/{sid}",
        json={"completed": done},
        headers=_hdr(uid),
    )
    assert r.status_code == 200, r.text


def test_subtask_autocomplete_drives_objective_completion(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    """서브태스크가 다 끝나면 목표 자동 완료, 하나라도 미완이면 자동 재오픈."""
    _seed_company_plan(
        "AC",
        [{"id": "p1", "text": "쪼갤 일", "done": False, "assignee_member_id": str(company_member)}],
    )
    oid = _company_objectives(_weekly(user_id))[0]["objective_id"]
    s1 = _add_subtask(user_id, oid, "A")
    s2 = _add_subtask(user_id, oid, "B")
    # 서브태스크가 생기면(미완) 목표는 미완.
    assert _company_objectives(_weekly(user_id))[0]["completed"] is False

    _toggle_subtask(user_id, s1, True)
    assert _company_objectives(_weekly(user_id))[0]["completed"] is False  # 1/2
    _toggle_subtask(user_id, s2, True)
    assert _company_objectives(_weekly(user_id))[0]["completed"] is True  # 2/2 → 자동 완료

    _toggle_subtask(user_id, s1, False)
    assert _company_objectives(_weekly(user_id))[0]["completed"] is False  # 재오픈


def test_company_rollup_shows_assignee_subtask_progress(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    """회사 plans 보드(get_weekly)가 담당자 진행률(집계만)을 돌려준다 — 사적 디테일 없음."""
    from orthus.dashboard import get_weekly

    project_id = _seed_company_plan(
        "RU",
        [
            {
                "id": "p1",
                "text": "추적될 일",
                "done": False,
                "assignee_member_id": str(company_member),
            }
        ],
    )
    oid = _company_objectives(_weekly(user_id))[0]["objective_id"]  # materialize
    s1 = _add_subtask(user_id, oid, "A")
    _add_subtask(user_id, oid, "B")
    _toggle_subtask(user_id, s1, True)

    entry = get_weekly(NODE, project_id, date.fromisoformat(WEEK))
    prog = entry.assignee_progress["p1"]
    assert prog.has_objective is True
    assert prog.subtasks_total == 2
    assert prog.subtasks_done == 1
    assert prog.completed is False
    # 집계 필드만 — 서브태스크 제목/메모 같은 사적 내용은 모델에 없다.
    assert set(prog.model_dump().keys()) == {
        "has_objective",
        "subtasks_total",
        "subtasks_done",
        "completed",
    }


def test_company_rollup_no_objective_when_assignee_never_opened(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    """담당자가 주간플랜을 아직 안 열었으면 목표가 없어 has_objective=False."""
    from orthus.dashboard import get_weekly

    project_id = _seed_company_plan(
        "RU2",
        [{"id": "p1", "text": "미시작", "done": False, "assignee_member_id": str(company_member)}],
    )
    entry = get_weekly(NODE, project_id, date.fromisoformat(WEEK))
    prog = entry.assignee_progress["p1"]
    assert prog.has_objective is False
    assert prog.subtasks_total == 0
    assert prog.completed is False


def test_company_rollup_empty_when_no_assignees(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    from orthus.dashboard import get_weekly

    project_id = _seed_company_plan(
        "RU3",
        [{"id": "p1", "text": "담당 없음", "done": False, "assignee_member_id": None}],
    )
    entry = get_weekly(NODE, project_id, date.fromisoformat(WEEK))
    assert entry.assignee_progress == {}


# ---------------------------------------------------------------------------
# push: assigner가 계획을 저장(upsert_weekly)하는 즉시 담당자 보드에 반영 —
# 담당자가 자기 보드를 열 때까지 기다리지 않는다("자동으로 안 들어감" 수정).
# ---------------------------------------------------------------------------
def _linked_member(name: str, email: str) -> tuple[uuid.UUID, uuid.UUID]:
    """user_id가 연결된 팀원(assigner 저장 시 즉시 push 대상)."""
    uid = uuid.uuid4()
    member_id = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name=name))
        s.execute(
            insert(team_members).values(
                member_id=member_id,
                node_id=NODE,
                name=name,
                email=email,
                user_id=uid,
                sort_order=5,
                active=True,
            )
        )
        s.commit()
    return uid, member_id


@pytest.fixture
def member_b(company_member: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    # company_member 픽스처가 node/owner-scope 세팅 + caller(2fe) 매핑을 잡아 준다.
    return _linked_member("팀원B", "teamb@acme.example")


@pytest.fixture
def member_c(company_member: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return _linked_member("팀원C", "teamc@acme.example")


def _new_project(name: str) -> uuid.UUID:
    project_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(
                project_id=project_id, node_id=NODE, name=name, sort_order=0, active=True
            )
        )
        s.commit()
    return project_id


def _save_plan(project_id: uuid.UUID, items: list[dict], week: str = WEEK) -> None:
    """assigner가 /dashboard/plans에서 계획을 저장하는 경로(upsert_weekly)."""
    d.upsert_weekly(
        NODE,
        d.WeeklyUpsert(
            project_id=project_id,
            week_start=date.fromisoformat(week),
            plan_items=[d.PlanItem(**it) for it in items],
        ),
    )


def _company_objs_for_user(uid: uuid.UUID) -> list:
    """담당자 workspace의 회사 부여 목표를 DB에서 직접 읽는다(그 사람이 보드를 열지 않아도)."""
    with session() as s:
        ws = s.execute(
            select(personal_board_workspaces.c.workspace_id).where(
                personal_board_workspaces.c.user_id == uid,
                personal_board_workspaces.c.node_id == NODE,
            )
        ).first()
        if ws is None:
            return []
        rows = s.execute(
            select(
                personal_weekly_objectives.c.title,
                personal_weekly_objectives.c.week_start,
                personal_weekly_objectives.c.source_plan_item_id,
            ).where(
                personal_weekly_objectives.c.workspace_id == ws.workspace_id,
                personal_weekly_objectives.c.source_kind == "company_plan",
            )
        ).all()
    return rows


def test_assign_pushes_to_member_board_without_opening(
    user_id: uuid.UUID, member_b: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """담당자가 자기 보드를 한 번도 안 열었어도, assigner가 지정+저장하면 즉시 반영된다."""
    uid_b, mid_b = member_b
    project_id = _new_project("푸시")
    _save_plan(
        project_id,
        [{"id": "p1", "text": "B에게 맡긴 일", "assignee_member_id": str(mid_b)}],
    )
    objs = _company_objs_for_user(uid_b)
    assert [o.title for o in objs] == ["B에게 맡긴 일"]
    # assigner 롤업도 즉시 has_objective=True(예전엔 담당자가 열기 전까지 False였다).
    entry = d.get_weekly(NODE, project_id, date.fromisoformat(WEEK))
    assert entry.assignee_progress["p1"].has_objective is True


def test_unassign_on_save_removes_member_objective(
    user_id: uuid.UUID, member_b: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """담당 해제 후 저장하면 그 사람 보드에서 stale 제거된다(저장 즉시)."""
    uid_b, mid_b = member_b
    project_id = _new_project("푸시해제")
    _save_plan(project_id, [{"id": "p1", "text": "곧 해제", "assignee_member_id": str(mid_b)}])
    assert len(_company_objs_for_user(uid_b)) == 1

    _save_plan(project_id, [{"id": "p1", "text": "곧 해제", "assignee_member_id": None}])
    assert _company_objs_for_user(uid_b) == []


def test_reassign_between_members_on_save(
    user_id: uuid.UUID,
    member_b: tuple[uuid.UUID, uuid.UUID],
    member_c: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """담당을 B→C로 바꿔 저장하면 B 보드에서 빠지고 C 보드에 들어간다(old∪new 합집합 push)."""
    uid_b, mid_b = member_b
    uid_c, mid_c = member_c
    project_id = _new_project("재지정")
    _save_plan(project_id, [{"id": "p1", "text": "넘길 일", "assignee_member_id": str(mid_b)}])
    assert len(_company_objs_for_user(uid_b)) == 1
    assert _company_objs_for_user(uid_c) == []

    _save_plan(project_id, [{"id": "p1", "text": "넘길 일", "assignee_member_id": str(mid_c)}])
    assert _company_objs_for_user(uid_b) == []  # 이전 담당 B는 정리
    assert [o.title for o in _company_objs_for_user(uid_c)] == ["넘길 일"]  # 새 담당 C에 반영


def test_delete_plan_item_removes_member_objective_on_save(
    user_id: uuid.UUID, member_b: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """계획에서 담당 항목을 '삭제'(배열에서 제거)하고 저장하면, 그 항목 목표만 담당자
    보드에서 즉시 사라지고 나머지 담당 항목은 남는다(add의 반대 방향도 즉시 동기화)."""
    uid_b, mid_b = member_b
    project_id = _new_project("삭제")
    _save_plan(
        project_id,
        [
            {"id": "d1", "text": "지울 일", "assignee_member_id": str(mid_b)},
            {"id": "d2", "text": "남길 일", "assignee_member_id": str(mid_b)},
        ],
    )
    assert sorted(o.title for o in _company_objs_for_user(uid_b)) == ["남길 일", "지울 일"]

    # d1 삭제(배열에서 제거)하고 저장 → d1 목표만 제거, d2는 유지.
    _save_plan(project_id, [{"id": "d2", "text": "남길 일", "assignee_member_id": str(mid_b)}])
    assert [o.title for o in _company_objs_for_user(uid_b)] == ["남길 일"]


def test_clear_all_plan_items_clears_member_objectives_on_save(
    user_id: uuid.UUID, member_b: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """마지막 담당 항목까지 지워 계획을 비우면(파괴적 empty 가드 통과 시) 담당자 보드의
    회사 부여 목표도 즉시 비워진다."""
    uid_b, mid_b = member_b
    project_id = _new_project("전체삭제")
    _save_plan(project_id, [{"id": "z1", "text": "마지막 일", "assignee_member_id": str(mid_b)}])
    assert len(_company_objs_for_user(uid_b)) == 1

    # 빈 저장은 파괴적 empty 가드가 base_updated_at 일치를 요구한다(우발적 초기화 방지).
    base = d.get_weekly(NODE, project_id, date.fromisoformat(WEEK)).updated_at
    d.upsert_weekly(
        NODE,
        d.WeeklyUpsert(
            project_id=project_id,
            week_start=date.fromisoformat(WEEK),
            plan_items=[],
            base_updated_at=base,
        ),
    )
    assert _company_objs_for_user(uid_b) == []


def test_two_projects_same_week_keep_both_member_objectives(
    user_id: uuid.UUID, member_b: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """같은 주 두 프로젝트에 B가 담당인 항목을 각각 저장 — 둘째 저장의 stale-delete가
    첫째 프로젝트 목표를 지우면 안 된다(materialize는 그 주 모든 프로젝트를 keep-set에
    넣는다; advisory lock이 동시 저장에서도 이 keep-set을 최신으로 유지한다)."""
    uid_b, mid_b = member_b
    p1 = _new_project("멀티P1")
    p2 = _new_project("멀티P2")
    _save_plan(p1, [{"id": "a1", "text": "P1 일", "assignee_member_id": str(mid_b)}])
    assert len(_company_objs_for_user(uid_b)) == 1
    _save_plan(p2, [{"id": "a2", "text": "P2 일", "assignee_member_id": str(mid_b)}])
    titles = sorted(o.title for o in _company_objs_for_user(uid_b))
    assert titles == ["P1 일", "P2 일"]  # 둘 다 살아 있어야 한다


def test_unlinked_member_skipped_by_push_then_pull_backfills(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    """user_id 미연결 팀원은 push에서 건너뛰고, 그 사람이 보드를 열 때 pull이 백필한다."""
    project_id = _new_project("미연결")
    _save_plan(
        project_id,
        [{"id": "p1", "text": "미연결 담당", "assignee_member_id": str(company_member)}],
    )
    # push는 user_id 미연결(company_member는 이메일만) 멤버를 건너뛴다 — 아직 목표 없음.
    entry = d.get_weekly(NODE, project_id, date.fromisoformat(WEEK))
    assert entry.assignee_progress["p1"].has_objective is False
    # 담당자(2fe, 이메일 매칭)가 보드를 열면 pull이 백필.
    titles = [o["title"] for o in _company_objectives(_weekly(user_id))]
    assert "미연결 담당" in titles


def test_company_item_moved_to_next_week_follows(
    user_id: uuid.UUID, company_member: uuid.UUID
) -> None:
    """회사 항목이 다른 주로 옮겨지면 목표도 같은 objective로 그 주에 따라간다(양쪽 실종 방지).

    이전엔 ON CONFLICT가 week_start를 안 옮겨, 이동 시 옛 주차에 남았다가 stale-delete로
    사라져 두 주 모두에서 안 보였다."""
    next_week = "2026-06-14"
    project_id = _seed_company_plan(
        "이동",
        [{"id": "p1", "text": "옮길 일", "done": False, "assignee_member_id": str(company_member)}],
    )
    first = _company_objectives(_weekly(user_id, WEEK))
    assert len(first) == 1
    oid = first[0]["objective_id"]

    # 회사쪽에서 W1 비우고 W2에 같은 id로 추가(항목 이동).
    _set_plan_items(project_id, [])
    with session() as s:
        s.execute(
            insert(weekly_entries).values(
                entry_id=uuid.uuid4(),
                node_id=NODE,
                project_id=project_id,
                week_start=next_week,
                plan_items=[
                    {
                        "id": "p1",
                        "text": "옮길 일",
                        "done": False,
                        "assignee_member_id": str(company_member),
                    }
                ],
                retro_items=[],
            )
        )
        s.commit()

    moved = _company_objectives(_weekly(user_id, next_week))
    assert len(moved) == 1
    assert moved[0]["objective_id"] == oid  # 같은 목표가 이동(중복 생성 아님)
    assert _company_objectives(_weekly(user_id, WEEK)) == []  # 옛 주차엔 더 이상 없다
