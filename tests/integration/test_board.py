"""Integration tests for the /board kanban endpoint.

Tests the status column mapping, local overlay, Notion write-back, and validation gate.
Postgres must be up on localhost:5433 (run `make up && make migrate`).
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orthus.api.main import app
from orthus.board import set_status
from orthus.db import session
from orthus.settings import get_settings
from orthus.tables import notion_rows, task_states

client = TestClient(app)

BOARD_DB = "Nova 개발 로드맵"


@pytest.fixture(autouse=True)
def _disable_live_notion_writeback(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "conn_notion_token", "")
    monkeypatch.setattr(settings, "conn_notion_token_1", "")
    monkeypatch.setattr(settings, "conn_notion_token_2", "")


def _seed_row(user_id: uuid.UUID, notion_status: str | None = None) -> uuid.UUID:
    """Insert a single notion_rows row for the board DB and return its row_id."""
    row_id = uuid.uuid4()
    props = {}
    if notion_status is not None:
        props["상태"] = notion_status
    with session() as s:
        s.execute(
            insert(notion_rows).values(
                row_id=row_id,
                db_id="db-nova-test",
                db_name=BOARD_DB,
                properties=props,
                scope="company",
                project="nova",
                user_id=user_id,
            )
        )
        s.commit()
    return row_id


def test_list_board_maps_status_columns(user_id: uuid.UUID) -> None:
    """Row with properties->>'상태'='In Progress' → card.status == 'In Progress',
    is_local_override == False."""
    row_id = _seed_row(user_id, notion_status="In Progress")

    r = client.get("/board", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    body = r.json()
    assert body["columns"] == ["Todo", "In Progress", "Review", "Done"]

    cards = {str(c["row_id"]): c for c in body["cards"]}
    assert str(row_id) in cards
    card = cards[str(row_id)]
    assert card["status"] == "In Progress"
    assert card["is_local_override"] is False
    assert card["notion_status"] == "In Progress"


def test_set_status_writes_overlay(user_id: uuid.UUID) -> None:
    """Without a Notion token, PATCH keeps the local overlay and reports disabled."""
    row_id = _seed_row(user_id, notion_status="Not Started")

    p = client.patch(
        f"/board/{row_id}", json={"status": "Done"}, headers={"X-User-Id": str(user_id)}
    )
    assert p.status_code == 200
    card = p.json()
    assert card["status"] == "Done"
    assert card["is_local_override"] is True
    assert card["writeback_status"] == "disabled"

    # task_states row persisted
    with session() as s:
        ts_status = s.execute(
            select(task_states.c.status).where(task_states.c.row_id == row_id)
        ).scalar()
    assert ts_status == "Done"

    # GET reflects overlay
    r = client.get("/board", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    cards = {str(c["row_id"]): c for c in r.json()["cards"]}
    assert cards[str(row_id)]["status"] == "Done"
    assert cards[str(row_id)]["is_local_override"] is True
    assert cards[str(row_id)]["writeback_status"] == "local_only"


def test_set_status_pushes_notion_and_clears_overlay(user_id: uuid.UUID) -> None:
    """Successful Notion PATCH updates the local row snapshot and clears overlay."""
    row_id = _seed_row(user_id, notion_status="Not Started")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"object": "page", "id": str(row_id)})

    notion_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.notion.com/v1",
    )

    card = set_status(
        user_id,
        row_id,
        "Done",
        notion_token="secret-token",
        notion_client=notion_client,
    )

    assert card.status == "Done"
    assert card.notion_status == "Done"
    assert card.is_local_override is False
    assert card.writeback_status == "synced"
    assert card.writeback_error is None

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path == f"/v1/pages/{row_id}"
    assert request.headers["authorization"] == "Bearer secret-token"
    assert request.headers["notion-version"] == "2022-06-28"
    assert json.loads(request.content) == {
        "properties": {
            "상태": {
                "status": {
                    "name": "Done",
                }
            }
        }
    }

    with session() as s:
        ts_status = s.execute(
            select(task_states.c.status).where(task_states.c.row_id == row_id)
        ).scalar()
        props = s.execute(
            select(notion_rows.c.properties).where(notion_rows.c.row_id == row_id)
        ).scalar_one()

    assert ts_status is None
    assert props["상태"] == "Done"


def test_set_status_prefers_nova_scoped_token(
    user_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nova board write-back uses ORTHUS_CONN_NOTION_TOKEN_2 when configured."""
    row_id = _seed_row(user_id, notion_status="Not Started")
    requests: list[httpx.Request] = []
    settings = get_settings()
    monkeypatch.setattr(settings, "conn_notion_token", "broad-token")
    monkeypatch.setattr(settings, "conn_notion_token_2", "nova-token")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"object": "page", "id": str(row_id)})

    notion_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.notion.com/v1",
    )

    card = set_status(
        user_id,
        row_id,
        "Done",
        notion_client=notion_client,
    )

    assert card.writeback_status == "synced"
    assert requests[0].headers["authorization"] == "Bearer nova-token"


def test_set_status_push_failure_keeps_overlay(user_id: uuid.UUID) -> None:
    """Failed Notion PATCH preserves local overlay and leaves Notion snapshot unchanged."""
    row_id = _seed_row(user_id, notion_status="Not Started")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "temporarily unavailable"})

    notion_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.notion.com/v1",
    )

    card = set_status(
        user_id,
        row_id,
        "Review",
        notion_token="secret-token",
        notion_client=notion_client,
    )

    assert card.status == "Review"
    assert card.notion_status == "Not Started"
    assert card.is_local_override is True
    assert card.writeback_status == "failed"
    assert card.writeback_error == "notion_http_500"

    with session() as s:
        ts_status = s.execute(
            select(task_states.c.status).where(task_states.c.row_id == row_id)
        ).scalar()
        props = s.execute(
            select(notion_rows.c.properties).where(notion_rows.c.row_id == row_id)
        ).scalar_one()

    assert ts_status == "Review"
    assert props["상태"] == "Not Started"


def test_set_status_rejects_unknown_column(user_id: uuid.UUID) -> None:
    """PATCH with an unmapped status string → 422."""
    row_id = _seed_row(user_id, notion_status="In Progress")
    r = client.patch(
        f"/board/{row_id}", json={"status": "Frobnicated"}, headers={"X-User-Id": str(user_id)}
    )
    assert r.status_code == 422


def test_unknown_notion_status_falls_to_todo(user_id: uuid.UUID) -> None:
    """A Notion status not in STATUS_MAP (e.g. '완료중') → card.status == 'Todo'."""
    row_id = _seed_row(user_id, notion_status="완료중")

    r = client.get("/board", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    cards = {str(c["row_id"]): c for c in r.json()["cards"]}
    assert cards[str(row_id)]["status"] == "Todo"
    assert cards[str(row_id)]["notion_status"] == "완료중"


def test_overlay_wins_over_notion(user_id: uuid.UUID) -> None:
    """Notion says 'Done'; overlay set to 'Todo' → GET returns 'Todo'."""
    row_id = _seed_row(user_id, notion_status="Done")

    p = client.patch(
        f"/board/{row_id}", json={"status": "Todo"}, headers={"X-User-Id": str(user_id)}
    )
    assert p.status_code == 200

    r = client.get("/board", headers={"X-User-Id": str(user_id)})
    assert r.status_code == 200
    cards = {str(c["row_id"]): c for c in r.json()["cards"]}
    assert cards[str(row_id)]["status"] == "Todo"
    assert cards[str(row_id)]["is_local_override"] is True
