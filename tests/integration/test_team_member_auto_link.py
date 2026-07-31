"""신규 팀원 자동 연결 (foundation): member ↔ user 링크 + allowlist 초대.

회사 노드에서 팀원을 추가하면
  (1) 같은 이메일로 이미 로그인한 user가 있으면 team_members.user_id를 즉시 backfill하고,
  (2) 그 이메일을 allowlist에 'member'로 초대(insert-only)해 로그인할 수 있게 한다.
로그인 시에는 create_session_token이 link_member_for_login으로 역방향 링크를
best-effort로 채운다. 둘 다 내 보드/배정이 곧바로 이어지게 하는 토대다.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus import dashboard as d
from orthus.api.main import app
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import auth_allowlist, auth_identities, team_members, users

client = TestClient(app)
NODE = "company"


@pytest.fixture
def company_admin(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Demo-mode operator on a company node (require_node_operator bypasses demo)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", NODE)
    return user_id


def _hdr(uid: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(uid)}


def _active_role(email: str) -> str | None:
    with session() as s:
        row = s.execute(
            select(auth_allowlist.c.role).where(
                auth_allowlist.c.node_id == NODE,
                auth_allowlist.c.email == email,
                auth_allowlist.c.revoked_at.is_(None),
            )
        ).first()
    return row.role if row else None


def _add_logged_in_user(email: str) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(insert(users).values(user_id=uid, display_name=email))
        s.execute(
            insert(auth_identities).values(
                identity_id=uuid.uuid4(),
                user_id=uid,
                provider="dev",
                provider_subject=str(uid),
                email=email,
                email_verified=True,
            )
        )
        s.commit()
    return uid


def test_create_member_auto_invites_to_allowlist(company_admin: uuid.UUID) -> None:
    res = client.post(
        "/dashboard/team-members",
        json={"name": "새 팀원", "email": "newbie@acme.example", "color": "#5d9bd8"},
        headers=_hdr(company_admin),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    # email은 있지만 아직 그 계정이 없으니: 초대는 됐고 링크는 안 됨
    assert body["invited"] is True
    assert body["linked"] is False
    assert body["user_id"] is None
    assert _active_role("newbie@acme.example") == "member"


def test_create_member_with_no_email_is_neither_linked_nor_invited(
    company_admin: uuid.UUID,
) -> None:
    res = client.post(
        "/dashboard/team-members",
        json={"name": "이메일없음"},
        headers=_hdr(company_admin),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["invited"] is False
    assert body["linked"] is False


def test_create_member_links_existing_logged_in_user(company_admin: uuid.UUID) -> None:
    teammate = _add_logged_in_user("teammate@acme.example")
    res = client.post(
        "/dashboard/team-members",
        json={"name": "동료", "email": "teammate@acme.example"},
        headers=_hdr(company_admin),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["linked"] is True
    assert body["user_id"] == str(teammate)
    assert body["invited"] is True
    # DB에도 backfill됐다
    with session() as s:
        linked = s.execute(
            select(team_members.c.user_id).where(team_members.c.member_id == uuid.UUID(body["member_id"]))
        ).one()
    assert linked.user_id == teammate


def test_login_hook_links_unlinked_member(company_admin: uuid.UUID) -> None:
    teammate = _add_logged_in_user("late@acme.example")
    with session() as s:
        s.execute(
            insert(team_members).values(
                member_id=uuid.uuid4(),
                node_id=NODE,
                name="늦깎이",
                email="late@acme.example",
                color="#9b68d6",
                sort_order=1,
                active=True,
            )
        )
        s.commit()
    # 로그인 전: 미연결
    late = next(m for m in d.list_team_members(NODE) if m.name == "늦깎이")
    assert late.linked is False
    # 로그인 훅이 이메일로 연결
    d.link_member_for_login(NODE, teammate)
    late = next(m for m in d.list_team_members(NODE) if m.name == "늦깎이")
    assert late.linked is True
    assert late.user_id == teammate


def test_deactivating_member_revokes_invite(company_admin: uuid.UUID) -> None:
    created = client.post(
        "/dashboard/team-members",
        json={"name": "퇴사예정", "email": "leaving@acme.example"},
        headers=_hdr(company_admin),
    ).json()
    assert _active_role("leaving@acme.example") == "member"
    # active=False로 수정 → 오프보딩 대칭: 초대 회수
    out = client.patch(
        f"/dashboard/team-members/{created['member_id']}",
        json={"name": "퇴사예정", "email": "leaving@acme.example", "active": False},
        headers=_hdr(company_admin),
    )
    assert out.status_code == 200, out.text
    assert out.json()["invited"] is False
    assert _active_role("leaving@acme.example") is None


def test_deleting_member_revokes_invite(company_admin: uuid.UUID) -> None:
    created = client.post(
        "/dashboard/team-members",
        json={"name": "삭제대상", "email": "gone@acme.example"},
        headers=_hdr(company_admin),
    ).json()
    assert _active_role("gone@acme.example") == "member"
    res = client.delete(
        f"/dashboard/team-members/{created['member_id']}", headers=_hdr(company_admin)
    )
    assert res.status_code == 204, res.text
    assert _active_role("gone@acme.example") is None


def test_changing_email_revokes_old_and_invites_new(company_admin: uuid.UUID) -> None:
    created = client.post(
        "/dashboard/team-members",
        json={"name": "주소변경", "email": "old@acme.example"},
        headers=_hdr(company_admin),
    ).json()
    assert _active_role("old@acme.example") == "member"
    out = client.patch(
        f"/dashboard/team-members/{created['member_id']}",
        json={"name": "주소변경", "email": "new@acme.example"},
        headers=_hdr(company_admin),
    )
    assert out.status_code == 200, out.text
    assert _active_role("old@acme.example") is None  # 옛 이메일 회수
    assert _active_role("new@acme.example") == "member"  # 새 이메일 초대


def test_clearing_email_clears_stale_user_link(company_admin: uuid.UUID) -> None:
    teammate = _add_logged_in_user("temp@acme.example")
    created = client.post(
        "/dashboard/team-members",
        json={"name": "임시연결", "email": "temp@acme.example"},
        headers=_hdr(company_admin),
    ).json()
    assert created["linked"] is True
    # 이메일 제거 → 옛 계정 링크가 남으면 안 된다(stale link 해제) + 초대 회수
    out = client.patch(
        f"/dashboard/team-members/{created['member_id']}",
        json={"name": "임시연결", "email": ""},
        headers=_hdr(company_admin),
    )
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["linked"] is False
    assert body["user_id"] is None
    assert body["invited"] is False
    assert _active_role("temp@acme.example") is None
    # sanity: 그 user는 여전히 존재(링크만 끊김)
    with session() as s:
        assert s.execute(
            select(users.c.user_id).where(users.c.user_id == teammate)
        ).first() is not None


def test_shared_email_not_revoked_while_another_active_member_has_it(
    company_admin: uuid.UUID,
) -> None:
    a = client.post(
        "/dashboard/team-members",
        json={"name": "공유A", "email": "shared@acme.example"},
        headers=_hdr(company_admin),
    ).json()
    client.post(
        "/dashboard/team-members",
        json={"name": "공유B", "email": "shared@acme.example"},
        headers=_hdr(company_admin),
    )
    # A 삭제해도 B가 같은 이메일을 보유 → 초대 유지
    client.delete(f"/dashboard/team-members/{a['member_id']}", headers=_hdr(company_admin))
    assert _active_role("shared@acme.example") == "member"


def test_create_is_best_effort_even_if_invite_actor_is_bogus(
    company_admin: uuid.UUID,
) -> None:
    """created_by FK가 위반돼도(존재하지 않는 actor) SAVEPOINT 격리로 멤버 생성은 성공."""
    bogus = uuid.uuid4()  # users에 없는 id → auth_allowlist.created_by FK 위반 유발
    member = d.create_team_member(
        NODE,
        d.TeamMemberIn(name="베스트에포트", email="be@acme.example"),
        created_by=bogus,
    )
    # 멤버는 생성됨
    with session() as s:
        assert s.execute(
            select(team_members.c.member_id).where(
                team_members.c.member_id == member.member_id
            )
        ).first() is not None
    # 초대는 FK 실패로 건너뜀(접근 권한 누설 없음)
    assert _active_role("be@acme.example") is None


def test_invite_is_insert_only_and_does_not_downgrade(company_admin: uuid.UUID) -> None:
    # 이미 admin으로 allowlist된 이메일
    with session() as s:
        s.execute(
            insert(auth_allowlist).values(
                allowlist_id=uuid.uuid4(),
                node_id=NODE,
                email="boss@acme.example",
                role="admin",
                created_by=None,
                revoked_at=None,
            )
        )
        s.commit()
    res = client.post(
        "/dashboard/team-members",
        json={"name": "보스", "email": "boss@acme.example"},
        headers=_hdr(company_admin),
    )
    assert res.status_code == 201, res.text
    # 초대는 insert-only — 기존 admin을 member로 강등하지 않는다
    assert _active_role("boss@acme.example") == "admin"
