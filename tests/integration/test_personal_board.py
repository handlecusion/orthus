"""Personal workspace board API tests."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from orthus.api.main import app
from orthus.schemas.canonical import InternalDocument
from orthus.settings import get_settings

client = TestClient(app)


@pytest.fixture
def personal_user(user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "personal")
    monkeypatch.setattr(settings, "node_id", "personal-a")
    return user_id


@pytest.fixture
def wiki_docs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, InternalDocument, str]]:
    calls: list[tuple[uuid.UUID, InternalDocument, str]] = []

    def fake_upsert(user_id: uuid.UUID, doc: InternalDocument, *, scope: str, **_kwargs):
        calls.append((user_id, doc, scope))
        return uuid.uuid4(), True

    monkeypatch.setattr("orthus.personal_board.upsert_source_document", fake_upsert)
    return calls


def test_bootstrap_seeds_personal_workspace(personal_user: uuid.UUID) -> None:
    r = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["node_id"] == "personal-a"
    assert body["workspace_name"] == "개인 워크스페이스"
    assert body["selected_date"] == "2026-06-04"
    assert [day["date"] for day in body["days"]] == [
        "2026-06-04",
        "2026-06-05",
        "2026-06-06",
    ]
    assert [bucket["key"] for bucket in body["backlog_buckets"]] == [
        "next_week",
        "next_month",
        "next_quarter",
        "next_year",
        "someday",
        "never",
    ]
    assert [bucket["badge"] for bucket in body["backlog_buckets"]] == ["W", "M", "Q", "Y", "S", "N"]
    assert body["folders"][0]["name"] == "Backlog"
    assert body["folders"][0]["kind"] == "system"
    assert [item["kind"] for item in body["integrations"]] == [
        "calendar",
        "github",
        "gmail",
        "notion",
        "target",
        "backlog",
        "reflection",
        "search",
        "automation",
    ]


def test_personal_board_is_not_exposed_on_company_node(
    user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")

    r = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(user_id)},
    )

    assert r.status_code == 404


def test_create_note_updates_personal_board_wiki_doc(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    r = client.post(
        "/personal-board/notes",
        json={
            "note_date": "2026-06-04",
            "kind": "incident",
            "title": "세금 자료 누락",
            "body": "홈택스에서 내려받을 파일이 하나 빠졌다.",
        },
        headers={"X-User-Id": str(personal_user)},
    )

    assert r.status_code == 201
    assert r.json()["title"] == "세금 자료 누락"
    assert len(wiki_docs) == 1
    called_user_id, doc, scope = wiki_docs[0]
    assert called_user_id == personal_user
    assert scope == "personal"
    assert doc.source == "personal_board"
    assert doc.source_canonical_id == f"personal_board:daily:personal-a:{personal_user}:2026-06-04"
    assert doc.source_external_id == doc.source_canonical_id
    assert "## 특이사항" in doc.markdown
    assert "세금 자료 누락" in doc.markdown
    assert "홈택스에서 내려받을 파일" in doc.markdown


def test_wiki_authoring_failure_degrades_to_deferred_source_document(
    personal_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    def fake_upsert(
        _user_id: uuid.UUID,
        _doc: InternalDocument,
        *,
        scope: str,
        defer_authoring: bool = False,
        **_kwargs,
    ):
        assert scope == "personal"
        calls.append(defer_authoring)
        if not defer_authoring:
            raise RuntimeError("llm unavailable")
        return uuid.uuid4(), True

    monkeypatch.setattr("orthus.personal_board.upsert_source_document", fake_upsert)

    r = client.post(
        "/personal-board/notes",
        json={
            "note_date": "2026-06-04",
            "kind": "note",
            "title": "보험 자료",
            "body": "스캔본은 금요일까지 받기로 했다.",
        },
        headers={"X-User-Id": str(personal_user)},
    )

    assert r.status_code == 201
    assert calls == [False, True]


def test_create_task_and_complete_round_trip(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={
            "title": "병원 예약",
            "scheduled_date": "2026-06-04",
            "scheduled_time": "09:30",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    assert listed.json()["days"][0]["tasks"][0]["title"] == "병원 예약"

    patched = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"status": "done"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert patched.status_code == 200
    assert patched.json()["completed"] is True
    # 할 일은 live 보드(ticket_list)가 SoR — 위키 문서로 distill되지 않는다.
    assert "병원 예약" not in wiki_docs[-1][1].markdown
    assert wiki_docs[-1][1].wiki_authoring is False


def test_task_note_persists_across_bootstrap(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    """티켓 노트 유실 회귀(0093): note는 patch로 저장되고 bootstrap에 다시 온다."""
    headers = {"X-User-Id": str(personal_user)}
    created = client.post(
        "/personal-board/tasks",
        json={"title": "노트 유실 점검", "scheduled_date": "2026-06-04"},
        headers=headers,
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]
    assert created.json()["note"] is None

    patched = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"note": "회의에서 나온 메모\n둘째 줄"},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["note"] == "회의에서 나온 메모\n둘째 줄"

    # 새로고침/재시작에 해당하는 bootstrap 재조회에서도 남아 있어야 한다.
    listed = client.get("/personal-board/bootstrap?date=2026-06-04", headers=headers)
    assert listed.status_code == 200
    task = next(t for t in listed.json()["days"][0]["tasks"] if t["task_id"] == task_id)
    assert task["note"] == "회의에서 나온 메모\n둘째 줄"

    # 빈 문자열 = 노트 삭제(NULL), 너무 긴 노트는 422.
    cleared = client.patch(
        f"/personal-board/tasks/{task_id}", json={"note": "   "}, headers=headers
    )
    assert cleared.status_code == 200
    assert cleared.json()["note"] is None
    too_long = client.patch(
        f"/personal-board/tasks/{task_id}", json={"note": "x" * 20_001}, headers=headers
    )
    assert too_long.status_code == 422


def test_task_comments_persist_and_are_owner_scoped(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    """티켓 댓글 유실 회귀(0093): POST 후 GET으로 다시 조회된다."""
    headers = {"X-User-Id": str(personal_user)}
    created = client.post(
        "/personal-board/tasks",
        json={"title": "댓글 점검", "scheduled_date": "2026-06-04"},
        headers=headers,
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    posted = client.post(
        f"/personal-board/tasks/{task_id}/comments",
        json={"body": "첫 댓글"},
        headers=headers,
    )
    assert posted.status_code == 201
    assert posted.json()["body"] == "첫 댓글"

    listed = client.get(f"/personal-board/tasks/{task_id}/comments", headers=headers)
    assert listed.status_code == 200
    assert [c["body"] for c in listed.json()] == ["첫 댓글"]

    # 빈 본문은 422, 없는 task는 404.
    empty = client.post(
        f"/personal-board/tasks/{task_id}/comments", json={"body": "  "}, headers=headers
    )
    assert empty.status_code == 422
    missing = client.get(f"/personal-board/tasks/{uuid.uuid4()}/comments", headers=headers)
    assert missing.status_code == 404

    # 실제 타 유저(B)가 A의 task_id로 읽기/쓰기 시도 → 404 (owner 격리).
    import sqlalchemy as sa

    from orthus.db import session as db_session
    from orthus.tables import users as users_table

    other_uid = uuid.uuid4()
    with db_session() as s:
        s.execute(sa.insert(users_table).values(user_id=other_uid, display_name="Other User"))
        s.commit()
    other_headers = {"X-User-Id": str(other_uid)}
    cross_read = client.get(f"/personal-board/tasks/{task_id}/comments", headers=other_headers)
    assert cross_read.status_code == 404
    cross_write = client.post(
        f"/personal-board/tasks/{task_id}/comments", json={"body": "침입"}, headers=other_headers
    )
    assert cross_write.status_code == 404


def test_create_task_with_client_id_is_idempotent(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    """Offline-first: replaying a create with the same client id must not dupe."""
    client_id = str(uuid.uuid4())
    headers = {"X-User-Id": str(personal_user)}
    body = {
        "title": "오프라인 태스크",
        "scheduled_date": "2026-06-05",
        "task_id": client_id,
    }
    first = client.post("/personal-board/tasks", json=body, headers=headers)
    assert first.status_code == 201
    assert first.json()["task_id"] == client_id

    # Replay the exact same create (as the outbox would on reconnect).
    second = client.post("/personal-board/tasks", json=body, headers=headers)
    assert second.status_code == 201
    assert second.json()["task_id"] == client_id

    listed = client.get("/personal-board/bootstrap?date=2026-06-05", headers=headers)
    tasks = listed.json()["days"][0]["tasks"]
    matching = [t for t in tasks if t["task_id"] == client_id]
    assert len(matching) == 1, "client-id create must be idempotent (no duplicate)"


def test_create_subtask_with_client_id_is_idempotent(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    headers = {"X-User-Id": str(personal_user)}
    task = client.post(
        "/personal-board/tasks",
        json={"title": "부모 태스크", "scheduled_date": "2026-06-06"},
        headers=headers,
    )
    task_id = task.json()["task_id"]
    sub_id = str(uuid.uuid4())
    body = {"title": "오프라인 서브", "subtask_id": sub_id}
    first = client.post(f"/personal-board/tasks/{task_id}/subtasks", json=body, headers=headers)
    assert first.status_code == 201
    assert first.json()["subtask_id"] == sub_id
    second = client.post(f"/personal-board/tasks/{task_id}/subtasks", json=body, headers=headers)
    assert second.status_code == 201
    assert second.json()["subtask_id"] == sub_id

    listed = client.get("/personal-board/bootstrap?date=2026-06-06", headers=headers)
    day_task = next(t for t in listed.json()["days"][0]["tasks"] if t["task_id"] == task_id)
    matching = [s for s in day_task["subtasks"] if s["subtask_id"] == sub_id]
    assert len(matching) == 1, "client-id subtask create must be idempotent"


def test_create_task_uses_zero_based_order(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "첫 일정", "scheduled_date": "2026-06-04"},
        headers={"X-User-Id": str(personal_user)},
    )

    assert created.status_code == 201
    assert created.json()["order_index"] == 0


def test_invalid_create_enums_are_rejected_before_db_constraints(
    personal_user: uuid.UUID,
) -> None:
    invalid_task = client.post(
        "/personal-board/tasks",
        json={
            "title": "잘못된 우선순위",
            "scheduled_date": "2026-06-04",
            "priority": "impossible",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert invalid_task.status_code == 422
    assert invalid_task.json()["detail"] == "invalid priority"

    invalid_event = client.post(
        "/personal-board/fixed-events",
        json={
            "title": "잘못된 소스",
            "starts_at": "2026-06-04T10:00:00+09:00",
            "ends_at": "2026-06-04T10:30:00+09:00",
            "source_kind": "impossible",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert invalid_event.status_code == 422
    assert invalid_event.json()["detail"] == "invalid source_kind"

    invalid_note = client.post(
        "/personal-board/notes",
        json={
            "note_date": "2026-06-04",
            "kind": "impossible",
            "title": "잘못된 종류",
            "body": "DB constraint 전 API에서 거부되어야 한다.",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert invalid_note.status_code == 422
    assert invalid_note.json()["detail"] == "invalid kind"


def test_project_channel_can_be_created_and_assigned(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    project = client.post(
        "/personal-board/projects",
        json={"name": "세무", "color": "#5d9bd8"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert project.status_code == 201
    project_id = project.json()["project_id"]

    created = client.post(
        "/personal-board/tasks",
        json={"title": "증빙 정리", "scheduled_date": "2026-06-04", "project_id": project_id},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    assert created.json()["project"]["name"] == "세무"

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    task = listed.json()["days"][0]["tasks"][0]
    assert task["project"]["project_id"] == project_id
    # 채널 배정 할 일도 live 보드 데이터 — 위키 문서에 넣지 않는다.
    assert "증빙 정리" not in wiki_docs[-1][1].markdown
    assert wiki_docs[-1][1].wiki_authoring is False

    cleared = client.patch(
        f"/personal-board/tasks/{created.json()['task_id']}",
        json={"project_id": None},
        headers={"X-User-Id": str(personal_user)},
    )
    assert cleared.status_code == 200
    assert cleared.json()["project"] is None


def test_project_channel_can_be_deleted_and_unassigns_tasks(
    personal_user: uuid.UUID,
) -> None:
    project = client.post(
        "/personal-board/projects",
        json={"name": "올리브영", "color": "#5d9bd8"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert project.status_code == 201
    project_id = project.json()["project_id"]

    task = client.post(
        "/personal-board/tasks",
        json={"title": "발주", "scheduled_date": "2026-06-04", "project_id": project_id},
        headers={"X-User-Id": str(personal_user)},
    )
    assert task.status_code == 201
    task_id = task.json()["task_id"]

    deleted = client.delete(
        f"/personal-board/projects/{project_id}",
        headers={"X-User-Id": str(personal_user)},
    )
    assert deleted.status_code == 204

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    body = listed.json()
    assert all(p["project_id"] != project_id for p in body["projects"])
    surviving = next(t for t in body["days"][0]["tasks"] if t["task_id"] == task_id)
    assert surviving["project"] is None

    # Deleting an already-archived (or unknown) channel is a 404.
    again = client.delete(
        f"/personal-board/projects/{project_id}",
        headers={"X-User-Id": str(personal_user)},
    )
    assert again.status_code == 404


def test_folder_create_matches_ojosama_sidebar_contract(personal_user: uuid.UUID) -> None:
    created = client.post(
        "/personal-board/folders",
        json={"name": "개인 리서치"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    assert created.json()["name"] == "개인 리서치"
    assert created.json()["kind"] == "custom"

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    folders = listed.json()["folders"]
    assert [folder["name"] for folder in folders] == ["Backlog", "개인 리서치"]
    assert folders[1]["order_index"] == 1


def test_reserved_folder_name_cannot_mutate_system_folder(personal_user: uuid.UUID) -> None:
    rejected = client.post(
        "/personal-board/folders",
        json={"name": "backlog"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "reserved folder name"

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    folders = listed.json()["folders"]
    assert folders == [
        {
            "folder_id": folders[0]["folder_id"],
            "name": "Backlog",
            "kind": "system",
            "order_index": 0,
        }
    ]


def test_preferences_and_bucket_collapse_persist(
    personal_user: uuid.UUID,
) -> None:
    boot = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert boot.status_code == 200
    bucket_id = boot.json()["backlog_buckets"][0]["bucket_id"]

    pref = client.patch(
        "/personal-board/preferences",
        json={
            "filter_mode": "done",
            "sort_mode": "priority",
            "right_panel": "integrations",
            "active_integration": "github",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert pref.status_code == 200
    assert pref.json()["filter_mode"] == "done"
    assert pref.json()["sort_mode"] == "priority"
    assert pref.json()["right_panel"] == "integrations"
    assert pref.json()["active_integration"] == "github"

    bucket = client.patch(
        f"/personal-board/backlog-buckets/{bucket_id}",
        json={"collapsed": True},
        headers={"X-User-Id": str(personal_user)},
    )
    assert bucket.status_code == 200
    assert bucket.json()["collapsed"] is True

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    assert listed.json()["preferences"]["filter_mode"] == "done"
    assert listed.json()["preferences"]["sort_mode"] == "priority"
    assert listed.json()["preferences"]["right_panel"] == "integrations"
    assert listed.json()["preferences"]["active_integration"] == "github"
    assert listed.json()["backlog_buckets"][0]["collapsed"] is True


def test_move_task_between_day_and_backlog(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    bucket_id = listed.json()["backlog_buckets"][0]["bucket_id"]

    created = client.post(
        "/personal-board/tasks",
        json={"title": "영수증 모으기", "scheduled_date": "2026-06-04"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    moved_to_backlog = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"scheduled_date": None, "backlog_bucket_id": bucket_id},
        headers={"X-User-Id": str(personal_user)},
    )
    assert moved_to_backlog.status_code == 200
    assert moved_to_backlog.json()["scheduled_date"] is None
    assert moved_to_backlog.json()["backlog_bucket_id"] == bucket_id

    moved_to_day = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={
            "scheduled_date": "2026-06-05",
            "backlog_bucket_id": None,
            "order_index": None,
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert moved_to_day.status_code == 200
    assert moved_to_day.json()["scheduled_date"] == "2026-06-05"
    assert moved_to_day.json()["backlog_bucket_id"] is None
    # 백로그↔일자 이동한 할 일도 위키에 distill되지 않는다(live 보드 SoR).
    assert "영수증 모으기" not in wiki_docs[-1][1].markdown
    assert wiki_docs[-1][1].wiki_authoring is False


def test_reorder_task_rebalances_sibling_order_indexes(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    task_ids: list[str] = []
    for title in ["첫 번째", "두 번째", "세 번째"]:
        created = client.post(
            "/personal-board/tasks",
            json={"title": title, "scheduled_date": "2026-06-04"},
            headers={"X-User-Id": str(personal_user)},
        )
        assert created.status_code == 201
        task_ids.append(created.json()["task_id"])

    moved = client.patch(
        f"/personal-board/tasks/{task_ids[0]}",
        json={"order_index": 2},
        headers={"X-User-Id": str(personal_user)},
    )

    assert moved.status_code == 200
    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    tasks = listed.json()["days"][0]["tasks"]
    assert [task["title"] for task in tasks] == ["두 번째", "세 번째", "첫 번째"]
    assert [task["order_index"] for task in tasks] == [0, 1, 2]
    assert "첫 번째" not in wiki_docs[-1][1].markdown
    assert wiki_docs[-1][1].wiki_authoring is False


def test_create_and_toggle_subtask(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "계약서 확인", "scheduled_date": "2026-06-04"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    subtask = client.post(
        f"/personal-board/tasks/{task_id}/subtasks",
        json={"title": "첨부파일 열기"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert subtask.status_code == 201
    subtask_id = subtask.json()["subtask_id"]

    patched = client.patch(
        f"/personal-board/subtasks/{subtask_id}",
        json={"completed": True},
        headers={"X-User-Id": str(personal_user)},
    )
    assert patched.status_code == 200
    assert patched.json()["completed"] is True

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    subtasks = listed.json()["days"][0]["tasks"][0]["subtasks"]
    assert any(item["title"] == "첨부파일 열기" and item["completed"] for item in subtasks)
    # 할 일과 하위 할 일 모두 live 보드 데이터 — 위키 문서에 남지 않는다.
    assert "계약서 확인" not in wiki_docs[-1][1].markdown
    assert "첨부파일 열기" not in wiki_docs[-1][1].markdown
    assert wiki_docs[-1][1].wiki_authoring is False


def test_completing_task_cascades_subtasks_done(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    """본 태스크를 done 처리하면 미완료 서브태스크도 모두 완료 처리된다."""
    headers = {"X-User-Id": str(personal_user)}
    created = client.post(
        "/personal-board/tasks",
        json={"title": "런칭 준비", "scheduled_date": "2026-06-05"},
        headers=headers,
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    for title in ("디자인 QA", "카피 검수", "배포 점검"):
        sub = client.post(
            f"/personal-board/tasks/{task_id}/subtasks",
            json={"title": title},
            headers=headers,
        )
        assert sub.status_code == 201

    # 모든 서브태스크가 미완료인 상태에서 본 태스크를 완료한다.
    done = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"status": "done"},
        headers=headers,
    )
    assert done.status_code == 200
    assert done.json()["completed"] is True

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-05",
        headers=headers,
    )
    task = next(t for t in listed.json()["days"][0]["tasks"] if t["task_id"] == task_id)
    assert task["subtasks"], "서브태스크가 있어야 한다"
    assert all(item["completed"] for item in task["subtasks"])


def test_rename_and_delete_subtask(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "발표 준비", "scheduled_date": "2026-06-04"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    subtask = client.post(
        f"/personal-board/tasks/{task_id}/subtasks",
        json={"title": "슬라이드 초안"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert subtask.status_code == 201
    subtask_id = subtask.json()["subtask_id"]

    renamed = client.patch(
        f"/personal-board/subtasks/{subtask_id}",
        json={"title": "슬라이드 최종본"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "슬라이드 최종본"

    deleted = client.delete(
        f"/personal-board/subtasks/{subtask_id}",
        headers={"X-User-Id": str(personal_user)},
    )
    assert deleted.status_code == 204

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    subtasks = listed.json()["days"][0]["tasks"][0]["subtasks"]
    assert all(item["subtask_id"] != subtask_id for item in subtasks)

    missing = client.delete(
        f"/personal-board/subtasks/{subtask_id}",
        headers={"X-User-Id": str(personal_user)},
    )
    assert missing.status_code == 404


def test_create_fixed_event_appears_on_day(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/fixed-events",
        json={
            "title": "세무사 통화",
            "starts_at": "2026-06-04T10:00:00+09:00",
            "ends_at": "2026-06-04T10:30:00+09:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    event = listed.json()["days"][0]["fixed_events"][0]
    assert event["title"] == "세무사 통화"
    # 일정(캘린더)도 live 데이터 — personal_schedule_list가 SoR라 위키에 넣지 않는다.
    assert "세무사 통화" not in wiki_docs[-1][1].markdown
    assert wiki_docs[-1][1].wiki_authoring is False


def test_update_fixed_event_edits_inline_fields_and_resyncs_days(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/fixed-events",
        json={
            "title": "오전 통화",
            "starts_at": "2026-06-04T10:00:00+09:00",
            "ends_at": "2026-06-04T10:30:00+09:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    event_id = created.json()["event_id"]

    patched = client.patch(
        f"/personal-board/fixed-events/{event_id}",
        json={
            "title": "야간 통화",
            "starts_at": "2026-06-04T23:30:00+09:00",
            "ends_at": "2026-06-05T00:30:00+09:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "야간 통화"

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    days = listed.json()["days"]
    assert days[0]["fixed_events"][0]["title"] == "야간 통화"
    assert days[1]["fixed_events"][0]["title"] == "야간 통화"
    assert f"personal_board:daily:personal-a:{personal_user}:2026-06-04" in {
        doc.source_canonical_id for _, doc, _ in wiki_docs
    }
    assert f"personal_board:daily:personal-a:{personal_user}:2026-06-05" in {
        doc.source_canonical_id for _, doc, _ in wiki_docs
    }


def test_fixed_event_groups_by_workspace_timezone_at_day_boundary(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/fixed-events",
        json={
            "title": "자정 점검",
            "starts_at": "2026-06-04T00:30:00+09:00",
            "ends_at": "2026-06-04T01:00:00+09:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    assert listed.json()["days"][0]["fixed_events"][0]["title"] == "자정 점검"


def test_cross_midnight_fixed_event_appears_on_each_overlapped_day(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/fixed-events",
        json={
            "title": "야간 배포",
            "starts_at": "2026-06-04T23:30:00+09:00",
            "ends_at": "2026-06-05T01:00:00+09:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    days = listed.json()["days"]
    assert days[0]["fixed_events"][0]["title"] == "야간 배포"
    assert days[1]["fixed_events"][0]["title"] == "야간 배포"
    assert {doc.source_canonical_id for _, doc, _ in wiki_docs} == {
        f"personal_board:daily:personal-a:{personal_user}:2026-06-04",
        f"personal_board:daily:personal-a:{personal_user}:2026-06-05",
    }


def test_fixed_event_ending_at_midnight_does_not_occupy_next_day(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/fixed-events",
        json={
            "title": "자정 종료",
            "starts_at": "2026-06-04T23:00:00+09:00",
            "ends_at": "2026-06-05T00:00:00+09:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    days = listed.json()["days"]
    assert [event["title"] for event in days[0]["fixed_events"]] == ["자정 종료"]
    assert days[1]["fixed_events"] == []


def test_weekly_plan_objective_crud_round_trip(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    project = client.post(
        "/personal-board/projects",
        json={"name": "분기 목표", "color": "#5d9bd8"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert project.status_code == 201
    project_id = project.json()["project_id"]

    created = client.post(
        "/personal-board/weekly-plan/objectives",
        json={"week_start": "2026-06-15", "title": "투자 자료 정리", "project_id": project_id},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    objective = created.json()
    assert objective["title"] == "투자 자료 정리"
    assert objective["order_index"] == 0
    assert objective["project"]["name"] == "분기 목표"
    assert objective["day_allocations"] == []
    objective_id = objective["objective_id"]

    second = client.post(
        "/personal-board/weekly-plan/objectives",
        json={"week_start": "2026-06-15", "title": "두 번째 목표"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert second.status_code == 201
    assert second.json()["order_index"] == 1

    listed = client.get(
        "/personal-board/weekly-plan?week_start=2026-06-15",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    body = listed.json()
    assert body["week_start"] == "2026-06-15"
    assert [item["title"] for item in body["objectives"]] == ["투자 자료 정리", "두 번째 목표"]

    patched = client.patch(
        f"/personal-board/weekly-plan/objectives/{objective_id}",
        json={
            "completed": True,
            "project_id": None,
            "day_allocations": [
                {"weekday": 0, "minutes": 60},
                {"weekday": 4, "minutes": None},
            ],
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert patched.status_code == 200
    updated = patched.json()
    assert updated["completed"] is True
    assert updated["project"] is None
    assert updated["project_id"] is None
    assert updated["day_allocations"] == [
        {"weekday": 0, "minutes": 60, "order": None, "note": None},
        {"weekday": 4, "minutes": None, "order": None, "note": None},
    ]

    deleted = client.delete(
        f"/personal-board/weekly-plan/objectives/{objective_id}",
        headers={"X-User-Id": str(personal_user)},
    )
    assert deleted.status_code == 204

    relisted = client.get(
        "/personal-board/weekly-plan?week_start=2026-06-15",
        headers={"X-User-Id": str(personal_user)},
    )
    assert [item["title"] for item in relisted.json()["objectives"]] == ["두 번째 목표"]

    # weekly-plan endpoints must not trigger any wiki authoring/embedding
    assert wiki_docs == []


def test_objective_note_round_trips(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/weekly-plan/objectives",
        json={"week_start": "2026-06-15", "title": "노트 목표"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    objective = created.json()
    assert objective["note"] is None
    assert objective["subtasks"] == []
    assert objective["comments"] == []
    objective_id = objective["objective_id"]

    patched = client.patch(
        f"/personal-board/weekly-plan/objectives/{objective_id}",
        json={"note": "분기 목표 상세 메모"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert patched.status_code == 200
    assert patched.json()["note"] == "분기 목표 상세 메모"

    listed = client.get(
        "/personal-board/weekly-plan?week_start=2026-06-15",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    assert listed.json()["objectives"][0]["note"] == "분기 목표 상세 메모"
    assert wiki_docs == []


def test_objective_subtask_create_and_toggle(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/weekly-plan/objectives",
        json={"week_start": "2026-06-15", "title": "서브태스크 목표"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    objective_id = created.json()["objective_id"]

    subtask = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/subtasks",
        json={"title": "자료 수집", "weekday": 1},
        headers={"X-User-Id": str(personal_user)},
    )
    assert subtask.status_code == 201
    body = subtask.json()
    assert body["title"] == "자료 수집"
    assert body["completed"] is False
    assert body["order_index"] == 0
    assert body["weekday"] == 1
    subtask_id = body["subtask_id"]

    patched = client.patch(
        f"/personal-board/weekly-plan/objective-subtasks/{subtask_id}",
        json={"completed": True},
        headers={"X-User-Id": str(personal_user)},
    )
    assert patched.status_code == 200
    assert patched.json()["completed"] is True

    listed = client.get(
        "/personal-board/weekly-plan?week_start=2026-06-15",
        headers={"X-User-Id": str(personal_user)},
    )
    subtasks = listed.json()["objectives"][0]["subtasks"]
    assert [item["title"] for item in subtasks] == ["자료 수집"]
    assert subtasks[0]["completed"] is True
    assert subtasks[0]["weekday"] == 1
    assert wiki_docs == []


def test_objective_comment_create_populates_author(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/weekly-plan/objectives",
        json={"week_start": "2026-06-15", "title": "코멘트 목표"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    objective_id = created.json()["objective_id"]

    comment = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/comments",
        json={"body": "진행 상황 공유합니다", "weekday": 2},
        headers={"X-User-Id": str(personal_user)},
    )
    assert comment.status_code == 201
    body = comment.json()
    assert body["body"] == "진행 상황 공유합니다"
    assert body["user_id"] == str(personal_user)
    assert body["author_name"] == "Demo User"
    assert body["weekday"] == 2

    empty = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/comments",
        json={"body": "   ", "weekday": 2},
        headers={"X-User-Id": str(personal_user)},
    )
    assert empty.status_code == 422

    listed = client.get(
        "/personal-board/weekly-plan?week_start=2026-06-15",
        headers={"X-User-Id": str(personal_user)},
    )
    comments = listed.json()["objectives"][0]["comments"]
    assert [item["body"] for item in comments] == ["진행 상황 공유합니다"]
    assert comments[0]["author_name"] == "Demo User"
    assert comments[0]["weekday"] == 2
    assert wiki_docs == []


def test_objective_detail_is_per_day_independent(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/weekly-plan/objectives",
        json={"week_start": "2026-06-15", "title": "영화작업"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    objective_id = created.json()["objective_id"]

    # Subtasks on two different weekdays share an objective but stay distinct,
    # and order_index is scoped per weekday (each starts at 0).
    sub_tue = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/subtasks",
        json={"title": "화요일 컷 정리", "weekday": 1},
        headers={"X-User-Id": str(personal_user)},
    )
    assert sub_tue.status_code == 201
    assert sub_tue.json()["weekday"] == 1
    assert sub_tue.json()["order_index"] == 0

    sub_wed = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/subtasks",
        json={"title": "수요일 사운드", "weekday": 2},
        headers={"X-User-Id": str(personal_user)},
    )
    assert sub_wed.status_code == 201
    assert sub_wed.json()["weekday"] == 2
    assert sub_wed.json()["order_index"] == 0

    # A comment belongs to a single weekday.
    comment_tue = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/comments",
        json={"body": "화요일 메모", "weekday": 1},
        headers={"X-User-Id": str(personal_user)},
    )
    assert comment_tue.status_code == 201
    assert comment_tue.json()["weekday"] == 1

    # Per-day note lives in the matching day_allocations entry.
    patched = client.patch(
        f"/personal-board/weekly-plan/objectives/{objective_id}",
        json={
            "day_allocations": [
                {"weekday": 1, "minutes": 30, "note": "화요일 작업 노트"},
                {"weekday": 2, "minutes": 20},
            ]
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert patched.status_code == 200

    listed = client.get(
        "/personal-board/weekly-plan?week_start=2026-06-15",
        headers={"X-User-Id": str(personal_user)},
    )
    assert listed.status_code == 200
    objective = listed.json()["objectives"][0]

    subtasks_by_weekday = {item["weekday"]: item["title"] for item in objective["subtasks"]}
    assert subtasks_by_weekday == {1: "화요일 컷 정리", 2: "수요일 사운드"}

    assert [item["weekday"] for item in objective["comments"]] == [1]

    allocations_by_weekday = {alloc["weekday"]: alloc for alloc in objective["day_allocations"]}
    assert allocations_by_weekday[1]["note"] == "화요일 작업 노트"
    assert allocations_by_weekday[2]["note"] is None
    assert wiki_docs == []


def test_objective_move_day_clean_to_empty_target(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/weekly-plan/objectives",
        json={"week_start": "2026-06-15", "title": "이동 목표"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    objective_id = created.json()["objective_id"]

    patched = client.patch(
        f"/personal-board/weekly-plan/objectives/{objective_id}",
        json={"day_allocations": [{"weekday": 2, "minutes": 30}]},
        headers={"X-User-Id": str(personal_user)},
    )
    assert patched.status_code == 200

    sub = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/subtasks",
        json={"title": "수요일로 옮길 작업", "weekday": 2},
        headers={"X-User-Id": str(personal_user)},
    )
    assert sub.status_code == 201

    moved = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/move-day",
        json={"from_weekday": 2, "to_weekday": 3},
        headers={"X-User-Id": str(personal_user)},
    )
    assert moved.status_code == 200
    objective = moved.json()

    allocations_by_weekday = {alloc["weekday"]: alloc for alloc in objective["day_allocations"]}
    assert 2 not in allocations_by_weekday
    assert allocations_by_weekday[3]["minutes"] == 30

    assert [item["weekday"] for item in objective["subtasks"]] == [3]
    assert objective["subtasks"][0]["title"] == "수요일로 옮길 작업"
    assert wiki_docs == []


def test_objective_move_day_merges_into_target(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    created = client.post(
        "/personal-board/weekly-plan/objectives",
        json={"week_start": "2026-06-15", "title": "병합 목표"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    objective_id = created.json()["objective_id"]

    patched = client.patch(
        f"/personal-board/weekly-plan/objectives/{objective_id}",
        json={
            "day_allocations": [
                {"weekday": 2, "minutes": 30},
                {"weekday": 3, "minutes": 20},
            ]
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert patched.status_code == 200

    sub_tue = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/subtasks",
        json={"title": "화요일 작업", "weekday": 2},
        headers={"X-User-Id": str(personal_user)},
    )
    assert sub_tue.status_code == 201
    sub_wed = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/subtasks",
        json={"title": "수요일 작업", "weekday": 3},
        headers={"X-User-Id": str(personal_user)},
    )
    assert sub_wed.status_code == 201

    moved = client.post(
        f"/personal-board/weekly-plan/objectives/{objective_id}/move-day",
        json={"from_weekday": 2, "to_weekday": 3},
        headers={"X-User-Id": str(personal_user)},
    )
    assert moved.status_code == 200
    objective = moved.json()

    allocations_by_weekday = {alloc["weekday"]: alloc for alloc in objective["day_allocations"]}
    assert 2 not in allocations_by_weekday
    assert allocations_by_weekday[3]["minutes"] == 50

    wed_subtasks = [item for item in objective["subtasks"] if item["weekday"] == 3]
    assert len(wed_subtasks) == 2
    assert all(item["weekday"] == 3 for item in objective["subtasks"])
    assert len({item["order_index"] for item in wed_subtasks}) == 2
    assert wiki_docs == []


def test_weekly_plan_objective_endpoints_404_on_company_node(
    user_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "node_kind", "company")
    monkeypatch.setattr(settings, "node_id", "company")

    r = client.get(
        "/personal-board/weekly-plan",
        headers={"X-User-Id": str(user_id)},
    )
    assert r.status_code == 404


def test_update_objective_rejects_foreign_project(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/weekly-plan/objectives",
        json={"week_start": "2026-06-15", "title": "외부 프로젝트 차단"},
        headers={"X-User-Id": str(personal_user)},
    )
    objective_id = created.json()["objective_id"]

    bad = client.patch(
        f"/personal-board/weekly-plan/objectives/{objective_id}",
        json={"project_id": str(uuid.uuid4())},
        headers={"X-User-Id": str(personal_user)},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"] == "project not found"

    missing = client.patch(
        f"/personal-board/weekly-plan/objectives/{uuid.uuid4()}",
        json={"title": "없는 목표"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert missing.status_code == 404


def test_weekly_review_counts_done_and_open_tasks(
    personal_user: uuid.UUID,
    wiki_docs: list[tuple[uuid.UUID, InternalDocument, str]],
) -> None:
    project = client.post(
        "/personal-board/projects",
        json={"name": "정산", "color": "#f3c150"},
        headers={"X-User-Id": str(personal_user)},
    )
    project_id = project.json()["project_id"]

    # 2026-06-04 is a Thursday; its week (Sunday-start) starts 2026-05-31.
    done = client.post(
        "/personal-board/tasks",
        json={"title": "완료한 일", "scheduled_date": "2026-06-04", "project_id": project_id},
        headers={"X-User-Id": str(personal_user)},
    )
    done_id = done.json()["task_id"]
    client.patch(
        f"/personal-board/tasks/{done_id}",
        json={"status": "done"},
        headers={"X-User-Id": str(personal_user)},
    )
    client.post(
        "/personal-board/tasks",
        json={"title": "미완료한 일", "scheduled_date": "2026-06-05"},
        headers={"X-User-Id": str(personal_user)},
    )

    review = client.get(
        "/personal-board/weekly-review?week_start=2026-05-31",
        headers={"X-User-Id": str(personal_user)},
    )
    assert review.status_code == 200
    body = review.json()
    assert body["week_start"] == "2026-05-31"
    assert body["total_done"] == 1
    assert body["total_open"] == 1
    assert len(body["daily"]) == 7
    # Sunday-start week: weekday index 0 = Sunday .. 6 = Saturday.
    assert [day["weekday"] for day in body["daily"]] == [0, 1, 2, 3, 4, 5, 6]
    thursday = body["daily"][4]
    assert thursday["date"] == "2026-06-04"
    assert thursday["done_count"] == 1
    assert thursday["done_tasks"][0]["title"] == "완료한 일"
    friday = body["daily"][5]
    assert friday["open_count"] == 1
    assert body["by_project"] == [
        {"project_id": project_id, "project_name": "정산", "color": "#f3c150", "done_count": 1}
    ]


def test_days_range_returns_clamped_window(
    personal_user: uuid.UUID,
) -> None:
    client.post(
        "/personal-board/tasks",
        json={"title": "월요일 할 일", "scheduled_date": "2026-06-08"},
        headers={"X-User-Id": str(personal_user)},
    )

    r = client.get(
        "/personal-board/days?start=2026-06-08&days=5",
        headers={"X-User-Id": str(personal_user)},
    )
    assert r.status_code == 200
    days = r.json()["days"]
    assert [day["date"] for day in days] == [
        "2026-06-08",
        "2026-06-09",
        "2026-06-10",
        "2026-06-11",
        "2026-06-12",
    ]
    assert days[0]["tasks"][0]["title"] == "월요일 할 일"

    clamped = client.get(
        "/personal-board/days?start=2026-06-08&days=99",
        headers={"X-User-Id": str(personal_user)},
    )
    assert clamped.status_code == 200
    assert len(clamped.json()["days"]) == 21


def test_duplicate_dated_task_copies_subtasks_and_respects_check(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "원본 일정", "scheduled_date": "2026-06-04", "scheduled_time": "09:30"},
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]
    client.post(
        f"/personal-board/tasks/{task_id}/subtasks",
        json={"title": "원본 하위작업"},
        headers={"X-User-Id": str(personal_user)},
    )

    duplicated = client.post(
        f"/personal-board/tasks/{task_id}/duplicate",
        headers={"X-User-Id": str(personal_user)},
    )
    assert duplicated.status_code == 201
    copy = duplicated.json()
    assert copy["task_id"] != task_id
    assert copy["title"] == "원본 일정"
    assert copy["status"] == "open"
    assert copy["completed"] is False
    assert copy["scheduled_date"] == "2026-06-04"
    assert copy["scheduled_time"] == "09:30:00"
    assert copy["backlog_bucket_id"] is None
    assert [sub["title"] for sub in copy["subtasks"]] == ["원본 하위작업"]
    assert all(sub["completed"] is False for sub in copy["subtasks"])

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    titles = [task["title"] for task in listed.json()["days"][0]["tasks"]]
    assert titles.count("원본 일정") == 2


def test_duplicate_backlog_task_keeps_bucket_placement(
    personal_user: uuid.UUID,
) -> None:
    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    bucket_id = listed.json()["backlog_buckets"][0]["bucket_id"]

    created = client.post(
        "/personal-board/tasks",
        json={"title": "백로그 원본", "backlog_bucket_id": bucket_id},
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]

    duplicated = client.post(
        f"/personal-board/tasks/{task_id}/duplicate",
        headers={"X-User-Id": str(personal_user)},
    )
    assert duplicated.status_code == 201
    copy = duplicated.json()
    assert copy["scheduled_date"] is None
    assert copy["backlog_bucket_id"] == bucket_id

    refreshed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    bucket = refreshed.json()["backlog_buckets"][0]
    assert [task["title"] for task in bucket["tasks"]].count("백로그 원본") == 2


def test_hard_delete_removes_task_and_subtasks(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "삭제 대상", "scheduled_date": "2026-06-04"},
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]
    client.post(
        f"/personal-board/tasks/{task_id}/subtasks",
        json={"title": "딸린 하위작업"},
        headers={"X-User-Id": str(personal_user)},
    )

    deleted = client.delete(
        f"/personal-board/tasks/{task_id}",
        headers={"X-User-Id": str(personal_user)},
    )
    assert deleted.status_code == 204

    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    titles = [task["title"] for task in listed.json()["days"][0]["tasks"]]
    assert "삭제 대상" not in titles

    again = client.delete(
        f"/personal-board/tasks/{task_id}",
        headers={"X-User-Id": str(personal_user)},
    )
    assert again.status_code == 404


def test_create_task_with_due_date_and_time(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={
            "title": "보고서 마감",
            "scheduled_date": "2026-06-04",
            "due_date": "2026-06-06",
            "due_time": "18:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["due_date"] == "2026-06-06"
    assert body["due_time"] == "18:00:00"

    # Survives a reload through the bootstrap projection.
    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    task = next(t for t in listed.json()["days"][0]["tasks"] if t["title"] == "보고서 마감")
    assert task["due_date"] == "2026-06-06"
    assert task["due_time"] == "18:00:00"


def test_due_time_requires_due_date_on_create(
    personal_user: uuid.UUID,
) -> None:
    rejected = client.post(
        "/personal-board/tasks",
        json={
            "title": "기한 없는 마감 시간",
            "scheduled_date": "2026-06-04",
            "due_time": "18:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert rejected.status_code == 422


def test_patch_due_date_and_clear_resets_time(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "마감일 편집", "scheduled_date": "2026-06-04"},
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]
    assert created.json()["due_date"] is None

    dated = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"due_date": "2026-06-08", "due_time": "09:15"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert dated.status_code == 200
    assert dated.json()["due_date"] == "2026-06-08"
    assert dated.json()["due_time"] == "09:15:00"

    # Clearing the date must also clear the time (no orphan time).
    cleared = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"due_date": None},
        headers={"X-User-Id": str(personal_user)},
    )
    assert cleared.status_code == 200
    assert cleared.json()["due_date"] is None
    assert cleared.json()["due_time"] is None


def test_patch_due_time_without_due_date_is_rejected(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "기한 가드", "scheduled_date": "2026-06-04"},
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]

    rejected = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"due_time": "10:00"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert rejected.status_code == 422


def test_duplicate_task_copies_due_date(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={
            "title": "마감 복제 원본",
            "scheduled_date": "2026-06-04",
            "due_date": "2026-06-09",
            "due_time": "12:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]

    duplicated = client.post(
        f"/personal-board/tasks/{task_id}/duplicate",
        headers={"X-User-Id": str(personal_user)},
    )
    assert duplicated.status_code == 201
    copy = duplicated.json()
    assert copy["task_id"] != task_id
    assert copy["due_date"] == "2026-06-09"
    assert copy["due_time"] == "12:00:00"


def test_create_task_with_scheduled_start_and_end(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={
            "title": "딥워크 블록",
            "scheduled_date": "2026-06-04",
            "scheduled_time": "09:00",
            "scheduled_end_time": "11:30",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["scheduled_time"] == "09:00:00"
    assert body["scheduled_end_time"] == "11:30:00"

    # Survives a reload through the bootstrap projection.
    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    task = next(t for t in listed.json()["days"][0]["tasks"] if t["title"] == "딥워크 블록")
    assert task["scheduled_time"] == "09:00:00"
    assert task["scheduled_end_time"] == "11:30:00"


def test_patch_set_scheduled_end_time(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "종료시각 편집", "scheduled_date": "2026-06-04", "scheduled_time": "13:00"},
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]
    assert created.json()["scheduled_end_time"] is None

    patched = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"scheduled_end_time": "14:15"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert patched.status_code == 200
    assert patched.json()["scheduled_time"] == "13:00:00"
    assert patched.json()["scheduled_end_time"] == "14:15:00"


def test_scheduled_end_without_start_is_rejected_on_create(
    personal_user: uuid.UUID,
) -> None:
    rejected = client.post(
        "/personal-board/tasks",
        json={
            "title": "시작 없는 종료",
            "scheduled_date": "2026-06-04",
            "scheduled_end_time": "11:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert rejected.status_code == 422


def test_scheduled_end_before_start_is_rejected_on_create(
    personal_user: uuid.UUID,
) -> None:
    rejected = client.post(
        "/personal-board/tasks",
        json={
            "title": "거꾸로 블록",
            "scheduled_date": "2026-06-04",
            "scheduled_time": "11:00",
            "scheduled_end_time": "10:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert rejected.status_code == 422

    # Equal start/end is also rejected (strictly greater).
    equal = client.post(
        "/personal-board/tasks",
        json={
            "title": "동일 블록",
            "scheduled_date": "2026-06-04",
            "scheduled_time": "11:00",
            "scheduled_end_time": "11:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert equal.status_code == 422


def test_patch_scheduled_end_without_start_is_rejected(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "종료 가드", "scheduled_date": "2026-06-04"},
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]

    rejected = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"scheduled_end_time": "10:00"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert rejected.status_code == 422

    # End <= start on patch is rejected too.
    started = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"scheduled_time": "10:00"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert started.status_code == 200
    too_early = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"scheduled_end_time": "09:30"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert too_early.status_code == 422


def test_move_to_backlog_clears_scheduled_times(
    personal_user: uuid.UUID,
) -> None:
    boot = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    bucket_id = boot.json()["backlog_buckets"][0]["bucket_id"]

    created = client.post(
        "/personal-board/tasks",
        json={
            "title": "백로그로 이동",
            "scheduled_date": "2026-06-04",
            "scheduled_time": "09:00",
            "scheduled_end_time": "10:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]

    moved = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"scheduled_date": None, "backlog_bucket_id": bucket_id},
        headers={"X-User-Id": str(personal_user)},
    )
    assert moved.status_code == 200
    assert moved.json()["scheduled_date"] is None
    assert moved.json()["scheduled_time"] is None
    assert moved.json()["scheduled_end_time"] is None


def test_clearing_scheduled_time_clears_end(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={
            "title": "시작 해제",
            "scheduled_date": "2026-06-04",
            "scheduled_time": "09:00",
            "scheduled_end_time": "10:00",
        },
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]

    cleared = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"scheduled_time": None},
        headers={"X-User-Id": str(personal_user)},
    )
    assert cleared.status_code == 200
    assert cleared.json()["scheduled_time"] is None
    assert cleared.json()["scheduled_end_time"] is None


def test_create_task_defaults_no_return_false(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "기본값", "scheduled_date": "2026-06-04"},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    assert created.json()["no_return"] is False


def test_create_task_with_no_return(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={
            "title": "외근 후 복귀안함",
            "scheduled_date": "2026-06-04",
            "scheduled_time": "15:00",
            "no_return": True,
        },
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    assert created.json()["no_return"] is True

    # Survives a reload through the bootstrap projection.
    listed = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    )
    task = next(t for t in listed.json()["days"][0]["tasks"] if t["title"] == "외근 후 복귀안함")
    assert task["no_return"] is True


def test_patch_no_return_toggle(
    personal_user: uuid.UUID,
) -> None:
    created = client.post(
        "/personal-board/tasks",
        json={"title": "복귀불가 토글", "scheduled_date": "2026-06-04", "scheduled_time": "13:00"},
        headers={"X-User-Id": str(personal_user)},
    )
    task_id = created.json()["task_id"]
    assert created.json()["no_return"] is False

    turned_on = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"no_return": True},
        headers={"X-User-Id": str(personal_user)},
    )
    assert turned_on.status_code == 200
    assert turned_on.json()["no_return"] is True
    # Independent flag: toggling it does not disturb the time block.
    assert turned_on.json()["scheduled_time"] == "13:00:00"

    turned_off = client.patch(
        f"/personal-board/tasks/{task_id}",
        json={"no_return": False},
        headers={"X-User-Id": str(personal_user)},
    )
    assert turned_off.status_code == 200
    assert turned_off.json()["no_return"] is False


def test_no_return_is_independent_of_schedule(
    personal_user: uuid.UUID,
) -> None:
    # A backlog task with no date/time can still be marked 복귀불가 (no schedule gate).
    bucket = client.get(
        "/personal-board/bootstrap?date=2026-06-04",
        headers={"X-User-Id": str(personal_user)},
    ).json()["backlog_buckets"][0]["bucket_id"]
    created = client.post(
        "/personal-board/tasks",
        json={"title": "백로그 복귀불가", "backlog_bucket_id": bucket, "no_return": True},
        headers={"X-User-Id": str(personal_user)},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["no_return"] is True
    assert body["scheduled_time"] is None
    assert body["scheduled_end_time"] is None
