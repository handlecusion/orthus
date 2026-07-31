"""Notion connector — implements Connector for the Notion API (prompt §7).

Block-type mapping (documented):

  Notion block type        → markdown prefix        → BlockNote type
  ─────────────────────────────────────────────────────────────────
  paragraph                → (none)                 → paragraph
  heading_1                → # …                    → heading (level 1)
  heading_2                → ## …                   → heading (level 2)
  heading_3                → ### …                  → heading (level 3)
  bulleted_list_item       → - …                    → bulletListItem
  numbered_list_item       → 1. …                   → numberedListItem
  to_do                    → - [ ] / - [x] …        → checkListItem
  quote                    → > …                    → blockQuote
  code                     → ```…```                → codeBlock
  divider                  → ---                    → (no content block)

Rich text is assembled by concatenating each span's `plain_text`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any
import uuid

import httpx

from orthus.connectors.project_map import resolve_project
from orthus.schemas.canonical import InternalDocument

# Inter-request delay (seconds). Set to 0 in tests via min_interval=0 constructor param.
_DEFAULT_MIN_INTERVAL: float = 0.35  # ~3 req/s headroom

# Maximum retries on HTTP 429.
_MAX_RETRIES = 3


def _rich_text_plain(rich_text: list[dict]) -> str:
    """Concatenate plain_text from a Notion rich_text array."""
    return "".join(rt.get("plain_text", "") for rt in rich_text)


def _map_block(block: dict) -> tuple[str, dict | None]:
    """Map a single Notion block to (markdown_line, blocknote_block | None).

    Returns (markdown, None) for blocks that have no BlockNote equivalent (e.g. divider).
    """
    btype = block.get("type", "")
    data = block.get(btype, {})
    rich_text: list[dict] = data.get("rich_text", [])
    text = _rich_text_plain(rich_text)

    # BlockNote content array for text-bearing blocks.
    def _bn_content(t: str) -> list[dict]:
        return [{"type": "text", "text": t, "styles": {}}]

    if btype == "paragraph":
        md = text
        bn: dict | None = {"type": "paragraph", "content": _bn_content(text)}

    elif btype == "heading_1":
        md = f"# {text}"
        bn = {"type": "heading", "props": {"level": 1}, "content": _bn_content(text)}

    elif btype == "heading_2":
        md = f"## {text}"
        bn = {"type": "heading", "props": {"level": 2}, "content": _bn_content(text)}

    elif btype == "heading_3":
        md = f"### {text}"
        bn = {"type": "heading", "props": {"level": 3}, "content": _bn_content(text)}

    elif btype == "bulleted_list_item":
        md = f"- {text}"
        bn = {"type": "bulletListItem", "content": _bn_content(text)}

    elif btype == "numbered_list_item":
        md = f"1. {text}"
        bn = {"type": "numberedListItem", "content": _bn_content(text)}

    elif btype == "to_do":
        checked = data.get("checked", False)
        box = "[x]" if checked else "[ ]"
        md = f"- {box} {text}"
        bn = {
            "type": "checkListItem",
            "props": {"checked": checked},
            "content": _bn_content(text),
        }

    elif btype == "quote":
        md = f"> {text}"
        bn = {"type": "blockQuote", "content": _bn_content(text)}

    elif btype == "code":
        lang = data.get("language", "")
        code_text = _rich_text_plain(data.get("rich_text", []))
        md = f"```{lang}\n{code_text}\n```"
        bn = {
            "type": "codeBlock",
            "props": {"language": lang},
            "content": _bn_content(code_text),
        }

    elif btype == "divider":
        md = "---"
        bn = None  # No BlockNote block for dividers

    else:
        # Unknown block type — emit plain text if available, skip otherwise.
        md = text
        bn = {"type": "paragraph", "content": _bn_content(text)} if text else None

    return md, bn


def _extract_title(page: dict) -> str:
    """Extract the page title from a Notion page object's properties."""
    props = page.get("properties", {})
    # The title property is typically named "title" or "Name".
    for prop in props.values():
        if prop.get("type") == "title":
            return _rich_text_plain(prop.get("title", []))
    return "(Untitled)"


def _person_name(person: dict) -> str:
    """Best-effort display name for a people/created_by/last_edited_by entry."""
    return person.get("name") or person.get("id", "")


def _render_date(value: dict | None) -> str:
    """Render a Notion date value as 'start' or 'start–end'."""
    if not value:
        return ""
    start = value.get("start", "")
    end = value.get("end")
    return f"{start}–{end}" if end else start


def _render_prop_value(prop: dict) -> str:
    """Render a single Notion property object → readable scalar string.

    Defensive: missing keys return "". Unknown types fall back to best-effort str.
    """
    ptype = prop.get("type", "")

    if ptype in ("title", "rich_text"):
        return _rich_text_plain(prop.get(ptype, []))
    if ptype in ("select", "status"):
        val = prop.get(ptype) or {}
        return val.get("name", "")
    if ptype == "multi_select":
        return ", ".join(o.get("name", "") for o in prop.get("multi_select", []))
    if ptype == "people":
        return ", ".join(_person_name(p) for p in prop.get("people", []))
    if ptype == "date":
        return _render_date(prop.get("date"))
    if ptype == "number":
        num = prop.get("number")
        return "" if num is None else str(num)
    if ptype == "checkbox":
        return "✓" if prop.get("checkbox") else "✗"
    if ptype in ("url", "email", "phone_number"):
        return prop.get(ptype) or ""
    if ptype == "formula":
        formula = prop.get("formula") or {}
        ftype = formula.get("type", "")
        if ftype == "date":
            return _render_date(formula.get("date"))
        if ftype == "checkbox":
            return "✓" if formula.get("checkbox") else "✗"
        val = formula.get(ftype)
        return "" if val is None else str(val)
    if ptype == "rollup":
        rollup = prop.get("rollup") or {}
        rtype = rollup.get("type", "")
        if rtype == "array":
            return ", ".join(_render_prop_value(item) for item in rollup.get("array", []))
        if rtype == "date":
            return _render_date(rollup.get("date"))
        val = rollup.get(rtype)
        return "" if val is None else str(val)
    if ptype == "relation":
        # IDs only — avoid N+1 page lookups (prompt §2).
        return ", ".join(r.get("id", "") for r in prop.get("relation", []))
    if ptype in ("created_time", "last_edited_time"):
        return prop.get(ptype) or ""
    if ptype == "files":
        names: list[str] = []
        for f in prop.get("files", []):
            name = f.get("name")
            url = (f.get("external") or {}).get("url") or (f.get("file") or {}).get("url", "")
            names.append(name or url)
        return ", ".join(n for n in names if n)
    if ptype in ("created_by", "last_edited_by"):
        return _person_name(prop.get(ptype) or {})

    # Unknown type — best-effort str of the type's payload.
    val = prop.get(ptype)
    return "" if val is None else str(val)


def _render_properties(props: dict) -> tuple[str, str]:
    """Render a Notion property map → (title, markdown).

    Title = the title-type property's plain_text. Markdown = one line per
    property: '**<name>**: <value>'.
    """
    title = "(Untitled)"
    lines: list[str] = []
    for name, prop in props.items():
        if prop.get("type") == "title":
            title = _rich_text_plain(prop.get("title", [])) or "(Untitled)"
        value = _render_prop_value(prop)
        lines.append(f"**{name}**: {value}")
    return title, "\n".join(lines)


def _structured_properties(props: dict) -> dict[str, str]:
    """Flat {prop_name: readable scalar/list} map for the structured(PG) store.

    Reuses `_render_prop_value` so the JSONB row carries the same readable values
    the markdown shows — never raw Notion API objects (P2.2a)."""
    return {name: _render_prop_value(prop) for name, prop in props.items()}


def _parse_last_edited(value: str) -> datetime | None:
    """Parse a Notion ISO last_edited_time into an aware datetime (or None)."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def normalize_notion_id(value: str) -> str:
    """Normalize Notion ids so hyphenated and compact UUID forms dedupe."""
    raw = (value or "").strip()
    if not raw:
        return raw
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError):
        return raw.lower()


def notion_canonical_source_id(page_id: str) -> str:
    return f"notion:page:{normalize_notion_id(page_id)}"


class NotionConnector:
    """Connector for the Notion API.

    Args:
        token: Notion integration token (bearer auth).
        client: Optional pre-built httpx.Client. If None, one is constructed.
            Pass an httpx.Client(transport=MockTransport(...)) for tests.
        min_interval: Minimum seconds between requests (default 0.35 ≈ 3 req/s).
            Set to 0 in tests to avoid delays.
        project_resolver: Optional ``(page_id, db_name) -> project`` callable.
            When given, it determines each document's project; otherwise the
            db_name heuristic (`resolve_project`) is used. `db_name` is the row's
            originating Notion DB (None for standalone pages); the resolver checks
            user overrides by db_name first, then falls back to id-set membership.
    """

    def __init__(
        self,
        token: str,
        *,
        client: httpx.Client | None = None,
        min_interval: float = _DEFAULT_MIN_INTERVAL,
        project_resolver: Callable[[str, str | None], str] | None = None,
    ) -> None:
        self._min_interval = min_interval
        self._last_request_at: float = 0.0
        # When set, the project of each yielded doc is derived by (page_id, db_name)
        # — user overrides by db_name take precedence, then id-set membership —
        # instead of the db_name heuristic (resolve_project).
        self._project_resolver = project_resolver

        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(
                base_url="https://api.notion.com/v1",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": "2022-06-28",
                },
                timeout=60.0,
            )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        """Rate-limited request with retry on transient 429 / timeout / 5xx.

        A bulk import makes thousands of calls; a single slow Notion response
        must not abort the whole run (the run-level iteration is not wrapped by
        the connector caller's per-doc error handling)."""
        for attempt in range(_MAX_RETRIES + 1):
            # Throttle to min_interval between requests.
            if self._min_interval > 0:
                elapsed = time.monotonic() - self._last_request_at
                wait = self._min_interval - elapsed
                if wait > 0:
                    time.sleep(wait)

            self._last_request_at = time.monotonic()
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.TimeoutException:
                if attempt >= _MAX_RETRIES:
                    raise
                time.sleep(1.5 * (attempt + 1))
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= _MAX_RETRIES:
                    resp.raise_for_status()
                retry_after = (
                    float(resp.headers.get("Retry-After", "1"))
                    if resp.status_code == 429
                    else 1.5 * (attempt + 1)
                )
                time.sleep(retry_after)
                continue

            resp.raise_for_status()
            return resp.json()

        # Unreachable — raise_for_status above covers it.
        raise RuntimeError("Exhausted retries")  # pragma: no cover

    def all_object_ids(self) -> set[str]:
        """Collect every object id this token can see via paginated /search.

        No object-type filter, no block/property fetch — id only — so a
        per-token id-set can be built cheaply to drive project resolution by
        page-set membership (P2 multitoken)."""
        ids: set[str] = set()
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor

            data = self._request("POST", "/search", json=body)
            for result in data.get("results", []):
                rid = result.get("id")
                if rid:
                    ids.add(rid)

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return ids

    def _search_pages(self) -> Iterator[dict]:
        """Yield all Notion page objects via paginated /search."""
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "filter": {"value": "page", "property": "object"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor

            data = self._request("POST", "/search", json=body)
            yield from data.get("results", [])

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    def _search_databases(self) -> Iterator[dict]:
        """Yield all Notion database objects via paginated /search."""
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {
                "filter": {"value": "database", "property": "object"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor

            data = self._request("POST", "/search", json=body)
            yield from data.get("results", [])

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    def _query_database(self, db_id: str) -> Iterator[dict]:
        """Yield all row (page) objects for a database via paginated query."""
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor

            data = self._request("POST", f"/databases/{db_id}/query", json=body)
            yield from data.get("results", [])

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    def _get_blocks(self, page_id: str) -> list[dict]:
        """Fetch all block children for a page (paginated)."""
        blocks: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor

            data = self._request("GET", f"/blocks/{page_id}/children", params=params)
            blocks.extend(data.get("results", []))

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return blocks

    def iter_documents(self, since: datetime | None) -> Iterator[InternalDocument]:
        """Yield one InternalDocument per standalone page and per database row.

        Pages whose parent is a database (`parent.type == "database_id"`) are skipped
        here — they are ingested via the database path below to avoid double block
        fetches. Same `since` incremental filter applies to both paths.
        """
        # --- standalone pages -------------------------------------------------
        for page in self._search_pages():
            # Database rows are also returned by /search?object=page; skip them
            # so the database path owns them (rendered from row properties).
            if page.get("parent", {}).get("type") == "database_id":
                continue

            last_edited_at = _parse_last_edited(page.get("last_edited_time", ""))

            # Incremental sync: skip pages not edited since `since`.
            if since is not None and last_edited_at is not None:
                if last_edited_at <= since:
                    continue

            page_id: str = page.get("id", "")
            title = _extract_title(page)
            blocks = self._get_blocks(page_id)

            # Project: the resolver (override-by-db_name then id-set membership)
            # takes precedence; otherwise atlas by default, nova when the page's
            # parent is the top-level Nova page (cheap single-hop check; no db_name).
            # Standalone pages have no db_name, so it is passed as None.
            if self._project_resolver is not None:
                page_project = self._project_resolver(page_id, None)
            else:
                parent = page.get("parent", {})
                parent_chain = (
                    [parent.get("page_id", "")] if parent.get("type") == "page_id" else []
                )
                page_project = resolve_project(db_name=None, parent_chain=parent_chain)

            md_lines: list[str] = []
            bn_blocks: list[dict] = []

            for block in blocks:
                md_line, bn_block = _map_block(block)
                if md_line:
                    md_lines.append(md_line)
                if bn_block is not None:
                    bn_blocks.append(bn_block)

            markdown = "\n\n".join(md_lines)

            yield InternalDocument(
                title=title,
                block_json=bn_blocks,
                markdown=markdown,
                source="notion",
                source_external_id=page_id,
                source_canonical_id=notion_canonical_source_id(page_id),
                source_last_edited_at=last_edited_at,
                project=page_project,
            )

        # --- database rows ----------------------------------------------------
        for db in self._search_databases():
            db_id: str = db.get("id", "")
            db_name = _rich_text_plain(db.get("title", [])) or db_id
            db_project = resolve_project(db_name=db_name)
            for row in self._query_database(db_id):
                last_edited_at = _parse_last_edited(row.get("last_edited_time", ""))

                if since is not None and last_edited_at is not None:
                    if last_edited_at <= since:
                        continue

                row_id: str = row.get("id", "")
                props = row.get("properties", {})
                title, markdown = _render_properties(props)

                # The resolver checks a user override by db_name first, then id-set
                # membership — both override the per-db db_name heuristic so a row's
                # project follows its override / page-set, not its db.
                row_project = (
                    self._project_resolver(row_id, db_name)
                    if self._project_resolver is not None
                    else db_project
                )

                yield InternalDocument(
                    title=title,
                    block_json=[],
                    markdown=markdown,
                    source="notion",
                    source_external_id=row_id,
                    source_canonical_id=notion_canonical_source_id(row_id),
                    source_db_name=db_name,
                    source_last_edited_at=last_edited_at,
                    project=row_project,
                    structured={
                        "db_id": db_id,
                        "db_name": db_name,
                        "row_id": row_id,
                        "properties": _structured_properties(props),
                    },
                )
