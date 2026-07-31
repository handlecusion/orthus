"""개인 메모(personal notes) — owner-scope + 팀 공유 + 얼라인 확인 + 프로젝트 링크.

docs/personal-notes.md 계약:
- 메모 생성자가 owner. scope=mine은 내 소유 + 레거시 공용(owner NULL)만 보인다.
- owner가 팀원에게 view/edit로 공유하면 GET /notes/shared에 뜨고, view는 수정 403.
- 수신자가 acknowledge하면 owner의 공유 목록에 acknowledged_at이 찍힌다.
- 공유되지 않은 유저는 메모 GET이 404.
- 메모↔프로젝트 링크는 양방향(메모의 projects, 프로젝트의 notes)으로 보인다.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from orthus.api.main import app
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import dashboard_projects, note_shares, team_members, users

client = TestClient(app)


def H(user_id: uuid.UUID) -> dict[str, str]:
    return {"X-User-Id": str(user_id)}


def _mkuser(name: str) -> uuid.UUID:
    uid = uuid.uuid4()
    with session() as s:
        s.execute(users.insert().values(user_id=uid, display_name=name))
        s.execute(
            team_members.insert().values(
                member_id=uuid.uuid4(),
                node_id="company",
                user_id=uid,
                name=name,
                color="blue",
                active=True,
            )
        )
        s.commit()
    return uid


def _mkproject(name: str) -> uuid.UUID:
    pid = uuid.uuid4()
    with session() as s:
        s.execute(dashboard_projects.insert().values(project_id=pid, node_id="company", name=name))
        s.commit()
    return pid


@pytest.fixture
def company_user(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")
    # user_id fixture가 만든 owner에 팀원 행을 붙여 member_identity가 이름을 준다.
    with session() as s:
        s.execute(
            team_members.insert().values(
                member_id=uuid.uuid4(),
                node_id="company",
                user_id=user_id,
                name="박기획",
                color="teal",
                active=True,
            )
        )
        s.commit()
    return user_id


def _create_memo(uid: uuid.UUID, title: str = "내 메모") -> dict:
    r = client.post("/dashboard/pages", json={"title": title, "kind": "memo"}, headers=H(uid))
    assert r.status_code == 201, r.text
    return r.json()


def test_memo_owner_scope(company_user: uuid.UUID) -> None:
    owner = company_user
    other = _mkuser("남")
    memo = _create_memo(owner)
    assert memo["owner_id"] == str(owner)

    mine = client.get("/dashboard/pages?kind=memo&scope=mine", headers=H(owner)).json()
    assert any(m["page_id"] == memo["page_id"] for m in mine)

    # 다른 유저의 scope=mine에는 owner의 메모가 안 보인다.
    other_mine = client.get("/dashboard/pages?kind=memo&scope=mine", headers=H(other)).json()
    assert all(m["page_id"] != memo["page_id"] for m in other_mine)

    # 공유 안 된 메모는 GET 404.
    assert client.get(f"/dashboard/pages/{memo['page_id']}", headers=H(other)).status_code == 404


def test_client_generated_page_id_is_idempotent(company_user: uuid.UUID) -> None:
    owner = company_user
    page_id = uuid.uuid4()
    payload = {
        "page_id": str(page_id),
        "title": "오프라인에서 쓴 메모",
        "body": '[{"type":"paragraph","content":[]}]',
        "kind": "memo",
    }

    first = client.post("/dashboard/pages", json=payload, headers=H(owner))
    replay = client.post("/dashboard/pages", json=payload, headers=H(owner))

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert first.json() == replay.json()
    assert first.json()["page_id"] == str(page_id)

    mine = client.get("/dashboard/pages?kind=memo&scope=mine", headers=H(owner)).json()
    assert [row["page_id"] for row in mine].count(str(page_id)) == 1


def test_client_generated_page_retry_advances_same_owner_snapshot(
    company_user: uuid.UUID,
) -> None:
    page_id = uuid.uuid4()
    first = client.post(
        "/dashboard/pages",
        json={
            "page_id": str(page_id),
            "title": "오프라인 초안",
            "body": "첫 본문",
            "kind": "memo",
        },
        headers=H(company_user),
    )
    assert first.status_code == 201, first.text

    replay = client.post(
        "/dashboard/pages",
        json={
            "page_id": str(page_id),
            "title": "재연결 전 최신 제목",
            "body": "재연결 전 최신 본문",
            "kind": "memo",
        },
        headers=H(company_user),
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["title"] == "재연결 전 최신 제목"
    assert replay.json()["body"] == "재연결 전 최신 본문"


def test_client_generated_page_id_rejects_cross_kind_reuse(
    company_user: uuid.UUID,
) -> None:
    page_id = uuid.uuid4()
    assert (
        client.post(
            "/dashboard/pages",
            json={"page_id": str(page_id), "title": "메모", "kind": "memo"},
            headers=H(company_user),
        ).status_code
        == 201
    )
    collision = client.post(
        "/dashboard/pages",
        json={"page_id": str(page_id), "title": "하위 페이지", "kind": "page"},
        headers=H(company_user),
    )
    assert collision.status_code == 409


def test_memo_list_includes_capped_plain_text_preview(company_user: uuid.UUID) -> None:
    body = json.dumps(
        [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "사이드바에서 바로 보여야 하는 본문 요약",
                        "styles": {},
                    }
                ],
            }
        ],
        ensure_ascii=False,
    )
    created = client.post(
        "/dashboard/pages",
        json={"title": "미리보기 메모", "body": body, "kind": "memo"},
        headers=H(company_user),
    )
    assert created.status_code == 201, created.text

    rows = client.get(
        "/dashboard/pages?kind=memo&scope=mine&include_preview=true",
        headers=H(company_user),
    ).json()
    row = next(item for item in rows if item["page_id"] == created.json()["page_id"])
    assert row["preview"] == "사이드바에서 바로 보여야 하는 본문 요약"


def test_scope_all_never_exposes_private_memo_preview(company_user: uuid.UUID) -> None:
    other = _mkuser("미리보기 비소유자")
    created = client.post(
        "/dashboard/pages",
        json={"title": "비공개", "body": "외부에 보이면 안 되는 본문", "kind": "memo"},
        headers=H(company_user),
    )
    assert created.status_code == 201, created.text

    rows = client.get(
        "/dashboard/pages?kind=memo&scope=all&include_preview=true", headers=H(other)
    ).json()
    row = next(item for item in rows if item["page_id"] == created.json()["page_id"])
    assert row["preview"] is None


def test_deeply_nested_body_cannot_break_memo_list(company_user: uuid.UUID) -> None:
    hostile = "[" * 1000 + '"x"' + "]" * 1000
    created = client.post(
        "/dashboard/pages",
        json={"title": "깊은 JSON", "body": hostile, "kind": "memo"},
        headers=H(company_user),
    )
    assert created.status_code == 201, created.text

    response = client.get(
        "/dashboard/pages?kind=memo&scope=mine&include_preview=true",
        headers=H(company_user),
    )
    assert response.status_code == 200, response.text
    row = next(item for item in response.json() if item["page_id"] == created.json()["page_id"])
    assert row["preview"] is None


def test_client_generated_page_id_rejects_other_owner_collision(
    company_user: uuid.UUID,
) -> None:
    owner = company_user
    other = _mkuser("다른 소유자")
    page_id = uuid.uuid4()
    created = client.post(
        "/dashboard/pages",
        json={"page_id": str(page_id), "title": "소유자 메모", "kind": "memo"},
        headers=H(owner),
    )
    assert created.status_code == 201, created.text

    collision = client.post(
        "/dashboard/pages",
        json={"page_id": str(page_id), "title": "탈취 시도", "kind": "memo"},
        headers=H(other),
    )
    assert collision.status_code == 409
    assert collision.json() == {"detail": "page_id already exists"}
    assert client.get(f"/dashboard/pages/{page_id}", headers=H(other)).status_code == 404


def test_client_generated_page_id_rejects_other_node_collision(
    company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = company_user
    page_id = uuid.uuid4()
    created = client.post(
        "/dashboard/pages",
        json={"page_id": str(page_id), "title": "company A", "kind": "memo"},
        headers=H(owner),
    )
    assert created.status_code == 201, created.text

    settings = get_settings()
    monkeypatch.setattr(settings, "node_id", "other-company")
    collision = client.post(
        "/dashboard/pages",
        json={"page_id": str(page_id), "title": "company B", "kind": "memo"},
        headers=H(owner),
    )
    assert collision.status_code == 409
    assert collision.json() == {"detail": "page_id already exists"}


def test_share_view_then_edit_and_ack(company_user: uuid.UUID) -> None:
    owner = company_user
    mate = _mkuser("팀원")
    memo = _create_memo(owner, "공유 메모")
    pid = memo["page_id"]

    # view로 공유
    r = client.post(
        f"/dashboard/notes/{pid}/shares",
        json={"shared_with": [str(mate)], "permission": "view"},
        headers=H(owner),
    )
    assert r.status_code == 201, r.text
    shares = r.json()
    assert len(shares) == 1 and shares[0]["permission"] == "view"
    assert shares[0]["shared_with_name"] == "팀원"

    # 수신자의 shared 목록에 뜬다
    shared = client.get("/dashboard/notes/shared", headers=H(mate)).json()
    assert any(s["page_id"] == pid for s in shared)
    got = next(s for s in shared if s["page_id"] == pid)
    assert got["permission"] == "view"
    assert got["owner_name"] == "박기획"
    assert got["acknowledged_at"] is None

    # view는 열람 가능, 수정은 403
    assert client.get(f"/dashboard/pages/{pid}", headers=H(mate)).status_code == 200
    patch_view = client.patch(f"/dashboard/pages/{pid}", json={"body": "hacked"}, headers=H(mate))
    assert patch_view.status_code == 403

    # edit으로 승격하면 수정 가능
    client.post(
        f"/dashboard/notes/{pid}/shares",
        json={"shared_with": [str(mate)], "permission": "edit"},
        headers=H(owner),
    )
    patch_edit = client.patch(f"/dashboard/pages/{pid}", json={"body": "ok"}, headers=H(mate))
    assert patch_edit.status_code == 200

    # 수신자가 얼라인 확인
    ack = client.post(f"/dashboard/notes/{pid}/acknowledge", headers=H(mate))
    assert ack.status_code == 204
    shares2 = client.get(f"/dashboard/notes/{pid}/shares", headers=H(owner)).json()
    assert shares2[0]["acknowledged_at"] is not None

    # 프로젝트 링크도 하나 걸어 둔다(삭제 시 정리 확인용)
    proj = _mkproject("정리대상")
    client.put(
        f"/dashboard/notes/{pid}/projects",
        json={"project_ids": [str(proj)]},
        headers=H(owner),
    )

    # edit 수신자라도 삭제는 소유자만 → owner는 되고, 여기선 소유자 삭제 확인
    assert client.delete(f"/dashboard/pages/{pid}", headers=H(owner)).status_code == 204

    # 삭제 시 딸린 공유행/프로젝트 링크가 함께 제거된다(고아 방지)
    from orthus import notes as nt

    assert nt.list_shares("company", uuid.UUID(pid)) == []
    assert nt.list_project_links("company", uuid.UUID(pid)) == []


def test_only_owner_can_share(company_user: uuid.UUID) -> None:
    owner = company_user
    mate = _mkuser("팀원2")
    stranger = _mkuser("외부인")
    memo = _create_memo(owner)
    pid = memo["page_id"]
    # 공유 안 받은 유저는 공유 시도 403 (note_access=none → not writable)
    r = client.post(
        f"/dashboard/notes/{pid}/shares",
        json={"shared_with": [str(mate)], "permission": "view"},
        headers=H(stranger),
    )
    assert r.status_code == 403


def test_project_link_bidirectional(company_user: uuid.UUID) -> None:
    owner = company_user
    memo = _create_memo(owner, "프로젝트 메모")
    pid = memo["page_id"]
    proj = _mkproject("아틀라스")
    other_proj = _mkproject("노바티")

    # 링크 설정 (존재하는 프로젝트만 반영)
    fake = str(uuid.uuid4())
    r = client.put(
        f"/dashboard/notes/{pid}/projects",
        json={"project_ids": [str(proj), fake]},
        headers=H(owner),
    )
    assert r.status_code == 200, r.text
    links = r.json()
    assert [link["project_id"] for link in links] == [str(proj)]
    assert links[0]["name"] == "아틀라스"

    # 프로젝트 상세의 '관련 메모'에 뜬다
    proj_notes = client.get(f"/dashboard/projects/{proj}/notes", headers=H(owner)).json()
    assert any(n["page_id"] == pid for n in proj_notes)
    # 링크 안 한 프로젝트에는 안 뜬다
    empty = client.get(f"/dashboard/projects/{other_proj}/notes", headers=H(owner)).json()
    assert empty == []

    # 링크 교체(비우기)
    r2 = client.put(f"/dashboard/notes/{pid}/projects", json={"project_ids": []}, headers=H(owner))
    assert r2.json() == []


def test_note_library_bulk_projects_preview_and_share_state(company_user: uuid.UUID) -> None:
    owner = company_user
    mate = _mkuser("라이브러리 팀원")
    stranger = _mkuser("라이브러리 외부인")
    atlas = _mkproject("아틀라스")
    nova = _mkproject("노바티")
    body = json.dumps(
        [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "프로젝트별로 찾을 본문", "styles": {}}],
            }
        ],
        ensure_ascii=False,
    )
    linked = client.post(
        "/dashboard/pages",
        json={"title": "두 프로젝트 메모", "body": body, "kind": "memo"},
        headers=H(owner),
    ).json()
    unassigned = _create_memo(owner, "분류 안 된 메모")
    client.put(
        f"/dashboard/notes/{linked['page_id']}/projects",
        json={"project_ids": [str(atlas), str(nova)]},
        headers=H(owner),
    )
    client.post(
        f"/dashboard/notes/{linked['page_id']}/shares",
        json={"shared_with": [str(mate)], "permission": "view"},
        headers=H(owner),
    )

    mine_response = client.get("/dashboard/notes/library?scope=mine", headers=H(owner))
    assert mine_response.status_code == 200, mine_response.text
    mine = mine_response.json()
    linked_row = next(row for row in mine if row["page_id"] == linked["page_id"])
    assert linked_row["preview"] == "프로젝트별로 찾을 본문"
    assert {project["project_id"] for project in linked_row["projects"]} == {
        str(atlas),
        str(nova),
    }
    assert linked_row["access"] == "owner"
    assert linked_row["share_count"] == 1
    assert linked_row["acknowledged_count"] == 0
    assert next(row for row in mine if row["page_id"] == unassigned["page_id"])["projects"] == []

    shared_response = client.get("/dashboard/notes/library?scope=shared", headers=H(mate))
    assert shared_response.status_code == 200, shared_response.text
    shared_row = next(row for row in shared_response.json() if row["page_id"] == linked["page_id"])
    assert shared_row["owner_name"] == "박기획"
    assert shared_row["access"] == "view"
    assert shared_row["permission"] == "view"
    assert shared_row["preview"] == "프로젝트별로 찾을 본문"
    assert {project["project_id"] for project in shared_row["projects"]} == {
        str(atlas),
        str(nova),
    }

    permission_response = client.post(
        f"/dashboard/notes/{linked['page_id']}/shares",
        json={"shared_with": [str(mate)], "permission": "edit"},
        headers=H(owner),
    )
    assert permission_response.status_code == 201, permission_response.text
    shared_edit = client.get("/dashboard/notes/library?scope=shared", headers=H(mate)).json()
    shared_edit_row = next(row for row in shared_edit if row["page_id"] == linked["page_id"])
    assert shared_edit_row["access"] == "edit"
    assert shared_edit_row["permission"] == "edit"

    acknowledged = client.post(
        f"/dashboard/notes/{linked['page_id']}/acknowledge",
        headers=H(mate),
    )
    assert acknowledged.status_code == 204, acknowledged.text
    mine_after_ack = client.get("/dashboard/notes/library?scope=mine", headers=H(owner)).json()
    assert (
        next(row for row in mine_after_ack if row["page_id"] == linked["page_id"])[
            "acknowledged_count"
        ]
        == 1
    )
    shared_after_ack = client.get("/dashboard/notes/library?scope=shared", headers=H(mate)).json()
    assert (
        next(row for row in shared_after_ack if row["page_id"] == linked["page_id"])[
            "acknowledged_at"
        ]
        is not None
    )

    # The legacy schema has no permission CHECK. A malformed direct row must
    # fail closed to view instead of making the entire shared library return 500.
    with session() as s:
        s.execute(
            note_shares.update()
            .where(note_shares.c.page_id == uuid.UUID(linked["page_id"]))
            .values(permission="unexpected")
        )
        s.commit()
    malformed = client.get("/dashboard/notes/library?scope=shared", headers=H(mate))
    assert malformed.status_code == 200, malformed.text
    malformed_row = next(row for row in malformed.json() if row["page_id"] == linked["page_id"])
    assert malformed_row["access"] == "view"
    assert malformed_row["permission"] == "view"

    assert client.get("/dashboard/notes/library?scope=mine", headers=H(stranger)).json() == []
    assert client.get("/dashboard/notes/library?scope=shared", headers=H(stranger)).json() == []
    assert client.get("/dashboard/notes/library?scope=unknown", headers=H(owner)).status_code == 422
    assert client.get("/dashboard/pages?scope=unknown", headers=H(owner)).status_code == 422


def test_project_note_reverse_lookup_respects_note_visibility(company_user: uuid.UUID) -> None:
    owner = company_user
    mate = _mkuser("프로젝트 조회 팀원")
    project = _mkproject("비공개 경계")
    private_note = _create_memo(owner, "소유자만 보는 메모")
    own_note = _create_memo(mate, "팀원 자신의 메모")
    for note, actor in ((private_note, owner), (own_note, mate)):
        response = client.put(
            f"/dashboard/notes/{note['page_id']}/projects",
            json={"project_ids": [str(project)]},
            headers=H(actor),
        )
        assert response.status_code == 200, response.text

    before_share = client.get(f"/dashboard/projects/{project}/notes", headers=H(mate))
    assert before_share.status_code == 200, before_share.text
    assert {row["page_id"] for row in before_share.json()} == {own_note["page_id"]}

    client.post(
        f"/dashboard/notes/{private_note['page_id']}/shares",
        json={"shared_with": [str(mate)], "permission": "view"},
        headers=H(owner),
    )
    after_share = client.get(f"/dashboard/projects/{project}/notes", headers=H(mate))
    assert {row["page_id"] for row in after_share.json()} == {
        own_note["page_id"],
        private_note["page_id"],
    }


def test_legacy_null_owner_memo_visible_and_writable(company_user: uuid.UUID) -> None:
    """owner_id NULL(레거시 회사 공용) 메모는 누구나 열람/수정 가능하고 mine에도 뜬다."""
    owner = company_user
    other = _mkuser("동료")
    # 레거시 메모를 직접 삽입(owner_id NULL)
    from orthus import dashboard as d

    legacy = d.create_page("company", d.PageIn(title="레거시", kind="memo"), owner_id=None)
    lid = str(legacy.page_id)

    # 다른 유저도 열람/수정 가능
    assert client.get(f"/dashboard/pages/{lid}", headers=H(other)).status_code == 200
    assert (
        client.patch(
            f"/dashboard/pages/{lid}", json={"title": "고침"}, headers=H(other)
        ).status_code
        == 200
    )
    # mine에도 노출(공용 보존)
    mine = client.get("/dashboard/pages?kind=memo&scope=mine", headers=H(owner)).json()
    assert any(m["page_id"] == lid for m in mine)
    library = client.get("/dashboard/notes/library?scope=mine", headers=H(other)).json()
    legacy_row = next(row for row in library if row["page_id"] == lid)
    assert legacy_row["access"] == "edit"
    assert legacy_row["owner_id"] is None
