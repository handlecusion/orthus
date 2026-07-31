"""Notion write-back helpers.

Read ingest stays in ``orthus.connectors.notion``. This module owns explicit
Notion mutations so write paths remain easy to audit and test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from orthus.audit.logger import audit

NOTION_API_BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_STATUS_PROPERTY = "상태"

NotionWritebackStatus = Literal["synced", "disabled", "failed"]


@dataclass(frozen=True)
class NotionWritebackResult:
    status: NotionWritebackStatus
    notion_status: str | None = None
    error: str | None = None


def push_page_status(
    page_id: str,
    notion_status: str,
    *,
    token: str,
    client: httpx.Client | None = None,
    property_name: str = NOTION_STATUS_PROPERTY,
) -> NotionWritebackResult:
    """PATCH a Notion page status property.

    Empty token is a deterministic local-only mode, not an exception. HTTP
    failures are returned as ``failed`` so callers can preserve local overlays.
    """

    token = token.strip()
    with audit("notion.writeback.status") as span:
        span.add_meta(page_id=page_id, property_name=property_name, notion_status=notion_status)

        if not token:
            result = NotionWritebackResult(
                status="disabled",
                notion_status=notion_status,
                error="ORTHUS_CONN_NOTION_TOKEN is not configured",
            )
            span.set_output({"status": result.status, "error": result.error})
            return result

        owned_client = client is None
        if client is None:
            client = httpx.Client(base_url=NOTION_API_BASE_URL, timeout=30.0)

        try:
            response = client.patch(
                f"/pages/{page_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": NOTION_VERSION,
                },
                json={
                    "properties": {
                        property_name: {
                            "status": {
                                "name": notion_status,
                            }
                        }
                    }
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            result = NotionWritebackResult(
                status="failed",
                notion_status=notion_status,
                error=f"notion_http_{exc.response.status_code}",
            )
            span.set_output({"status": result.status, "error": result.error})
            return result
        except httpx.HTTPError as exc:
            result = NotionWritebackResult(
                status="failed",
                notion_status=notion_status,
                error=type(exc).__name__,
            )
            span.set_output({"status": result.status, "error": result.error})
            return result
        finally:
            if owned_client:
                client.close()

        result = NotionWritebackResult(status="synced", notion_status=notion_status)
        span.set_output({"status": result.status})
        return result
