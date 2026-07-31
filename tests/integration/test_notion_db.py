"""Integration tests for Notion DATABASE ingestion (P2.0).

Uses httpx.MockTransport so no live network calls are made. Covers:
  - database rows → InternalDocuments with rendered property markdown,
  - standalone page ingestion alongside databases,
  - database-child pages SKIPPED by the page path (no double-ingest),
  - incremental `since` filtering of rows,
  - run_import created/updated/skipped tallies.

Postgres must be up on localhost:5433 (run `make up && make migrate`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select

from orthus.connectors import NotionConnector, run_import
from orthus.connectors.notion import _render_properties
from orthus.db import session
from orthus.documents import _row_uuid
from orthus.tables import documents, notion_rows

# ---------------------------------------------------------------------------
# IDs / timestamps
# ---------------------------------------------------------------------------

_DB_ID = "db-tickets-0001"

_ROW_1_ID = "row-aaaa-0001"
_ROW_2_ID = "row-bbbb-0002"

_STANDALONE_PAGE_ID = "page-standalone-0001"
_DB_CHILD_PAGE_ID = "page-dbchild-0002"  # a row of _DB_ID, must be skipped by page path

_EDITED_T1 = "2024-02-01T10:00:00.000Z"
_EDITED_T2 = "2024-02-01T11:00:00.000Z"

_REL_PAGE_A = "rel-page-aaaa"
_REL_PAGE_B = "rel-page-bbbb"

# ---------------------------------------------------------------------------
# Canned responses
# ---------------------------------------------------------------------------

# /search object=database
_SEARCH_DB_RESPONSE: dict[str, Any] = {
    "object": "list",
    "results": [{"object": "database", "id": _DB_ID}],
    "has_more": False,
    "next_cursor": None,
}

# /search object=page → one standalone page + one database-child page
_SEARCH_PAGE_RESPONSE: dict[str, Any] = {
    "object": "list",
    "results": [
        {
            "object": "page",
            "id": _STANDALONE_PAGE_ID,
            "last_edited_time": _EDITED_T1,
            "parent": {"type": "workspace", "workspace": True},
            "properties": {
                "title": {"type": "title", "title": [{"plain_text": "Standalone Page"}]}
            },
        },
        {
            "object": "page",
            "id": _DB_CHILD_PAGE_ID,
            "last_edited_time": _EDITED_T1,
            "parent": {"type": "database_id", "database_id": _DB_ID},
            "properties": {
                "이름": {"type": "title", "title": [{"plain_text": "DB Child (skip me)"}]}
            },
        },
    ],
    "has_more": False,
    "next_cursor": None,
}

_BLOCKS_STANDALONE: dict[str, Any] = {
    "object": "list",
    "results": [
        {
            "type": "paragraph",
            "paragraph": {"rich_text": [{"plain_text": "Standalone body."}]},
        }
    ],
    "has_more": False,
    "next_cursor": None,
}


def _row(row_id: str, last_edited: str, title: str, priority: str) -> dict:
    """A database row (page object) with varied property types."""
    return {
        "object": "page",
        "id": row_id,
        "last_edited_time": last_edited,
        "parent": {"type": "database_id", "database_id": _DB_ID},
        "properties": {
            "이름": {"type": "title", "title": [{"plain_text": title}]},
            "상태": {"type": "status", "status": {"name": "진행중"}},
            "우선순위": {"type": "select", "select": {"name": priority}},
            "태그": {
                "type": "multi_select",
                "multi_select": [{"name": "backend"}, {"name": "urgent"}],
            },
            "기간": {"type": "date", "date": {"start": "2024-02-01", "end": "2024-02-05"}},
            "담당자": {
                "type": "people",
                "people": [{"name": "Alice"}, {"id": "user-bob"}],
            },
            "관련": {
                "type": "relation",
                "relation": [{"id": _REL_PAGE_A}, {"id": _REL_PAGE_B}],
            },
            "포인트": {"type": "number", "number": 8},
            "완료": {"type": "checkbox", "checkbox": False},
        },
    }


_DB_QUERY_RESPONSE: dict[str, Any] = {
    "object": "list",
    "results": [
        _row(_ROW_1_ID, _EDITED_T1, "티켓 A", "높음"),
        _row(_ROW_2_ID, _EDITED_T2, "티켓 B", "낮음"),
    ],
    "has_more": False,
    "next_cursor": None,
}


def _make_handler(
    db_query_response: dict | None = None,
    page_search_response: dict | None = None,
) -> Any:
    dbq = db_query_response if db_query_response is not None else _DB_QUERY_RESPONSE
    psr = page_search_response if page_search_response is not None else _SEARCH_PAGE_RESPONSE

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            body = request.read().decode() if request.content else ""
            if "database" in body:
                return httpx.Response(200, json=_SEARCH_DB_RESPONSE)
            return httpx.Response(200, json=psr)
        if path == f"/v1/databases/{_DB_ID}/query":
            return httpx.Response(200, json=dbq)
        if path == f"/v1/blocks/{_STANDALONE_PAGE_ID}/children":
            return httpx.Response(200, json=_BLOCKS_STANDALONE)
        return httpx.Response(404, json={"message": f"not found: {path}"})

    return handler


def _make_connector(handler: Any) -> NotionConnector:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.notion.com/v1",
    )
    return NotionConnector("test-token-placeholder", client=client, min_interval=0)


def _docs_by_external_id() -> dict[str, dict]:
    with session() as s:
        rows = s.execute(
            select(
                documents.c.source_external_id,
                documents.c.title,
                documents.c.markdown,
            ).where(documents.c.source == "notion")
        ).all()
    return {r[0]: {"title": r[1], "markdown": r[2]} for r in rows}


# ---------------------------------------------------------------------------
# Pure rendering unit-ish test (no DB needed, but kept here for cohesion)
# ---------------------------------------------------------------------------


def test_render_properties_covers_types() -> None:
    props = _row(_ROW_1_ID, _EDITED_T1, "티켓 A", "높음")["properties"]
    title, markdown = _render_properties(props)

    assert title == "티켓 A"
    assert "**상태**: 진행중" in markdown
    assert "**우선순위**: 높음" in markdown
    assert "**태그**: backend, urgent" in markdown
    assert "**기간**: 2024-02-01–2024-02-05" in markdown
    assert "**담당자**: Alice, user-bob" in markdown
    assert f"**관련**: {_REL_PAGE_A}, {_REL_PAGE_B}" in markdown
    assert "**포인트**: 8" in markdown
    assert "**완료**: ✗" in markdown


# ---------------------------------------------------------------------------
# Import integration tests
# ---------------------------------------------------------------------------


def test_db_rows_and_standalone_page_ingested_child_skipped(user_id: UUID) -> None:
    """DB rows + standalone page ingested; database-child page skipped (no double)."""
    connector = _make_connector(_make_handler())
    report = run_import(connector, user_id, since=None)

    # 2 db rows + 1 standalone page = 3 documents. DB-child page is NOT separately ingested.
    assert report.created == 3
    assert report.updated == 0
    assert report.skipped == 0

    docs = _docs_by_external_id()
    assert set(docs.keys()) == {_ROW_1_ID, _ROW_2_ID, _STANDALONE_PAGE_ID}
    assert _DB_CHILD_PAGE_ID not in docs

    # Spot-check rendered property markdown on a db row.
    assert docs[_ROW_1_ID]["title"] == "티켓 A"
    assert "**상태**: 진행중" in docs[_ROW_1_ID]["markdown"]

    # Standalone page keeps body-block markdown.
    assert "Standalone body." in docs[_STANDALONE_PAGE_ID]["markdown"]


def test_incremental_since_filters_db_rows(user_id: UUID) -> None:
    """since between the two row times: only the newer row is yielded."""
    connector = _make_connector(_make_handler())
    run_import(connector, user_id, since=None)

    # Advance row 2 so it is newer than `since`; keep row 1 at the cutoff.
    _EDITED_T3 = "2024-02-02T09:00:00.000Z"
    advanced_query: dict = {
        "object": "list",
        "results": [
            _row(_ROW_1_ID, _EDITED_T1, "티켓 A", "높음"),
            _row(_ROW_2_ID, _EDITED_T3, "티켓 B Updated", "낮음"),
        ],
        "has_more": False,
        "next_cursor": None,
    }
    # Standalone page unchanged (== since) → skipped by since filter.
    since = datetime.fromisoformat(_EDITED_T1.replace("Z", "+00:00"))

    connector2 = _make_connector(_make_handler(db_query_response=advanced_query))
    report = run_import(connector2, user_id, since=since)

    # Row 1 (== since) skipped by filter; row 2 (> since, advanced) → updated.
    # Standalone page (== since) and db-child skipped by filter / page path.
    assert report.updated == 1
    assert report.created == 0
    assert len(report.doc_ids) == 1


def test_reimport_db_no_duplicates(user_id: UUID) -> None:
    """Re-importing the same data must not create duplicates and tally skipped."""
    connector = _make_connector(_make_handler())
    run_import(connector, user_id, since=None)

    connector2 = _make_connector(_make_handler())
    report2 = run_import(connector2, user_id, since=None)

    assert report2.created == 0
    assert report2.updated == 0
    assert report2.skipped == 3
    assert len(_docs_by_external_id()) == 3


def test_db_row_import_writes_notion_rows(user_id: UUID) -> None:
    """A Notion DB row import also persists a typed row into `notion_rows`
    (structured store), idempotent on re-import (P2.2a)."""
    connector = _make_connector(_make_handler())
    run_import(connector, user_id, since=None)

    with session() as s:
        rows = s.execute(
            select(
                notion_rows.c.row_id,
                notion_rows.c.db_id,
                notion_rows.c.db_name,
                notion_rows.c.properties,
                notion_rows.c.scope,
                notion_rows.c.user_id,
            )
        ).all()
    by_row = {str(r.row_id): r for r in rows}

    # one notion_rows row per DB row (the standalone page has no `structured`).
    assert len(rows) == 2
    r1 = by_row[str(_row_uuid(_ROW_1_ID))]
    assert r1.db_id == _DB_ID
    assert r1.db_name == _DB_ID  # mock database object has no title -> falls back to id
    assert r1.properties["상태"] == "진행중"
    assert r1.properties["우선순위"] == "높음"
    assert r1.scope == "company"
    assert r1.user_id == user_id

    # re-import is idempotent: still exactly 2 rows.
    connector2 = _make_connector(_make_handler())
    run_import(connector2, user_id, since=None)
    with session() as s:
        n = s.execute(select(notion_rows.c.row_id)).all()
    assert len(n) == 2
