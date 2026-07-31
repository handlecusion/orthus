"""프로젝트 인라인 데이터베이스 — 노션식 표/칸반.

칸반 보드 = 표 = 데이터베이스(같은 행 컬렉션의 다른 뷰)라는 노션 모델을 검증한다:
- 생성 시 이름(title)+상태(status) 속성과 표/보드 뷰가 시드된다.
- board view는 select/status 속성으로 group_by 한다.
- 행 추가/수정(props)/삭제, 속성 추가, 옵션(보드 컬럼) 추가가 동작한다.
- node 스코프 격리 + title 정확히 1개 검증.
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


def _new_project(uid: uuid.UUID) -> str:
    return client.post(
        "/dashboard/projects",
        json={"name": f"P-{uuid.uuid4().hex[:6]}", "color": None, "sort_order": 0, "active": True},
        headers=H(uid),
    ).json()["project_id"]


def test_create_seeds_title_status_and_views(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    out = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "업무", "default_view": "board"},
        headers=H(uid),
    )
    assert out.status_code == 201, out.text
    bundle = out.json()
    db = bundle["database"]
    assert db["title"] == "업무"
    assert bundle["rows"] == []

    # 정확히 하나의 title + 하나의 status.
    types = [p["type"] for p in db["properties"]]
    assert types.count("title") == 1
    assert "status" in types
    status = next(p for p in db["properties"] if p["type"] == "status")
    assert {o["name"] for o in status["options"]} == {"할 일", "진행 중", "완료"}

    # 표/보드 뷰가 둘 다, board는 default_view=board라 먼저, status로 group_by.
    view_types = [v["type"] for v in db["views"]]
    assert view_types == ["board", "table"]
    board = next(v for v in db["views"] if v["type"] == "board")
    assert board["group_by"] == status["id"]


def test_row_crud_and_board_move(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "보드", "default_view": "board"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    title_p = next(p for p in bundle["database"]["properties"] if p["type"] == "title")
    status_p = next(p for p in bundle["database"]["properties"] if p["type"] == "status")
    todo = next(o for o in status_p["options"] if o["name"] == "할 일")
    done = next(o for o in status_p["options"] if o["name"] == "완료")

    # 행 추가(할 일 컬럼).
    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={"props": {title_p["id"]: "디자인 시안", status_p["id"]: todo["id"]}},
        headers=H(uid),
    )
    assert row.status_code == 201, row.text
    row_id = row.json()["row_id"]

    # 보드에서 완료 컬럼으로 드래그(= status prop 값 변경).
    moved = client.patch(
        f"/dashboard/databases/{db_id}/rows/{row_id}",
        json={"props": {title_p["id"]: "디자인 시안", status_p["id"]: done["id"]}},
        headers=H(uid),
    )
    assert moved.status_code == 200
    assert moved.json()["props"][status_p["id"]] == done["id"]

    # 번들 재조회 시 반영.
    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    assert len(got["rows"]) == 1
    assert got["rows"][0]["props"][status_p["id"]] == done["id"]

    # 삭제.
    assert (
        client.delete(f"/dashboard/databases/{db_id}/rows/{row_id}", headers=H(uid)).status_code
        == 204
    )
    assert client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()["rows"] == []


def test_board_column_order_uses_row_sort_order(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "보드 순서", "default_view": "board"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    title_p = next(p for p in bundle["database"]["properties"] if p["type"] == "title")
    status_p = next(p for p in bundle["database"]["properties"] if p["type"] == "status")
    todo = next(o for o in status_p["options"] if o["name"] == "할 일")

    rows = []
    for name in ["A 카드", "B 카드", "C 카드"]:
        rows.append(
            client.post(
                f"/dashboard/databases/{db_id}/rows",
                json={"props": {title_p["id"]: name, status_p["id"]: todo["id"]}},
                headers=H(uid),
            ).json()
        )

    moved = client.patch(
        f"/dashboard/databases/{db_id}/rows/{rows[2]['row_id']}",
        json={"sort_order": 1.5},
        headers=H(uid),
    )
    assert moved.status_code == 200, moved.text

    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    todo_rows = [row for row in got["rows"] if row["props"][status_p["id"]] == todo["id"]]
    assert [row["props"][title_p["id"]] for row in todo_rows] == ["A 카드", "C 카드", "B 카드"]


def test_row_sort_order_persists_for_manual_table_order(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "수동 순서", "default_view": "table"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    title_p = next(p for p in bundle["database"]["properties"] if p["type"] == "title")

    created = []
    for name in ["첫 번째", "두 번째", "세 번째"]:
        created.append(
            client.post(
                f"/dashboard/databases/{db_id}/rows",
                json={"props": {title_p["id"]: name}},
                headers=H(uid),
            ).json()
        )
    assert [row["props"][title_p["id"]] for row in created] == ["첫 번째", "두 번째", "세 번째"]

    moved = client.patch(
        f"/dashboard/databases/{db_id}/rows/{created[2]['row_id']}",
        json={"sort_order": 0.5},
        headers=H(uid),
    )
    assert moved.status_code == 200, moved.text

    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    assert [row["props"][title_p["id"]] for row in got["rows"]] == [
        "세 번째",
        "첫 번째",
        "두 번째",
    ]


def test_row_duplicate_payload_preserves_page_and_media_metadata(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "복제 보드", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    title_p = next(p for p in bundle["database"]["properties"] if p["type"] == "title")
    status_p = next(p for p in bundle["database"]["properties"] if p["type"] == "status")
    files_p = next(p for p in bundle["database"]["properties"] if p["type"] == "files")
    todo = status_p["options"][0]
    media_values = [
        {
            "id": "img-demo",
            "name": "시안.png",
            "type": "image",
            "mime": "image/png",
            "size": 128,
            "url": "https://example.com/mock-image.png",
        },
        {
            "id": "video-demo",
            "name": "데모.mp4",
            "type": "video",
            "mime": "video/mp4",
            "size": 2048,
            "url": "https://example.com/mock-video.mp4",
        },
    ]
    page_body = '[{"type":"paragraph","content":[{"type":"text","text":"복제 본문"}]}]'

    source = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={
            "props": {
                title_p["id"]: "미디어 첨부 카드",
                status_p["id"]: todo["id"],
                files_p["id"]: media_values,
            },
            "sort_order": 1,
            "icon": "🎥",
            "cover": "https://example.com/cover.png",
            "body": page_body,
        },
        headers=H(uid),
    ).json()
    tail = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={
            "props": {title_p["id"]: "다음 카드", status_p["id"]: todo["id"]},
            "sort_order": 3,
        },
        headers=H(uid),
    ).json()

    duplicated = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={
            "props": source["props"],
            "sort_order": 2,
            "icon": source["icon"],
            "cover": source["cover"],
            "body": source["body"],
        },
        headers=H(uid),
    )
    assert duplicated.status_code == 201, duplicated.text
    copied = duplicated.json()
    assert copied["row_id"] != source["row_id"]
    assert copied["props"] == source["props"]
    assert copied["icon"] == source["icon"]
    assert copied["cover"] == source["cover"]
    assert copied["body"] == source["body"]
    assert copied["sort_order"] == 2
    assert copied["props"][files_p["id"]] == media_values

    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    assert [row["row_id"] for row in got["rows"]] == [
        source["row_id"],
        copied["row_id"],
        tail["row_id"],
    ]
    assert [row["props"][title_p["id"]] for row in got["rows"]] == [
        "미디어 첨부 카드",
        "미디어 첨부 카드",
        "다음 카드",
    ]


def test_property_duplicate_payload_preserves_options_and_row_values(
    company_user: uuid.UUID,
) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "속성 복제", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    title_p = next(p for p in props if p["type"] == "title")
    status_p = next(p for p in props if p["type"] == "status")
    files_p = next(p for p in props if p["type"] == "files")
    media_values = [
        {
            "id": "img-copy-demo",
            "name": "복제시안.png",
            "type": "image",
            "mime": "image/png",
            "size": 256,
            "url": "https://example.com/copy-image.png",
        }
    ]
    source_status = status_p["options"][1]
    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={
            "props": {
                title_p["id"]: "복제 속성 값",
                status_p["id"]: source_status["id"],
                files_p["id"]: media_values,
            }
        },
        headers=H(uid),
    ).json()

    option_id_map = {
        option["id"]: f"copy{idx:04d}" for idx, option in enumerate(status_p["options"])
    }
    copied_status = {
        **status_p,
        "id": "statcopy",
        "name": f"{status_p['name']} 복사본",
        "options": [
            {**option, "id": option_id_map[option["id"]]} for option in status_p["options"]
        ],
    }
    copied_files = {**files_p, "id": "filecopy", "name": f"{files_p['name']} 복사본"}
    duplicated_props = []
    for prop in props:
        duplicated_props.append(prop)
        if prop["id"] == status_p["id"]:
            duplicated_props.append(copied_status)
        if prop["id"] == files_p["id"]:
            duplicated_props.append(copied_files)

    saved_schema = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"properties": duplicated_props},
        headers=H(uid),
    )
    assert saved_schema.status_code == 200, saved_schema.text
    saved_status_copy = next(p for p in saved_schema.json()["properties"] if p["id"] == "statcopy")
    assert [o["id"] for o in saved_status_copy["options"]] == [
        option_id_map[option["id"]] for option in status_p["options"]
    ]
    assert [
        {k: v for k, v in option.items() if k != "id"} for option in saved_status_copy["options"]
    ] == [{k: v for k, v in option.items() if k != "id"} for option in status_p["options"]]

    patched = client.patch(
        f"/dashboard/databases/{db_id}/rows/{row['row_id']}",
        json={
            "props": {
                **row["props"],
                "statcopy": option_id_map[source_status["id"]],
                "filecopy": media_values,
            }
        },
        headers=H(uid),
    )
    assert patched.status_code == 200, patched.text
    saved_row = patched.json()
    assert saved_row["props"][status_p["id"]] == source_status["id"]
    assert saved_row["props"]["statcopy"] == option_id_map[source_status["id"]]
    assert saved_row["props"][files_p["id"]] == media_values
    assert saved_row["props"]["filecopy"] == media_values


def test_row_create_normalizes_props_against_schema(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post("/dashboard/databases", json={"project_id": pid}, headers=H(uid)).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    title_p = next(p for p in props if p["type"] == "title")
    status_p = next(p for p in props if p["type"] == "status")
    extra_props = [
        {"id": "num00001", "name": "점수", "type": "number", "options": []},
        {"id": "done0001", "name": "확인", "type": "checkbox", "options": []},
        {
            "id": "tags0001",
            "name": "태그",
            "type": "multi_select",
            "options": [
                {"id": "tag00001", "name": "디자인", "color": "blue"},
                {"id": "tag00002", "name": "개발", "color": "green"},
            ],
        },
        {"id": "people01", "name": "참여자", "type": "person", "options": []},
        {"id": "created1", "name": "등록일", "type": "created_time", "options": []},
    ]
    patched = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"properties": [*props, *extra_props]},
        headers=H(uid),
    )
    assert patched.status_code == 200, patched.text

    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={
            "props": {
                title_p["id"]: 123,
                status_p["id"]: "missing-option",
                "num00001": "3.5",
                "done0001": "true",
                "tags0001": ["tag00001", "bad-tag", "tag00001", "tag00002"],
                "people01": ["", "u1", 7, "u1", "u2"],
                "created1": "client-forged",
                "unknown01": "drop me",
            }
        },
        headers=H(uid),
    )
    assert row.status_code == 201, row.text
    saved = row.json()["props"]
    assert saved[title_p["id"]] == "123"
    assert saved[status_p["id"]] is None
    assert saved["num00001"] == 3.5
    assert saved["done0001"] is True
    assert saved["tags0001"] == ["tag00001", "tag00002"]
    assert saved["people01"] == ["u1", "u2"]
    assert "created1" not in saved
    assert "unknown01" not in saved


def test_row_update_normalizes_replacement_props(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    by_name = {p["name"]: p for p in bundle["database"]["properties"]}
    title_p = by_name["이름"]
    status_p = by_name["상태"]
    assignee_p = by_name["담당자"]
    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={"props": {title_p["id"]: "초기", status_p["id"]: status_p["options"][0]["id"]}},
        headers=H(uid),
    )
    assert row.status_code == 201, row.text
    row_id = row.json()["row_id"]

    patched = client.patch(
        f"/dashboard/databases/{db_id}/rows/{row_id}",
        json={
            "props": {
                title_p["id"]: "정규화",
                status_p["id"]: "bad-status",
                assignee_p["id"]: "single-user",
                "ghost": "not a property",
            }
        },
        headers=H(uid),
    )
    assert patched.status_code == 200, patched.text
    saved = patched.json()["props"]
    assert saved[title_p["id"]] == "정규화"
    assert saved[status_p["id"]] is None
    assert saved[assignee_p["id"]] == ["single-user"]
    assert "ghost" not in saved


def test_add_property_and_option(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post("/dashboard/databases", json={"project_id": pid}, headers=H(uid)).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]

    # 날짜 속성 추가 + status에 옵션 추가.
    props.append({"id": "due0001", "name": "마감", "type": "date", "options": []})
    status = next(p for p in props if p["type"] == "status")
    status["options"].append({"id": "rev00001", "name": "검토", "color": "yellow"})

    out = client.patch(f"/dashboard/databases/{db_id}", json={"properties": props}, headers=H(uid))
    assert out.status_code == 200, out.text
    names = [p["name"] for p in out.json()["properties"]]
    assert "마감" in names
    status2 = next(p for p in out.json()["properties"] if p["type"] == "status")
    assert "검토" in [o["name"] for o in status2["options"]]


def test_property_order_persists_for_table_columns(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    names = [p["name"] for p in props]
    priority_idx = names.index("우선순위")
    assignee_idx = names.index("담당자")
    assert assignee_idx < priority_idx

    priority = props.pop(priority_idx)
    props.insert(assignee_idx, priority)
    out = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"properties": props},
        headers=H(uid),
    )
    assert out.status_code == 200, out.text
    ordered_names = [p["name"] for p in out.json()["properties"]]
    assert ordered_names.index("우선순위") == assignee_idx
    assert ordered_names.index("담당자") == assignee_idx + 1

    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    assert [p["name"] for p in got["database"]["properties"]][:4] == [
        "이름",
        "상태",
        "우선순위",
        "담당자",
    ]


def test_option_order_persists_for_board_columns(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    status = next(p for p in props if p["name"] == "상태")
    names = [o["name"] for o in status["options"]]
    start_idx = names.index("시작 전")
    hold_idx = names.index("보류")
    assert start_idx < hold_idx
    assert status["options"][start_idx]["group"] == status["options"][hold_idx]["group"] == "to_do"

    hold = status["options"].pop(hold_idx)
    status["options"].insert(start_idx, hold)
    out = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"properties": props},
        headers=H(uid),
    )
    assert out.status_code == 200, out.text
    saved_status = next(p for p in out.json()["properties"] if p["id"] == status["id"])
    assert [o["name"] for o in saved_status["options"][:2]] == ["보류", "시작 전"]

    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    got_status = next(p for p in got["database"]["properties"] if p["id"] == status["id"])
    assert [o["name"] for o in got_status["options"][:2]] == ["보류", "시작 전"]


def test_property_delete_prunes_rows_and_dependent_views(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    views = bundle["database"]["views"]
    by_name = {p["name"]: p for p in props}
    title_p = by_name["이름"]
    priority_p = by_name["우선순위"]
    status_p = by_name["상태"]
    high = next(o for o in priority_p["options"] if o["name"] == "높음")

    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={
            "props": {
                title_p["id"]: "삭제된 속성 정리",
                status_p["id"]: status_p["options"][0]["id"],
                priority_p["id"]: high["id"],
            }
        },
        headers=H(uid),
    )
    assert row.status_code == 201, row.text

    board = views[0]
    board.update(
        {
            "group_by": priority_p["id"],
            "hidden": [priority_p["id"]],
            "filters": [
                {
                    "id": "filter01",
                    "prop_id": priority_p["id"],
                    "op": "is",
                    "value": high["id"],
                }
            ],
            "sorts": [{"id": "sort0001", "prop_id": priority_p["id"], "direction": "asc"}],
        }
    )
    saved = client.patch(f"/dashboard/databases/{db_id}", json={"views": views}, headers=H(uid))
    assert saved.status_code == 200, saved.text
    assert saved.json()["views"][0]["group_by"] == priority_p["id"]

    next_props = [p for p in props if p["id"] != priority_p["id"]]
    deleted = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"properties": next_props},
        headers=H(uid),
    )
    assert deleted.status_code == 200, deleted.text
    deleted_board = deleted.json()["views"][0]
    assert deleted_board["group_by"] == status_p["id"]
    assert deleted_board["hidden"] == []
    assert deleted_board["filters"] == []
    assert deleted_board["sorts"] == []

    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    assert priority_p["id"] not in got["rows"][0]["props"]
    assert got["rows"][0]["props"][title_p["id"]] == "삭제된 속성 정리"
    assert got["rows"][0]["props"][status_p["id"]] == status_p["options"][0]["id"]


def test_option_delete_prunes_row_values(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    by_name = {p["name"]: p for p in props}
    title_p = by_name["이름"]
    priority_p = by_name["우선순위"]
    status_p = by_name["상태"]
    high = next(o for o in priority_p["options"] if o["name"] == "높음")
    hotfix = next(o for o in priority_p["options"] if o["name"] == "핫픽스")
    todo = status_p["options"][0]

    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={
            "props": {
                title_p["id"]: "삭제된 옵션 정리",
                status_p["id"]: todo["id"],
                priority_p["id"]: high["id"],
            }
        },
        headers=H(uid),
    )
    assert row.status_code == 201, row.text

    for prop in props:
        if prop["id"] == priority_p["id"]:
            prop["options"] = [hotfix]
        if prop["id"] == status_p["id"]:
            prop["options"] = [o for o in prop["options"] if o["id"] != todo["id"]]

    patched = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"properties": props},
        headers=H(uid),
    )
    assert patched.status_code == 200, patched.text

    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    saved = got["rows"][0]["props"]
    assert saved[title_p["id"]] == "삭제된 옵션 정리"
    assert saved[priority_p["id"]] is None
    assert saved[status_p["id"]] is None


def test_option_rename_recolor_delete_lifecycle(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "default_view": "board"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    title_p = next(p for p in props if p["type"] == "title")
    status = next(p for p in props if p["type"] == "status")
    todo = next(o for o in status["options"] if o["name"] == "할 일")

    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={"props": {title_p["id"]: "컬럼 관리 QA", status["id"]: todo["id"]}},
        headers=H(uid),
    )
    assert row.status_code == 201, row.text
    row_id = row.json()["row_id"]

    for prop in props:
        if prop["id"] == status["id"]:
            prop["options"] = [
                {**option, "name": "백로그", "color": "purple"}
                if option["id"] == todo["id"]
                else option
                for option in prop["options"]
            ]

    renamed = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"properties": props},
        headers=H(uid),
    )
    assert renamed.status_code == 200, renamed.text
    status2 = next(p for p in renamed.json()["properties"] if p["type"] == "status")
    backlog = next(o for o in status2["options"] if o["id"] == todo["id"])
    assert backlog["name"] == "백로그"
    assert backlog["color"] == "purple"

    for prop in props:
        if prop["id"] == status["id"]:
            prop["options"] = [option for option in prop["options"] if option["id"] != todo["id"]]

    deleted = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"properties": props},
        headers=H(uid),
    )
    assert deleted.status_code == 200, deleted.text
    status3 = next(p for p in deleted.json()["properties"] if p["type"] == "status")
    assert todo["id"] not in [o["id"] for o in status3["options"]]

    moved = client.patch(
        f"/dashboard/databases/{db_id}/rows/{row_id}",
        json={"props": {title_p["id"]: "컬럼 관리 QA", status["id"]: None}},
        headers=H(uid),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["props"][status["id"]] is None


def test_view_filters_sorts_and_hidden_persist(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post("/dashboard/databases", json={"project_id": pid}, headers=H(uid)).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    title_p = next(p for p in props if p["type"] == "title")
    status_p = next(p for p in props if p["type"] == "status")
    view = bundle["database"]["views"][0]
    view.update(
        {
            "hidden": [status_p["id"], "missing-prop"],
            "column_widths": {title_p["id"]: 72, status_p["id"]: 900, "missing-prop": 180},
            "filters": [
                {
                    "id": "filter01",
                    "prop_id": status_p["id"],
                    "op": "is",
                    "value": status_p["options"][0]["id"],
                },
                {"id": "badprop01", "prop_id": "missing-prop", "op": "is", "value": "x"},
                {"id": "badop001", "prop_id": title_p["id"], "op": "unsupported", "value": "x"},
            ],
            "sorts": [
                {"id": "sort0001", "prop_id": title_p["id"], "direction": "asc"},
                {"id": "badprop02", "prop_id": "missing-prop", "direction": "desc"},
            ],
        }
    )

    out = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"views": [view, bundle["database"]["views"][1]]},
        headers=H(uid),
    )
    assert out.status_code == 200, out.text
    saved_view = out.json()["views"][0]
    assert saved_view["hidden"] == [status_p["id"]]
    assert saved_view["column_widths"] == {title_p["id"]: 96, status_p["id"]: 720}
    assert saved_view["filters"] == [
        {
            "id": "filter01",
            "prop_id": status_p["id"],
            "op": "is",
            "value": status_p["options"][0]["id"],
        }
    ]
    assert saved_view["sorts"] == [{"id": "sort0001", "prop_id": title_p["id"], "direction": "asc"}]


def test_view_duplicate_payload_preserves_view_settings(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "복제 보기", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    title_p = next(p for p in props if p["type"] == "title")
    status_p = next(p for p in props if p["type"] == "status")
    files_p = next(p for p in props if p["type"] == "files")
    board = bundle["database"]["views"][0]
    table = bundle["database"]["views"][1]
    board.update(
        {
            "cover_property": files_p["id"],
            "hidden": [files_p["id"]],
            "column_widths": {title_p["id"]: 320, files_p["id"]: 420},
            "filters": [
                {
                    "id": "filter-original",
                    "prop_id": status_p["id"],
                    "op": "is",
                    "value": status_p["options"][0]["id"],
                }
            ],
            "sorts": [{"id": "sort-original", "prop_id": title_p["id"], "direction": "asc"}],
        }
    )
    copied = {
        **board,
        "id": "view-copy",
        "name": f"{board['name']} 복사본",
        "hidden": [*board["hidden"]],
        "filters": [{**board["filters"][0], "id": "filter-copy"}],
        "sorts": [{**board["sorts"][0], "id": "sort-copy"}],
    }

    out = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"views": [board, copied, table]},
        headers=H(uid),
    )
    assert out.status_code == 200, out.text
    saved = out.json()["views"]
    saved_board = saved[0]
    saved_copy = saved[1]
    assert saved_copy["id"] != saved_board["id"]
    assert saved_copy["name"] == f"{saved_board['name']} 복사본"
    assert saved_copy["type"] == saved_board["type"] == "board"
    assert saved_copy["group_by"] == saved_board["group_by"] == status_p["id"]
    assert saved_copy["cover_property"] == saved_board["cover_property"] == files_p["id"]
    assert saved_copy["hidden"] == saved_board["hidden"] == [files_p["id"]]
    assert (
        saved_copy["column_widths"]
        == saved_board["column_widths"]
        == {
            title_p["id"]: 320,
            files_p["id"]: 420,
        }
    )
    assert saved_copy["filters"] == [
        {
            "id": "filter-copy",
            "prop_id": status_p["id"],
            "op": "is",
            "value": status_p["options"][0]["id"],
        }
    ]
    assert saved_copy["sorts"] == [
        {"id": "sort-copy", "prop_id": title_p["id"], "direction": "asc"}
    ]


def test_view_type_switch_payload_normalizes_layout_fields(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "보기 전환", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    status_p = next(p for p in props if p["type"] == "status")
    files_p = next(p for p in props if p["type"] == "files")
    board = bundle["database"]["views"][0]
    table = bundle["database"]["views"][1]

    table_as_board = {
        **table,
        "type": "board",
        "group_by": None,
        "cover_property": files_p["id"],
    }
    out = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"views": [board, table_as_board]},
        headers=H(uid),
    )
    assert out.status_code == 200, out.text
    saved_board = out.json()["views"][1]
    assert saved_board["type"] == "board"
    assert saved_board["group_by"] == status_p["id"]
    assert saved_board["cover_property"] == files_p["id"]

    board_as_table = {
        **saved_board,
        "type": "table",
        "group_by": None,
        "cover_property": files_p["id"],
    }
    out = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"views": [board, board_as_table]},
        headers=H(uid),
    )
    assert out.status_code == 200, out.text
    saved_table = out.json()["views"][1]
    assert saved_table["type"] == "table"
    assert saved_table["group_by"] is None
    assert saved_table["cover_property"] is None


def test_view_rename_delete_and_empty_rejected(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post("/dashboard/databases", json={"project_id": pid}, headers=H(uid)).json()
    db_id = bundle["database"]["database_id"]
    first_view = bundle["database"]["views"][0]
    first_view["name"] = "내 업무 보드"

    out = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"views": [first_view]},
        headers=H(uid),
    )
    assert out.status_code == 200, out.text
    assert [v["name"] for v in out.json()["views"]] == ["내 업무 보드"]

    empty = client.patch(f"/dashboard/databases/{db_id}", json={"views": []}, headers=H(uid))
    assert empty.status_code == 422


def test_reject_two_titles(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post("/dashboard/databases", json={"project_id": pid}, headers=H(uid)).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    props.append({"id": "t2000000", "name": "또이름", "type": "title", "options": []})
    out = client.patch(f"/dashboard/databases/{db_id}", json={"properties": props}, headers=H(uid))
    assert out.status_code == 422


def test_tasks_template_benchmarks_notion(company_user: uuid.UUID) -> None:
    """노션 '협업업무표' 100% 벤치마킹 시드 검증."""
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db = bundle["database"]
    by_name = {p["name"]: p for p in db["properties"]}
    # 핵심 속성 존재 + 타입.
    assert by_name["담당자"]["type"] == "person"
    assert by_name["우선순위"]["type"] == "select"
    assert by_name["예상 소요 시간"]["type"] == "select"
    assert by_name["종료일"]["type"] == "date"
    assert by_name["첨부"]["type"] == "files"
    assert by_name["등록일"]["type"] == "created_time"
    # 상태(status) 컬럼 그룹.
    status = by_name["상태"]
    assert status["type"] == "status"
    groups = {o["name"]: o.get("group") for o in status["options"]}
    assert groups == {
        "시작 전": "to_do",
        "진행 중": "in_progress",
        "완료": "complete",
        "보류": "to_do",
    }
    # 우선순위 옵션.
    pr = {o["name"] for o in by_name["우선순위"]["options"]}
    assert {"높음", "핫픽스", "보통", "낮음", "비상"} <= pr
    # 보드 뷰가 첫 뷰 + 상태로 group_by.
    assert db["views"][0]["type"] == "board"
    assert db["views"][0]["group_by"] == status["id"]
    assert db["views"][0]["cover_property"] == by_name["첨부"]["id"]


def test_board_cover_property_persists_and_prunes_invalid(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    by_name = {p["name"]: p for p in bundle["database"]["properties"]}
    views = bundle["database"]["views"]
    board = views[0]
    table = views[1]

    board["cover_property"] = by_name["우선순위"]["id"]
    out = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"views": [board, table]},
        headers=H(uid),
    )
    assert out.status_code == 200, out.text
    assert out.json()["views"][0]["cover_property"] is None

    board = out.json()["views"][0]
    table = out.json()["views"][1]
    board["cover_property"] = by_name["첨부"]["id"]
    table["cover_property"] = by_name["첨부"]["id"]
    out = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"views": [board, table]},
        headers=H(uid),
    )
    assert out.status_code == 200, out.text
    assert out.json()["views"][0]["cover_property"] == by_name["첨부"]["id"]
    assert out.json()["views"][1]["cover_property"] is None


def test_person_property_accepts_multiple_assignees(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    by_name = {p["name"]: p for p in bundle["database"]["properties"]}
    assignees = [str(uuid.uuid4()), str(uuid.uuid4())]
    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={
            "props": {
                by_name["이름"]["id"]: "담당자 복수 지정",
                by_name["담당자"]["id"]: assignees,
            }
        },
        headers=H(uid),
    )
    assert row.status_code == 201, row.text
    row_id = row.json()["row_id"]
    assert row.json()["props"][by_name["담당자"]["id"]] == assignees

    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    saved = next(r for r in got["rows"] if r["row_id"] == row_id)
    assert saved["props"][by_name["담당자"]["id"]] == assignees


def test_row_as_page_body_icon(company_user: uuid.UUID) -> None:
    """행 = 노션 페이지: icon + 본문 저장/조회 + created/updated 노출.

    기본 번들은 body를 포함하고(구 FE 배포 skew 호환), `?body=none` opt-in만
    목록 body를 생략한다. 단건 행 조회는 항상 body 전체를 준다."""
    uid = company_user
    pid = _new_project(uid)
    db_id = client.post("/dashboard/databases", json={"project_id": pid}, headers=H(uid)).json()[
        "database"
    ]["database_id"]
    title_p = next(
        p["id"]
        for p in client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()["database"][
            "properties"
        ]
        if p["type"] == "title"
    )
    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={"props": {title_p: "기획 문서"}, "icon": "📝", "body": "본문 초안"},
        headers=H(uid),
    ).json()
    assert row["icon"] == "📝"
    assert row["body"] == "본문 초안"
    assert row["created_at"] is not None
    # 본문/아이콘 수정.
    patched = client.patch(
        f"/dashboard/databases/{db_id}/rows/{row['row_id']}",
        json={"body": "본문 v2", "icon": "📄"},
        headers=H(uid),
    ).json()
    assert patched["body"] == "본문 v2"
    assert patched["icon"] == "📄"
    # 기본 번들 재조회: body 포함 유지(구 FE가 번들 body를 권위값으로 쓴다).
    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    assert got["rows"][0]["body"] == "본문 v2"
    assert got["rows"][0]["icon"] == "📄"
    assert got["rows"][0]["props"][title_p] == "기획 문서"
    # `?body=none` opt-in만 목록 body를 생략한다(None).
    slim = client.get(f"/dashboard/databases/{db_id}?body=none", headers=H(uid)).json()
    assert slim["rows"][0]["body"] is None
    assert slim["rows"][0]["icon"] == "📄"
    assert slim["rows"][0]["props"][title_p] == "기획 문서"
    # 허용값 외 body 파라미터는 422.
    assert (
        client.get(f"/dashboard/databases/{db_id}?body=partial", headers=H(uid)).status_code == 422
    )
    # 단건 행 조회는 body 전체를 준다.
    single = client.get(f"/dashboard/databases/{db_id}/rows/{row['row_id']}", headers=H(uid))
    assert single.status_code == 200, single.text
    assert single.json()["body"] == "본문 v2"
    assert single.json()["icon"] == "📄"
    # 없는 행은 404.
    assert (
        client.get(f"/dashboard/databases/{db_id}/rows/{uuid.uuid4()}", headers=H(uid)).status_code
        == 404
    )


def test_bundle_body_none_keeps_row_list_shape(company_user: uuid.UUID) -> None:
    """`?body=none` opt-in이 번들 행 수/props/정렬을 바꾸지 않는다(body만 None)."""
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post("/dashboard/databases", json={"project_id": pid}, headers=H(uid)).json()
    db_id = bundle["database"]["database_id"]
    title_p = next(p for p in bundle["database"]["properties"] if p["type"] == "title")
    for name, body in [("본문 있음", "긴 본문"), ("본문 없음", None)]:
        created = client.post(
            f"/dashboard/databases/{db_id}/rows",
            json={"props": {title_p["id"]: name}, "body": body},
            headers=H(uid),
        )
        assert created.status_code == 201, created.text

    # 기본 번들은 body를 포함한다(저장값 그대로).
    full = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    assert len(full["rows"]) == 2
    assert [r["props"][title_p["id"]] for r in full["rows"]] == ["본문 있음", "본문 없음"]
    assert [r["body"] for r in full["rows"]] == ["긴 본문", None]

    # `?body=none`은 body만 생략하고 행 수/props/정렬은 동일하다.
    slim = client.get(f"/dashboard/databases/{db_id}?body=none", headers=H(uid)).json()
    assert len(slim["rows"]) == len(full["rows"])
    assert [r["props"][title_p["id"]] for r in slim["rows"]] == [
        r["props"][title_p["id"]] for r in full["rows"]
    ]
    assert [r["row_id"] for r in slim["rows"]] == [r["row_id"] for r in full["rows"]]
    assert all(r["body"] is None for r in slim["rows"])


def test_database_meta_returns_summary_without_rows(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "메타", "icon": "🗂️"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    title_p = next(p for p in bundle["database"]["properties"] if p["type"] == "title")
    for name in ["하나", "둘", "셋"]:
        client.post(
            f"/dashboard/databases/{db_id}/rows",
            json={"props": {title_p["id"]: name}},
            headers=H(uid),
        )

    meta = client.get(f"/dashboard/databases/{db_id}/meta", headers=H(uid))
    assert meta.status_code == 200, meta.text
    out = meta.json()
    assert out == {
        "database_id": db_id,
        "project_id": pid,
        "title": "메타",
        "icon": "🗂️",
        "row_count": 3,
    }
    # row_count는 번들 rows 길이와 항상 일치.
    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    assert len(got["rows"]) == out["row_count"]
    # 없는 데이터베이스는 404.
    assert (
        client.get(f"/dashboard/databases/{uuid.uuid4()}/meta", headers=H(uid)).status_code == 404
    )


def test_database_meta_node_scope_isolation(
    company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = company_user
    pid = _new_project(uid)
    db_id = client.post("/dashboard/databases", json={"project_id": pid}, headers=H(uid)).json()[
        "database"
    ]["database_id"]
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    assert client.get(f"/dashboard/databases/{db_id}/meta", headers=H(uid)).status_code == 404
    assert (
        client.get(f"/dashboard/databases/{db_id}/rows/{uuid.uuid4()}", headers=H(uid)).status_code
        == 404
    )


def test_progress_by_project_batched_parity(company_user: uuid.UUID) -> None:
    """배치 집계가 데이터베이스별 GROUP BY 루프와 같은 결과를 준다.

    status 그룹 분류(complete/in_progress/빈 값→to_do), status 속성 없는
    데이터베이스 제외, 행 0개 데이터베이스의 total=0 엔트리 유지를 고정한다."""
    uid = company_user
    pid_a = _new_project(uid)
    pid_b = _new_project(uid)
    pid_c = _new_project(uid)

    # A: tasks 템플릿(상태 그룹 to_do/in_progress/complete) + 행 4개.
    bundle_a = client.post(
        "/dashboard/databases",
        json={"project_id": pid_a, "title": "진행률 A", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_a = bundle_a["database"]["database_id"]
    title_a = next(p for p in bundle_a["database"]["properties"] if p["type"] == "title")
    status_a = next(p for p in bundle_a["database"]["properties"] if p["type"] == "status")
    by_group = {o.get("group"): o for o in status_a["options"]}
    rows_a = [
        ("완료 업무", by_group["complete"]["id"]),
        ("진행 업무", by_group["in_progress"]["id"]),
        ("대기 업무", by_group["to_do"]["id"]),
        ("상태 없음", None),
    ]
    for name, status in rows_a:
        props: dict = {title_a["id"]: name}
        if status is not None:
            props[status_a["id"]] = status
        assert (
            client.post(
                f"/dashboard/databases/{db_a}/rows", json={"props": props}, headers=H(uid)
            ).status_code
            == 201
        )

    # B: status 속성이 있으나 행이 없는 데이터베이스 → total=0 엔트리.
    client.post(
        "/dashboard/databases",
        json={"project_id": pid_b, "title": "진행률 B"},
        headers=H(uid),
    )

    # C: status 속성 없는 데이터베이스 → 집계 대상 아님.
    bundle_c = client.post(
        "/dashboard/databases",
        json={"project_id": pid_c, "title": "진행률 C"},
        headers=H(uid),
    ).json()
    db_c = bundle_c["database"]["database_id"]
    props_c = [p for p in bundle_c["database"]["properties"] if p["type"] != "status"]
    assert (
        client.patch(
            f"/dashboard/databases/{db_c}", json={"properties": props_c}, headers=H(uid)
        ).status_code
        == 200
    )

    out = client.get("/dashboard/projects/progress", headers=H(uid))
    assert out.status_code == 200, out.text
    by_project = {p["project_id"]: p for p in out.json()}
    assert by_project[pid_a] == {
        "project_id": pid_a,
        "total": 4,
        "complete": 1,
        "in_progress": 1,
    }
    assert by_project[pid_b] == {
        "project_id": pid_b,
        "total": 0,
        "complete": 0,
        "in_progress": 0,
    }
    assert pid_c not in by_project


def test_files_property_persists_media_attachments(company_user: uuid.UUID) -> None:
    """Notion files property: image/video/file attachment metadata survives row CRUD."""
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    title_p = next(p for p in bundle["database"]["properties"] if p["type"] == "title")
    files_p = next(p for p in bundle["database"]["properties"] if p["type"] == "files")
    attachments = [
        {
            "id": "img0000001",
            "name": "시안.png",
            "type": "image",
            "mime": "image/png",
            "size": 1200,
            "dataUrl": "data:image/png;base64,iVBORw0KGgo=",
        },
        {
            "id": "vid0000001",
            "name": "데모.mp4",
            "type": "video",
            "mime": "video/mp4",
            "size": 2048,
            "url": "https://example.com/demo.mp4",
        },
        {
            "id": "file000001",
            "name": "요구사항.pdf",
            "type": "file",
            "mime": "application/pdf",
            "size": 4096,
            "url": "https://example.com/spec.pdf",
        },
    ]

    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={"props": {title_p["id"]: "첨부 확인", files_p["id"]: attachments}},
        headers=H(uid),
    )
    assert row.status_code == 201, row.text
    row_id = row.json()["row_id"]
    assert row.json()["props"][files_p["id"]][0]["type"] == "image"

    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    saved = got["rows"][0]["props"][files_p["id"]]
    assert [item["name"] for item in saved] == ["시안.png", "데모.mp4", "요구사항.pdf"]

    reordered = [attachments[1], attachments[0], attachments[2]]
    patched = client.patch(
        f"/dashboard/databases/{db_id}/rows/{row_id}",
        json={"props": {title_p["id"]: "첨부 확인", files_p["id"]: reordered}},
        headers=H(uid),
    )
    assert patched.status_code == 200, patched.text
    assert [item["name"] for item in patched.json()["props"][files_p["id"]]] == [
        "데모.mp4",
        "시안.png",
        "요구사항.pdf",
    ]
    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    saved = got["rows"][0]["props"][files_p["id"]]
    assert [item["name"] for item in saved] == ["데모.mp4", "시안.png", "요구사항.pdf"]

    trimmed = attachments[:1]
    patched = client.patch(
        f"/dashboard/databases/{db_id}/rows/{row_id}",
        json={"props": {title_p["id"]: "첨부 확인", files_p["id"]: trimmed}},
        headers=H(uid),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["props"][files_p["id"]] == trimmed


def test_database_file_upload_serves_bytes_and_deletes(company_user: uuid.UUID) -> None:
    """Uploaded files live outside props JSONB but round-trip as Notion files values."""
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    title_p = next(p for p in bundle["database"]["properties"] if p["type"] == "title")
    files_p = next(p for p in bundle["database"]["properties"] if p["type"] == "files")
    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={"props": {title_p["id"]: "업로드 첨부"}},
        headers=H(uid),
    ).json()
    row_id = row["row_id"]

    payload = b"\x89PNG\r\n\x1a\nfake-image"
    uploaded = client.post(
        f"/dashboard/databases/{db_id}/rows/{row_id}/files",
        files={"file": ("시안.png", payload, "image/png")},
        headers=H(uid),
    )
    assert uploaded.status_code == 201, uploaded.text
    file_value = uploaded.json()
    assert file_value["name"] == "시안.png"
    assert file_value["type"] == "image"
    assert file_value["mime"] == "image/png"
    assert file_value["size"] == len(payload)
    assert file_value["url"].startswith(f"/dashboard/databases/{db_id}/files/")
    assert file_value["dataUrl"] is None

    patched = client.patch(
        f"/dashboard/databases/{db_id}/rows/{row_id}",
        json={"props": {title_p["id"]: "업로드 첨부", files_p["id"]: [file_value]}},
        headers=H(uid),
    )
    assert patched.status_code == 200, patched.text
    saved = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()["rows"][0]["props"][
        files_p["id"]
    ]
    assert saved[0]["url"] == file_value["url"]

    fetched = client.get(file_value["url"], headers=H(uid))
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == payload
    assert fetched.headers["content-type"].startswith("image/png")
    assert "inline" in fetched.headers["content-disposition"]

    deleted = client.delete(file_value["url"], headers=H(uid))
    assert deleted.status_code == 204
    assert client.get(file_value["url"], headers=H(uid)).status_code == 404


def test_row_patch_removing_file_value_deletes_stored_blob(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    title_p = next(p for p in bundle["database"]["properties"] if p["type"] == "title")
    files_p = next(p for p in bundle["database"]["properties"] if p["type"] == "files")
    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={"props": {title_p["id"]: "API 첨부 제거"}},
        headers=H(uid),
    ).json()
    row_id = row["row_id"]
    uploaded = client.post(
        f"/dashboard/databases/{db_id}/rows/{row_id}/files",
        files={"file": ("demo.mp4", b"fake-video", "video/mp4")},
        headers=H(uid),
    )
    assert uploaded.status_code == 201, uploaded.text
    file_value = uploaded.json()

    patched = client.patch(
        f"/dashboard/databases/{db_id}/rows/{row_id}",
        json={"props": {title_p["id"]: "API 첨부 제거", files_p["id"]: [file_value]}},
        headers=H(uid),
    )
    assert patched.status_code == 200, patched.text
    assert client.get(file_value["url"], headers=H(uid)).status_code == 200

    removed = client.patch(
        f"/dashboard/databases/{db_id}/rows/{row_id}",
        json={"props": {title_p["id"]: "API 첨부 제거"}},
        headers=H(uid),
    )
    assert removed.status_code == 200, removed.text
    assert files_p["id"] not in removed.json()["props"]
    assert client.get(file_value["url"], headers=H(uid)).status_code == 404


def test_files_property_delete_removes_stored_blobs(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    props = bundle["database"]["properties"]
    title_p = next(p for p in props if p["type"] == "title")
    files_p = next(p for p in props if p["type"] == "files")
    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={"props": {title_p["id"]: "파일 속성 삭제"}},
        headers=H(uid),
    ).json()
    row_id = row["row_id"]
    uploaded = client.post(
        f"/dashboard/databases/{db_id}/rows/{row_id}/files",
        files={"file": ("시안.png", b"fake-image", "image/png")},
        headers=H(uid),
    )
    assert uploaded.status_code == 201, uploaded.text
    file_value = uploaded.json()
    assert (
        client.patch(
            f"/dashboard/databases/{db_id}/rows/{row_id}",
            json={"props": {title_p["id"]: "파일 속성 삭제", files_p["id"]: [file_value]}},
            headers=H(uid),
        ).status_code
        == 200
    )

    without_files = [p for p in props if p["id"] != files_p["id"]]
    patched = client.patch(
        f"/dashboard/databases/{db_id}",
        json={"properties": without_files},
        headers=H(uid),
    )
    assert patched.status_code == 200, patched.text
    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    assert files_p["id"] not in got["rows"][0]["props"]
    assert client.get(file_value["url"], headers=H(uid)).status_code == 404


def test_delete_database_file_removes_row_references(company_user: uuid.UUID) -> None:
    uid = company_user
    pid = _new_project(uid)
    bundle = client.post(
        "/dashboard/databases",
        json={"project_id": pid, "title": "협업업무표", "template": "tasks"},
        headers=H(uid),
    ).json()
    db_id = bundle["database"]["database_id"]
    title_p = next(p for p in bundle["database"]["properties"] if p["type"] == "title")
    files_p = next(p for p in bundle["database"]["properties"] if p["type"] == "files")
    row = client.post(
        f"/dashboard/databases/{db_id}/rows",
        json={"props": {title_p["id"]: "첨부 삭제 API"}},
        headers=H(uid),
    ).json()
    row_id = row["row_id"]
    uploaded = client.post(
        f"/dashboard/databases/{db_id}/rows/{row_id}/files",
        files={"file": ("삭제.png", b"fake-image", "image/png")},
        headers=H(uid),
    )
    assert uploaded.status_code == 201, uploaded.text
    file_value = uploaded.json()
    assert (
        client.patch(
            f"/dashboard/databases/{db_id}/rows/{row_id}",
            json={"props": {title_p["id"]: "첨부 삭제 API", files_p["id"]: [file_value]}},
            headers=H(uid),
        ).status_code
        == 200
    )

    deleted = client.delete(file_value["url"], headers=H(uid))
    assert deleted.status_code == 204
    got = client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).json()
    assert got["rows"][0]["props"][files_p["id"]] == []


def test_node_scope_isolation(company_user: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> None:
    uid = company_user
    pid = _new_project(uid)
    db_id = client.post("/dashboard/databases", json={"project_id": pid}, headers=H(uid)).json()[
        "database"
    ]["database_id"]

    # 다른 노드에서는 보이지 않는다(company-node 전용이라 personal은 404).
    settings = get_settings()
    monkeypatch.setattr(settings, "node_kind", "personal")
    assert client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).status_code == 404


def test_ensure_board_creates_once_and_reuses_existing(company_user: uuid.UUID) -> None:
    """모든 프로젝트 기본 칸반 보드 보장(멱등) — 없으면 생성, 있으면 재사용."""
    uid = company_user
    pid = _new_project(uid)

    # 보드가 없으면 tasks 템플릿 '업무 보드'를 만든다(첫 뷰 = board).
    first = client.post(f"/dashboard/projects/{pid}/board/ensure", headers=H(uid))
    assert first.status_code == 200, first.text
    db = first.json()
    assert db["title"] == "업무 보드"
    assert db["views"][0]["type"] == "board"
    prop_names = [p["name"] for p in db["properties"]]
    assert "상태" in prop_names and "담당자" in prop_names

    # 재호출은 같은 데이터베이스를 반환하고 새로 만들지 않는다.
    again = client.post(f"/dashboard/projects/{pid}/board/ensure", headers=H(uid))
    assert again.json()["database_id"] == db["database_id"]
    dbs = client.get(f"/dashboard/projects/{pid}/databases", headers=H(uid)).json()
    assert len(dbs) == 1

    # 이미 보드 뷰가 있는 프로젝트(예: 아틀라스 협업업무표)는 그 보드를 재사용한다.
    pid2 = _new_project(uid)
    existing = client.post(
        "/dashboard/databases",
        json={"project_id": pid2, "title": "협업 업무표", "template": "tasks"},
        headers=H(uid),
    ).json()["database"]
    ensured = client.post(f"/dashboard/projects/{pid2}/board/ensure", headers=H(uid)).json()
    assert ensured["database_id"] == existing["database_id"]

    # 없는 프로젝트는 404.
    missing = client.post(f"/dashboard/projects/{uuid.uuid4()}/board/ensure", headers=H(uid))
    assert missing.status_code == 404


def test_delete_project_removes_linked_databases(company_user: uuid.UUID) -> None:
    """프로젝트 삭제 시 소속(+하위 프로젝트 소속) 데이터베이스가 고아로 남지 않는다."""
    uid = company_user
    pid = _new_project(uid)
    sub = client.post(
        "/dashboard/projects",
        json={
            "name": f"S-{uuid.uuid4().hex[:6]}",
            "sort_order": 0,
            "active": True,
            "parent_project_id": pid,
        },
        headers=H(uid),
    ).json()["project_id"]
    root_db = client.post(f"/dashboard/projects/{pid}/board/ensure", headers=H(uid)).json()
    sub_db = client.post(f"/dashboard/projects/{sub}/board/ensure", headers=H(uid)).json()

    assert client.delete(f"/dashboard/projects/{pid}", headers=H(uid)).status_code == 204
    for db_id in (root_db["database_id"], sub_db["database_id"]):
        assert client.get(f"/dashboard/databases/{db_id}", headers=H(uid)).status_code == 404
