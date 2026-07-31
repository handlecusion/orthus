"""대시보드 프로젝트 역할 옵션 — 노션식 선택 속성처럼 추가/이름변경/색/삭제.

이름을 바꾸면 그 역할을 쓰던 배정(project_assignments.role)에 cascade 반영되고,
삭제하면 해당 배정의 역할이 비워진다.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from orthus.api.main import app
from orthus.settings import get_settings

client = TestClient(app)


def H(user_id: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


@pytest.fixture
def company_user(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    return user_id


def _roles(uid: uuid.UUID) -> list[dict]:
    return client.get("/dashboard/project-roles", headers=H(uid)).json()


def test_list_seeds_defaults(company_user: uuid.UUID) -> None:
    roles = _roles(company_user)
    names = [r["name"] for r in roles]
    assert "PM" in names and "개발" in names and "기타" in names
    pm = next(r for r in roles if r["name"] == "PM")
    assert pm["color"] == "blue"


def test_create_rename_recolor_delete(company_user: uuid.UUID) -> None:
    uid = company_user
    # 추가
    created = client.post(
        "/dashboard/project-roles", json={"name": "QA", "color": "teal"}, headers=H(uid)
    )
    assert created.status_code == 201, created.text
    role_id = created.json()["role_id"]
    assert created.json()["color"] == "teal"
    assert "QA" in [r["name"] for r in _roles(uid)]

    # 색 변경
    recolored = client.patch(
        f"/dashboard/project-roles/{role_id}", json={"color": "red"}, headers=H(uid)
    )
    assert recolored.status_code == 200
    assert recolored.json()["color"] == "red"
    assert recolored.json()["name"] == "QA"

    # 이름 변경
    renamed = client.patch(
        f"/dashboard/project-roles/{role_id}", json={"name": "품질"}, headers=H(uid)
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "품질"
    names = [r["name"] for r in _roles(uid)]
    assert "품질" in names and "QA" not in names

    # 삭제
    deleted = client.delete(f"/dashboard/project-roles/{role_id}", headers=H(uid))
    assert deleted.status_code == 204
    assert "품질" not in [r["name"] for r in _roles(uid)]


def _make_assignment_with_role(uid: uuid.UUID, role: str) -> str:
    project_id = client.post(
        "/dashboard/projects",
        json={"name": f"P-{uuid.uuid4().hex[:6]}", "color": None, "sort_order": 0, "active": True},
        headers=H(uid),
    ).json()["project_id"]
    member_id = client.post(
        "/dashboard/team-members", json={"name": "박기획"}, headers=H(uid)
    ).json()["member_id"]
    asg = client.post(
        "/dashboard/assignments",
        json={"project_id": project_id, "member_id": member_id, "role": role, "sort_order": 0},
        headers=H(uid),
    )
    assert asg.status_code == 201, asg.text
    return asg.json()["assignment_id"]


def test_rename_cascades_to_assignments(company_user: uuid.UUID) -> None:
    uid = company_user
    _roles(uid)  # seed defaults
    role_id = next(r["role_id"] for r in _roles(uid) if r["name"] == "개발")
    _make_assignment_with_role(uid, "개발")

    client.patch(
        f"/dashboard/project-roles/{role_id}", json={"name": "엔지니어링"}, headers=H(uid)
    )
    asg = client.get("/dashboard/assignments", headers=H(uid)).json()
    roles_used = [a["role"] for a in asg]
    assert "엔지니어링" in roles_used and "개발" not in roles_used


def test_delete_clears_assignment_role(company_user: uuid.UUID) -> None:
    uid = company_user
    _roles(uid)
    role_id = next(r["role_id"] for r in _roles(uid) if r["name"] == "디자인")
    _make_assignment_with_role(uid, "디자인")

    client.delete(f"/dashboard/project-roles/{role_id}", headers=H(uid))
    asg = client.get("/dashboard/assignments", headers=H(uid)).json()
    assert all(a["role"] != "디자인" for a in asg)
    assert any(a["role"] is None for a in asg)


def test_rename_to_existing_name_rejected(company_user: uuid.UUID) -> None:
    uid = company_user
    _roles(uid)
    dev_id = next(r["role_id"] for r in _roles(uid) if r["name"] == "개발")
    out = client.patch(
        f"/dashboard/project-roles/{dev_id}", json={"name": "기획"}, headers=H(uid)
    )
    assert out.status_code == 422
