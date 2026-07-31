"""Integration tests for multitoken Notion ingest (P2 multitenant).

Project is assigned by which integration token's page-set a page belongs to
(id-set membership), not by the db_name heuristic. Three tokens on one
workspace: broad (company, sees everything), atlas subtree, nova subtree.

Uses httpx.MockTransport so no live network calls are made. Postgres must be up
on localhost:5433 (run `make up && make migrate`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select

from orthus.connectors import NotionConnector, run_import
from orthus.connectors.notion_import import _build_project_resolver
from orthus.db import session
from orthus.documents import _row_uuid
from orthus.tables import documents, notion_rows

# ---------------------------------------------------------------------------
# all_object_ids() — collects ids across pagination
# ---------------------------------------------------------------------------


def _paginated_search_handler(pages: list[dict]) -> Any:
    """Serve /search results across multiple pages (one `pages` entry per page)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/search":
            return httpx.Response(404, json={"message": f"not found: {request.url.path}"})
        body = request.read().decode() if request.content else ""
        # Pick the page by the cursor echoed in start_cursor (None => first page).
        idx = 0
        for i, pg in enumerate(pages):
            cursor_token = pg.get("_cursor_token")
            if cursor_token and cursor_token in body:
                idx = i
                break
        page = pages[idx]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": page["results"],
                "has_more": page["has_more"],
                "next_cursor": page.get("next_cursor"),
            },
        )

    return handler


def _connector(handler: Any) -> NotionConnector:
    client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.notion.com/v1"
    )
    return NotionConnector("test-token", client=client, min_interval=0)


def test_all_object_ids_collects_across_pagination() -> None:
    pages = [
        {
            "results": [{"id": "a-1"}, {"id": "a-2"}],
            "has_more": True,
            "next_cursor": "CUR2",
        },
        {
            "_cursor_token": "CUR2",
            "results": [{"id": "b-1"}, {"id": "b-2"}, {"id": "b-3"}],
            "has_more": False,
            "next_cursor": None,
        },
    ]
    conn = _connector(_paginated_search_handler(pages))
    ids = conn.all_object_ids()
    assert ids == {"a-1", "a-2", "b-1", "b-2", "b-3"}


# ---------------------------------------------------------------------------
# _build_project_resolver — id-set membership (nova > atlas > company)
# ---------------------------------------------------------------------------


def test_project_resolver_membership() -> None:
    atlas_ids = {"c-1", "c-2", "shared-na"}
    nova_ids = {"s-1", "s-2"}
    resolve = _build_project_resolver(atlas_ids, nova_ids)

    assert resolve("s-1", None) == "nova"  # in nova-set
    assert resolve("c-1", None) == "atlas"  # in atlas-set only
    assert resolve("company-only-46", None) == "company"  # in neither


# ---------------------------------------------------------------------------
# Ingest: id-set membership overrides the db_name heuristic
# ---------------------------------------------------------------------------

_DB_ID = "db-not-nova-0001"  # db_name does NOT match the Nova heuristic
_NOVA_ROW_ID = "row-nova-member"  # but this row's id IS in the nova id-set
_CAST_ROW_ID = "row-atlas-member"
_STANDALONE_PAGE_ID = "page-company-only"  # in neither id-set -> company


def _row(row_id: str) -> dict:
    return {
        "object": "page",
        "id": row_id,
        "last_edited_time": "2024-03-01T10:00:00.000Z",
        "parent": {"type": "database_id", "database_id": _DB_ID},
        "properties": {
            "이름": {"type": "title", "title": [{"plain_text": "행"}]},
            "상태": {"type": "status", "status": {"name": "진행중"}},
        },
    }


def _ingest_handler() -> Any:
    db_search = {
        "object": "list",
        # db_name "티켓" would resolve to 'atlas' under the db_name heuristic.
        "results": [{"object": "database", "id": _DB_ID, "title": [{"plain_text": "티켓"}]}],
        "has_more": False,
        "next_cursor": None,
    }
    page_search = {
        "object": "list",
        "results": [
            {
                "object": "page",
                "id": _STANDALONE_PAGE_ID,
                "last_edited_time": "2024-03-01T10:00:00.000Z",
                "parent": {"type": "workspace", "workspace": True},
                "properties": {
                    "title": {"type": "title", "title": [{"plain_text": "회사 페이지"}]}
                },
            }
        ],
        "has_more": False,
        "next_cursor": None,
    }
    blocks = {
        "object": "list",
        "results": [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "본문."}]}}],
        "has_more": False,
        "next_cursor": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            body = request.read().decode() if request.content else ""
            if "database" in body:
                return httpx.Response(200, json=db_search)
            return httpx.Response(200, json=page_search)
        if path == f"/v1/databases/{_DB_ID}/query":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "results": [_row(_NOVA_ROW_ID), _row(_CAST_ROW_ID)],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        if path == f"/v1/blocks/{_STANDALONE_PAGE_ID}/children":
            return httpx.Response(200, json=blocks)
        return httpx.Response(404, json={"message": f"not found: {path}"})

    return handler


def test_ingest_project_by_id_set_overrides_db_name(user_id: UUID) -> None:
    """A row whose db_name is NOT Nova but whose page_id is in the nova id-set
    is still stamped 'nova' — proving id-set membership overrides db_name."""
    # atlas id-set contains the atlas row; nova id-set contains the nova row.
    atlas_ids = {_CAST_ROW_ID}
    nova_ids = {_NOVA_ROW_ID}
    resolver = _build_project_resolver(atlas_ids, nova_ids)

    client = httpx.Client(
        transport=httpx.MockTransport(_ingest_handler()),
        base_url="https://api.notion.com/v1",
    )
    connector = NotionConnector(
        "broad-token", client=client, min_interval=0, project_resolver=resolver
    )
    run_import(connector, user_id, since=None)

    with session() as s:
        nova_proj = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.row_id == _row_uuid(_NOVA_ROW_ID))
        ).scalar()
        cast_proj = s.execute(
            select(notion_rows.c.project).where(notion_rows.c.row_id == _row_uuid(_CAST_ROW_ID))
        ).scalar()
    # db_name "티켓" → 'atlas' under heuristic, but id-set membership wins.
    assert nova_proj == "nova"
    assert cast_proj == "atlas"

    # Standalone page in neither id-set → company (heuristic would say atlas).
    with session() as s:
        page_proj = s.execute(
            select(documents.c.project).where(documents.c.source_external_id == _STANDALONE_PAGE_ID)
        ).scalar()
    assert page_proj == "company"
