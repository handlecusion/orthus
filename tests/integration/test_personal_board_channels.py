"""내 보드 채널 분리: 개인 채널 vs 회사 프로젝트 채널.

회사 노드 + owner-scope에서, 내가 담당(project_assignments)인 회사 프로젝트가 내 보드에
'회사 채널'로 자동으로 뜨고(이름 매칭 시 기존 개인 채널이 회사 채널로 전환), 그 채널
업무가 scope='company'로 회사 구성원 전체에게 데이터로 보이는지 검증한다. 사적 채널
업무(scope='personal')는 회사 집계에 절대 새지 않는다(경계).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select

from orthus.api.main import app
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import (
    auth_identities,
    dashboard_projects,
    personal_board_projects,
    personal_board_tasks,
    project_assignments,
    team_members,
)

client = TestClient(app)

NODE = "company"
EMAIL = "2fe@acme.example"


@dataclass
class Owner:
    user_id: uuid.UUID
    member_id: uuid.UUID


@pytest.fixture
def company_owner(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> Owner:
    """회사 노드 + owner-scope에서 team_members로 연결된 로그인 계정."""
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
                email=EMAIL,
                user_id=user_id,
                color="#5d9bd8",
                sort_order=1,
                active=True,
            )
        )
        s.commit()
    return Owner(user_id=user_id, member_id=member_id)


def _hdr(uid: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(uid)}


def _assign_project(member_id: uuid.UUID, name: str, color: str = "#abcabc") -> uuid.UUID:
    """회사 프로젝트를 만들고 member를 담당으로 배정한다."""
    project_id = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(dashboard_projects).values(
                project_id=project_id,
                node_id=NODE,
                name=name,
                color=color,
                sort_order=0,
                active=True,
            )
        )
        s.execute(
            insert(project_assignments).values(
                assignment_id=uuid.uuid4(),
                node_id=NODE,
                project_id=project_id,
                member_id=member_id,
                role="담당",
                sort_order=0,
            )
        )
        s.commit()
    return project_id


def _channels(body: dict) -> dict[str, dict]:
    return {p["name"]: p for p in body["projects"]}


def test_assigned_project_appears_as_company_channel(company_owner: Owner) -> None:
    project_id = _assign_project(company_owner.member_id, "아틀라스")

    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(company_owner.user_id)).json()
    channels = _channels(body)
    assert "아틀라스" in channels
    chan = channels["아틀라스"]
    assert chan["kind"] == "company"
    assert chan["company_project_id"] == str(project_id)


def test_name_matching_personal_channel_becomes_company_and_migrates_tasks(
    company_owner: Owner,
) -> None:
    uid = company_owner.user_id
    # 1) 먼저 같은 이름의 개인 채널과 그 안의 개인 업무를 만든다.
    client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid))
    chan = client.post(
        "/personal-board/projects", json={"name": "Nova"}, headers=_hdr(uid)
    ).json()
    assert chan["kind"] == "personal"
    chan_id = chan["project_id"]
    task = client.post(
        "/personal-board/tasks",
        json={"title": "ML 파이프라인", "scheduled_date": "2026-06-05", "project_id": chan_id},
        headers=_hdr(uid),
    ).json()
    assert task["scope"] == "personal"
    task_id = task["task_id"]

    # 2) 이름이 같은 회사 프로젝트에 담당 배정 → 다음 bootstrap에서 회사 채널로 전환.
    project_id = _assign_project(company_owner.member_id, "Nova")
    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    channels = _channels(body)
    # 채널은 그대로 하나(이름 유니크)인데 kind가 company로 바뀐다.
    assert channels["Nova"]["project_id"] == chan_id
    assert channels["Nova"]["kind"] == "company"
    assert channels["Nova"]["company_project_id"] == str(project_id)
    # 그 안에 있던 기존 개인 업무도 회사 공개로 승격된다.
    day = next(d for d in body["days"] if d["date"] == "2026-06-05")
    migrated = next(t for t in day["tasks"] if t["task_id"] == task_id)
    assert migrated["scope"] == "company"
    assert migrated["company_project_id"] == str(project_id)

    # 3) 회사쪽에서 그 프로젝트 업무로 보인다.
    company_view = client.get(
        f"/dashboard/projects/{project_id}/board-tasks", headers=_hdr(uid)
    ).json()
    assert [t["task_id"] for t in company_view] == [task_id]
    assert company_view[0]["owner_name"] == "박기획"


def test_task_created_in_company_channel_is_company_scope(company_owner: Owner) -> None:
    uid = company_owner.user_id
    project_id = _assign_project(company_owner.member_id, "플레이스")
    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    chan_id = _channels(body)["플레이스"]["project_id"]

    task = client.post(
        "/personal-board/tasks",
        json={"title": "랜딩 페이지", "scheduled_date": "2026-06-05", "project_id": chan_id},
        headers=_hdr(uid),
    ).json()
    assert task["scope"] == "company"
    assert task["company_project_id"] == str(project_id)

    company_view = client.get(
        f"/dashboard/projects/{project_id}/board-tasks", headers=_hdr(uid)
    ).json()
    assert [t["task_id"] for t in company_view] == [task["task_id"]]


def test_company_read_excludes_personal_channel_tasks(company_owner: Owner) -> None:
    """경계: 회사 집계는 scope='company'만 본다. 사적 채널 업무는 절대 안 샌다."""
    uid = company_owner.user_id
    project_id = _assign_project(company_owner.member_id, "공용프로젝트")
    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    company_chan_id = _channels(body)["공용프로젝트"]["project_id"]

    # 개인 채널 + 개인 업무
    personal_chan = client.post(
        "/personal-board/projects", json={"name": "비밀메모"}, headers=_hdr(uid)
    ).json()
    client.post(
        "/personal-board/tasks",
        json={"title": "사적인 일", "scheduled_date": "2026-06-05", "project_id": personal_chan["project_id"]},
        headers=_hdr(uid),
    )
    # 회사 채널 업무
    company_task = client.post(
        "/personal-board/tasks",
        json={"title": "회사 일", "scheduled_date": "2026-06-05", "project_id": company_chan_id},
        headers=_hdr(uid),
    ).json()

    company_view = client.get(
        f"/dashboard/projects/{project_id}/board-tasks", headers=_hdr(uid)
    ).json()
    titles = [t["title"] for t in company_view]
    assert titles == ["회사 일"]
    assert company_task["title"] not in [t["title"] for t in company_view if t["title"] != "회사 일"]


def test_moving_company_task_to_personal_channel_drops_company_scope(company_owner: Owner) -> None:
    uid = company_owner.user_id
    project_id = _assign_project(company_owner.member_id, "이동테스트")
    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    company_chan_id = _channels(body)["이동테스트"]["project_id"]
    personal_chan = client.post(
        "/personal-board/projects", json={"name": "개인함"}, headers=_hdr(uid)
    ).json()

    task = client.post(
        "/personal-board/tasks",
        json={"title": "옮길 일", "scheduled_date": "2026-06-05", "project_id": company_chan_id},
        headers=_hdr(uid),
    ).json()
    assert task["scope"] == "company"

    moved = client.patch(
        f"/personal-board/tasks/{task['task_id']}",
        json={"project_id": personal_chan["project_id"]},
        headers=_hdr(uid),
    ).json()
    assert moved["scope"] == "personal"
    assert moved["company_project_id"] is None
    # 회사 집계에서 사라진다.
    company_view = client.get(
        f"/dashboard/projects/{project_id}/board-tasks", headers=_hdr(uid)
    ).json()
    assert company_view == []


def test_unassign_reverts_channel_and_tasks_to_personal(company_owner: Owner) -> None:
    uid = company_owner.user_id
    project_id = _assign_project(company_owner.member_id, "되돌림")
    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    chan_id = _channels(body)["되돌림"]["project_id"]
    task_id = client.post(
        "/personal-board/tasks",
        json={"title": "복귀 일", "scheduled_date": "2026-06-05", "project_id": chan_id},
        headers=_hdr(uid),
    ).json()["task_id"]

    # 담당 해제
    with session() as s:
        s.execute(delete(project_assignments).where(project_assignments.c.project_id == project_id))
        s.commit()

    body2 = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    chan = _channels(body2)["되돌림"]
    assert chan["kind"] == "personal"
    assert chan["company_project_id"] is None
    day = next(d for d in body2["days"] if d["date"] == "2026-06-05")
    reverted = next(t for t in day["tasks"] if t["task_id"] == task_id)
    assert reverted["scope"] == "personal"
    # 회사쪽에서도 더 이상 안 보인다.
    assert client.get(f"/dashboard/projects/{project_id}/board-tasks", headers=_hdr(uid)).json() == []


def test_company_channel_is_read_only_from_board(company_owner: Owner) -> None:
    uid = company_owner.user_id
    _assign_project(company_owner.member_id, "잠금")
    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    chan_id = _channels(body)["잠금"]["project_id"]

    renamed = client.patch(
        f"/personal-board/projects/{chan_id}", json={"name": "딴이름"}, headers=_hdr(uid)
    )
    assert renamed.status_code == 422
    deleted = client.delete(f"/personal-board/projects/{chan_id}", headers=_hdr(uid))
    assert deleted.status_code == 422


def test_deleted_channel_is_not_resurrected_by_same_named_company_project(
    company_owner: Owner,
) -> None:
    """경계: 소프트삭제된 동명 개인 채널은 회사 프로젝트 배정으로 되살아나지 않고,
    그 잔존 개인 업무가 회사로 새지 않는다(아카이브 채널 부활 누수 차단)."""
    uid = company_owner.user_id
    chan = client.post(
        "/personal-board/projects", json={"name": "리서치"}, headers=_hdr(uid)
    ).json()
    old_chan_id = chan["project_id"]
    secret = client.post(
        "/personal-board/tasks",
        json={"title": "사적 리서치 메모", "scheduled_date": "2026-06-05", "project_id": old_chan_id},
        headers=_hdr(uid),
    ).json()
    secret_id = secret["task_id"]
    # 채널 삭제(소프트삭제) — 업무는 미지정으로 남고 scope는 personal 유지.
    assert client.delete(f"/personal-board/projects/{old_chan_id}", headers=_hdr(uid)).status_code == 204

    # 이름이 같은 회사 프로젝트에 배정 후 보드 재로드.
    project_id = _assign_project(company_owner.member_id, "리서치")
    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    # 새 회사 채널은 삭제된 채널과 다른 row(다른 id)다.
    company_chan = _channels(body)["리서치"]
    assert company_chan["kind"] == "company"
    assert company_chan["project_id"] != old_chan_id
    # 삭제된 채널의 잔존 업무는 여전히 개인(scope='personal')이고 회사로 안 샌다.
    with session() as s:
        scope = s.execute(
            select(personal_board_tasks.c.scope).where(
                personal_board_tasks.c.task_id == uuid.UUID(secret_id)
            )
        ).scalar_one()
    assert scope == "personal"
    company_view = client.get(
        f"/dashboard/projects/{project_id}/board-tasks", headers=_hdr(uid)
    ).json()
    assert all(t["task_id"] != secret_id for t in company_view)


def test_create_personal_channel_named_like_company_channel_is_rejected(
    company_owner: Owner,
) -> None:
    uid = company_owner.user_id
    _assign_project(company_owner.member_id, "겹침")
    client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid))  # 회사 채널 생성
    dup = client.post("/personal-board/projects", json={"name": "겹침"}, headers=_hdr(uid))
    assert dup.status_code == 422


def test_company_read_drops_unassigned_owner_without_owner_rebootstrap(
    company_owner: Owner,
) -> None:
    """담당이 빠지면 소유자가 보드를 다시 안 열어도 회사쪽 노출은 즉시 끊긴다."""
    uid = company_owner.user_id
    project_id = _assign_project(company_owner.member_id, "권위경계")
    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(uid)).json()
    chan_id = _channels(body)["권위경계"]["project_id"]
    client.post(
        "/personal-board/tasks",
        json={"title": "회사 업무", "scheduled_date": "2026-06-05", "project_id": chan_id},
        headers=_hdr(uid),
    )
    # 배정 동안엔 보인다.
    assert len(client.get(f"/dashboard/projects/{project_id}/board-tasks", headers=_hdr(uid)).json()) == 1

    # 담당 해제 — 소유자는 보드를 다시 열지 않는다(scope는 아직 'company'로 남아 있음).
    with session() as s:
        s.execute(delete(project_assignments).where(project_assignments.c.project_id == project_id))
        s.commit()
    # 그래도 회사 읽기는 즉시 비어 있어야 한다(읽기 시점 담당 재검증).
    assert client.get(f"/dashboard/projects/{project_id}/board-tasks", headers=_hdr(uid)).json() == []


def test_personal_node_has_no_company_channels(
    company_owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """개인 노드에서는 회사 채널이 뜨지 않는다(담당/팀멤버가 있어도 no-op)."""
    _assign_project(company_owner.member_id, "회사전용")
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    body = client.get(
        "/personal-board/bootstrap?date=2026-06-05", headers=_hdr(company_owner.user_id)
    ).json()
    assert all(p["kind"] == "personal" for p in body["projects"])
    assert "회사전용" not in _channels(body)


def test_non_member_account_gets_no_company_channels(
    user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """팀멤버로 연결되지 않은 계정은 회사 채널이 없다(조용한 no-op)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", NODE)
    monkeypatch.setattr(settings, "owner_scope_enabled", True)
    # 담당이 있는 회사 프로젝트가 있어도, 이 계정은 그 member가 아니다.
    other_member = uuid.uuid4()
    with session() as s:
        s.execute(
            insert(team_members).values(
                member_id=other_member, node_id=NODE, name="남", email="other@x.kr",
                sort_order=1, active=True,
            )
        )
        s.commit()
    _assign_project(other_member, "남의프로젝트")

    body = client.get("/personal-board/bootstrap?date=2026-06-05", headers=_hdr(user_id)).json()
    assert _channels(body) == {} or all(p["kind"] == "personal" for p in body["projects"])
    with session() as s:
        rows = s.execute(
            select(personal_board_projects.c.kind).where(
                personal_board_projects.c.kind == "company"
            )
        ).all()
    # 회사 채널이 이 워크스페이스에 만들어지지 않았다.
    assert all(r.kind != "company" for r in rows) or not rows
