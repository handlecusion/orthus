"""Integration tests for the Notion connector.

Uses httpx.MockTransport so no live network calls are made.
Postgres must be up on localhost:5433 (run `make up && make migrate`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, select

from orthus.connectors import NotionConnector, run_import
from orthus.db import session
from orthus.tables import corpus_chunks, documents

# ---------------------------------------------------------------------------
# Canned Notion API responses
# ---------------------------------------------------------------------------

_PAGE_1_ID = "page-aaaa-0001"
_PAGE_2_ID = "page-bbbb-0002"

_EDITED_T1 = "2024-01-10T10:00:00.000Z"
_EDITED_T2 = "2024-01-10T11:00:00.000Z"

_SEARCH_RESPONSE: dict[str, Any] = {
    "object": "list",
    "results": [
        {
            "object": "page",
            "id": _PAGE_1_ID,
            "last_edited_time": _EDITED_T1,
            "properties": {
                "title": {
                    "type": "title",
                    "title": [{"plain_text": "Page One"}],
                }
            },
        },
        {
            "object": "page",
            "id": _PAGE_2_ID,
            "last_edited_time": _EDITED_T2,
            "properties": {
                "title": {
                    "type": "title",
                    "title": [{"plain_text": "Page Two"}],
                }
            },
        },
    ],
    "has_more": False,
    "next_cursor": None,
}

_BLOCKS_PAGE_1: dict[str, Any] = {
    "object": "list",
    "results": [
        {
            "type": "heading_1",
            "heading_1": {"rich_text": [{"plain_text": "Hello World"}]},
        },
        {
            "type": "paragraph",
            "paragraph": {"rich_text": [{"plain_text": "This is page one content."}]},
        },
        {
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"plain_text": "Bullet item"}]},
        },
    ],
    "has_more": False,
    "next_cursor": None,
}

_BLOCKS_PAGE_2: dict[str, Any] = {
    "object": "list",
    "results": [
        {
            "type": "paragraph",
            "paragraph": {"rich_text": [{"plain_text": "Page two body."}]},
        },
        {
            "type": "to_do",
            "to_do": {"rich_text": [{"plain_text": "A task"}], "checked": False},
        },
        {
            "type": "divider",
            "divider": {},
        },
    ],
    "has_more": False,
    "next_cursor": None,
}


def _make_handler(
    search_response: dict | None = None,
    blocks_page_1: dict | None = None,
    blocks_page_2: dict | None = None,
) -> Any:
    """Build a MockTransport handler serving canned responses.

    NOTE: httpx with base_url='https://api.notion.com/v1' produces paths like
    '/v1/search' and '/v1/blocks/{id}/children' in the handler's request object.
    """
    sr = search_response if search_response is not None else _SEARCH_RESPONSE
    bp1 = blocks_page_1 if blocks_page_1 is not None else _BLOCKS_PAGE_1
    bp2 = blocks_page_2 if blocks_page_2 is not None else _BLOCKS_PAGE_2

    empty_list = {"object": "list", "results": [], "has_more": False, "next_cursor": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            body = request.read().decode() if request.content else ""
            # These page-only fixtures have no databases.
            if "database" in body:
                return httpx.Response(200, json=empty_list)
            return httpx.Response(200, json=sr)
        if path == f"/v1/blocks/{_PAGE_1_ID}/children":
            return httpx.Response(200, json=bp1)
        if path == f"/v1/blocks/{_PAGE_2_ID}/children":
            return httpx.Response(200, json=bp2)
        return httpx.Response(404, json={"message": f"not found: {path}"})

    return handler


def _make_connector(handler: Any) -> NotionConnector:
    """Build a NotionConnector backed by MockTransport with zero inter-request delay."""
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.notion.com/v1",
    )
    return NotionConnector("test-token-placeholder", client=client, min_interval=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_documents(source: str = "notion") -> int:
    with session() as s:
        return s.execute(
            select(func.count()).select_from(documents).where(documents.c.source == source)
        ).scalar_one()


def _count_corpus_chunks_for(doc_id: UUID) -> int:
    with session() as s:
        return s.execute(
            select(func.count()).select_from(corpus_chunks).where(corpus_chunks.c.doc_id == doc_id)
        ).scalar_one()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_first_import_creates_documents(user_id: UUID) -> None:
    """First import should create 2 documents and corpus chunks for each."""
    connector = _make_connector(_make_handler())
    report = run_import(connector, user_id, since=None)

    assert report.created == 2
    assert report.updated == 0
    assert report.skipped == 0
    assert len(report.doc_ids) == 2

    assert _count_documents() == 2

    for doc_id in report.doc_ids:
        assert _count_corpus_chunks_for(doc_id) >= 1, f"Expected corpus chunks for doc_id={doc_id}"


def test_reimport_same_data_no_duplicates(user_id: UUID) -> None:
    """Re-importing with no since and same last_edited_time must not create duplicates."""
    connector = _make_connector(_make_handler())
    run_import(connector, user_id, since=None)
    assert _count_documents() == 2

    connector2 = _make_connector(_make_handler())
    report2 = run_import(connector2, user_id, since=None)

    assert _count_documents() == 2
    assert report2.skipped == 2
    assert report2.created == 0
    assert report2.updated == 0


def test_incremental_since_skips_all_when_since_gte_all_edits(user_id: UUID) -> None:
    """Incremental import with since >= all page edits should skip everything."""
    connector = _make_connector(_make_handler())
    run_import(connector, user_id, since=None)

    # since = _EDITED_T2 → both pages have last_edited_time <= since → connector yields 0 docs.
    since = datetime.fromisoformat(_EDITED_T2.replace("Z", "+00:00"))
    connector2 = _make_connector(_make_handler())
    report = run_import(connector2, user_id, since=since)

    assert report.created == 0
    assert report.updated == 0
    # Nothing yielded by connector → doc_ids is empty.
    assert len(report.doc_ids) == 0


def test_incremental_since_partial_skip(user_id: UUID) -> None:
    """since between the two page times: only the newer page is yielded; if its
    last_edited_time advanced it gets updated, otherwise skipped."""
    connector = _make_connector(_make_handler())
    run_import(connector, user_id, since=None)

    since = datetime.fromisoformat(_EDITED_T1.replace("Z", "+00:00"))

    # Build a search response where page 2 has advanced last_edited_time.
    _EDITED_T3 = "2024-01-10T12:00:00.000Z"
    advanced_search: dict = {
        "object": "list",
        "results": [
            {
                "object": "page",
                "id": _PAGE_1_ID,
                "last_edited_time": _EDITED_T1,
                "properties": {"title": {"type": "title", "title": [{"plain_text": "Page One"}]}},
            },
            {
                "object": "page",
                "id": _PAGE_2_ID,
                "last_edited_time": _EDITED_T3,
                "properties": {
                    "title": {
                        "type": "title",
                        "title": [{"plain_text": "Page Two Updated"}],
                    }
                },
            },
        ],
        "has_more": False,
        "next_cursor": None,
    }

    connector2 = _make_connector(_make_handler(search_response=advanced_search))
    report = run_import(connector2, user_id, since=since)

    # Page 1: last_edited_time == since → skipped by since filter (<=), not yielded.
    # Page 2: last_edited_time T3 > since=T1 → yielded; pre-existed + newer time → updated.
    assert report.updated == 1
    assert report.created == 0
    assert len(report.doc_ids) == 1
    assert _count_documents() == 2


def test_advanced_page_gets_updated(user_id: UUID) -> None:
    """A page whose last_edited_time advanced (no since) should produce updated=1."""
    connector = _make_connector(_make_handler())
    run_import(connector, user_id, since=None)

    _EDITED_T3 = "2024-01-11T08:00:00.000Z"
    advanced_search: dict = {
        "object": "list",
        "results": [
            {
                "object": "page",
                "id": _PAGE_1_ID,
                "last_edited_time": _EDITED_T3,
                "properties": {
                    "title": {
                        "type": "title",
                        "title": [{"plain_text": "Page One Revised"}],
                    }
                },
            },
        ],
        "has_more": False,
        "next_cursor": None,
    }

    connector2 = _make_connector(_make_handler(search_response=advanced_search))
    report = run_import(connector2, user_id, since=None)

    assert report.updated == 1
    assert report.created == 0
    assert report.skipped == 0
    assert _count_documents() == 2
