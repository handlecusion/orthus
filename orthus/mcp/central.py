"""HTTPS client for the central knowledge endpoints (P8.7b MCP surface).

The owner's local MCP server talks to central over HTTPS with a knowledge-scoped
collector token. Base URL comes from ``ORTHUS_MCP_CENTRAL_URL``; the token comes
from ``ORTHUS_MCP_TOKEN`` first, else the macOS Keychain via
``security find-generic-password -s orthus-mcp-token -w`` (argv subprocess, never
a shell string). The token is never logged.

Errors are normalized to ``CentralError`` with a clear, leak-free message so the
server can surface a useful ``ToolError`` (unreachable / 401 revoked / 403
missing scope / 429 rate limited).
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import httpx

CENTRAL_URL_ENV = "ORTHUS_MCP_CENTRAL_URL"
TOKEN_ENV = "ORTHUS_MCP_TOKEN"
KEYCHAIN_SERVICE = "orthus-mcp-token"
# 120s, not 30s: the `structured` tool's central endpoint runs the codex NL→SQL
# compile (~20s, slower under load) which exceeded a 30s read timeout and made the
# cli agent's structured tool fail. The agent's overall run budget (180s) bounds it.
DEFAULT_TIMEOUT_SECONDS = 120.0
# Reverse proxies use 502/503/504 for a temporarily unavailable upstream;
# Tailscale Serve/Funnel can surface the same condition as 530.  A brief retry
# keeps read-only CLI/MCP queries from failing during a central process restart
# or tunnel handoff.  Writes are deliberately excluded because retrying them
# without an endpoint-specific idempotency key could duplicate an action.
TRANSIENT_STATUS_CODES = frozenset({502, 503, 504, 530})
RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
READ_RETRY_DELAYS_SECONDS = (0.25, 0.75)


class CentralError(RuntimeError):
    """A central knowledge request failed; message is safe to surface.

    ``status`` carries the HTTP status code (None for transport errors) so
    callers (orthus ticket CLI) can map 404/422/429 to actionable guidance."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CentralConfig:
    base_url: str
    token: str


def _keychain_token() -> str | None:
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if completed.returncode != 0:
        return None
    token = completed.stdout.strip()
    return token or None


def resolve_token() -> str | None:
    env_token = os.environ.get(TOKEN_ENV, "").strip()
    if env_token:
        return env_token
    return _keychain_token()


def resolve_config() -> CentralConfig:
    base_url = os.environ.get(CENTRAL_URL_ENV, "").strip()
    if not base_url:
        raise CentralError(f"{CENTRAL_URL_ENV} is not set; point it at the central API base URL")
    token = resolve_token()
    if not token:
        raise CentralError(
            f"knowledge token not found (set {TOKEN_ENV} or store it in Keychain "
            f"service '{KEYCHAIN_SERVICE}')"
        )
    return CentralConfig(base_url=base_url.rstrip("/"), token=token)


def _explain_status(status: int) -> str:
    if status == 401:
        return "central rejected the knowledge token (401: revoked or invalid)"
    if status == 403:
        return "knowledge token is missing the required scope (403)"
    if status == 429:
        return "central rate limited the token (429: too many /ask calls)"
    if status == 404:
        return "central endpoint not found (404: knowledge surface unavailable)"
    if status in TRANSIENT_STATUS_CODES:
        return f"central temporarily unavailable (HTTP {status}; retry shortly)"
    return f"central returned HTTP {status}"


class CentralClient:
    """Thin HTTPS caller for the central knowledge endpoints."""

    def __init__(self, config: CentralConfig | None = None) -> None:
        self._config = config or resolve_config()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self._config.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._config.token}"}
        retryable = method.upper() in RETRYABLE_METHODS
        response: httpx.Response | None = None
        for attempt in range(len(READ_RETRY_DELAYS_SECONDS) + 1):
            try:
                with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
                    response = client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                if retryable and attempt < len(READ_RETRY_DELAYS_SECONDS):
                    time.sleep(READ_RETRY_DELAYS_SECONDS[attempt])
                    continue
                raise CentralError(
                    f"central unreachable at {self._config.base_url} ({type(exc).__name__})"
                ) from None
            if (
                retryable
                and response.status_code in TRANSIENT_STATUS_CODES
                and attempt < len(READ_RETRY_DELAYS_SECONDS)
            ):
                time.sleep(READ_RETRY_DELAYS_SECONDS[attempt])
                continue
            break

        assert response is not None  # every non-exception attempt assigns it
        if response.status_code >= 400:
            message = _explain_status(response.status_code)
            # Surface the server's own `detail` (e.g. validation reasons like
            # "task must have exactly one placement") — it is server-generated
            # and token-free, and the CLI needs it for actionable errors.
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            if isinstance(detail, str) and detail.strip():
                message = f"{message}: {detail.strip()}"
            raise CentralError(message, status=response.status_code)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise CentralError("central returned a non-JSON response") from None

    def gateway_submit_action(self, payload: dict[str, Any]) -> dict:
        """P10.4b: submit a gated commit-action candidate through the P3 policy
        gate. The endpoint (not this call) decides the outcome; requires an
        agent_task-scoped token whose owner is an operator."""
        return self._request("POST", "/collector/gateway/actions", json=payload)

    def wiki_search(self, query: str, *, scope: str, limit: int) -> dict:
        return self._request(
            "GET",
            "/wiki/search",
            params={"query": query, "scope": scope, "limit": limit},
        )

    def wiki_page(self, slug: str) -> dict:
        return self._request("GET", f"/wiki/pages/{slug}")

    def wiki_ask(self, question: str, *, scope: str, context_wiki_slug: str | None) -> dict:
        # Pin the wiki backend. /ask auto-routes, and a knowledge question is often
        # mis-routed to the structured NL→SQL backend, which then runs the wrong SQL or
        # gate-rejects (422) — so wiki_ask, a company knowledge grounding tool, would
        # fail to answer. route="wiki" forces pure wiki grounding (the structured gate
        # is unaffected; this only selects the backend).
        body: dict[str, Any] = {"question": question, "scope": scope, "route": "wiki"}
        if context_wiki_slug:
            body["context_wiki_slug"] = context_wiki_slug
        return self._request("POST", "/ask", json=body)

    def structured(self, question: str, *, scope: str, project: str | None) -> dict:
        body: dict[str, Any] = {"question": question, "scope": scope}
        if project:
            body["project"] = project
        return self._request("POST", "/ask/structured", json=body)

    def team_schedule(self, since: str | None, until: str | None) -> Any:
        params: dict[str, Any] = {}
        if since:
            params["from"] = since
        if until:
            params["to"] = until
        return self._request("GET", "/dashboard/calendar", params=params)

    def team_schedule_add(self, payload: dict[str, Any]) -> dict:
        return self._request("POST", "/dashboard/calendar/events", json=payload)

    def team_schedule_update(self, event_id: str, payload: dict[str, Any]) -> dict:
        return self._request("PATCH", f"/dashboard/calendar/events/{event_id}", json=payload)

    def team_schedule_delete(self, event_id: str) -> dict:
        self._request("DELETE", f"/dashboard/calendar/events/{event_id}")
        return {"deleted": event_id}

    def team_members(self) -> Any:
        return self._request("GET", "/dashboard/team-members")

    def team_members_add(self, payload: dict[str, Any]) -> dict:
        return self._request("POST", "/dashboard/team-members", json=payload)

    def board(self) -> dict:
        return self._request("GET", "/board")

    def projects(self) -> dict:
        return self._request("GET", "/projects")

    def personal_schedule_list(self, since: str | None, until: str | None) -> Any:
        params: dict[str, Any] = {}
        if since:
            params["from"] = since
        if until:
            params["to"] = until
        return self._request("GET", "/personal-board/fixed-events", params=params)

    def personal_schedule_add(self, payload: dict[str, Any]) -> dict:
        return self._request("POST", "/personal-board/fixed-events", json=payload)

    def personal_schedule_update(self, event_id: str, payload: dict[str, Any]) -> dict:
        return self._request("PATCH", f"/personal-board/fixed-events/{event_id}", json=payload)

    def board_tasks_list(
        self,
        *,
        status: str | None = None,
        project_id: str | None = None,
        id_prefix: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
    ) -> Any:
        params: dict[str, Any] = {"limit": limit}
        for key, value in (
            ("status", status),
            ("project_id", project_id),
            ("id_prefix", id_prefix),
            ("date_from", date_from),
            ("date_to", date_to),
        ):
            if value is not None:
                params[key] = value
        return self._request("GET", "/personal-board/tasks", params=params)

    def board_task_create(self, payload: dict[str, Any]) -> dict:
        return self._request("POST", "/personal-board/tasks", json=payload)

    def board_task_update(self, task_id: str, payload: dict[str, Any]) -> dict:
        return self._request("PATCH", f"/personal-board/tasks/{task_id}", json=payload)

    def board_task_comments(self, task_id: str) -> Any:
        return self._request("GET", f"/personal-board/tasks/{task_id}/comments")

    def board_task_comment_add(self, task_id: str, body: str) -> dict:
        return self._request(
            "POST", f"/personal-board/tasks/{task_id}/comments", json={"body": body}
        )

    def board_projects(self) -> Any:
        return self._request("GET", "/personal-board/projects")

    def project_board_tasks(self, project_id: str) -> Any:
        return self._request("GET", f"/dashboard/projects/{project_id}/board-tasks")

    def project_databases(self, project_id: str) -> Any:
        return self._request("GET", f"/dashboard/projects/{project_id}/databases")

    def database_bundle(self, database_id: str) -> Any:
        return self._request("GET", f"/dashboard/databases/{database_id}", params={"body": "none"})

    def database_row_get(self, database_id: str, row_id: str) -> dict:
        return self._request("GET", f"/dashboard/databases/{database_id}/rows/{row_id}")

    def database_row_create(self, database_id: str, payload: dict[str, Any]) -> dict:
        return self._request("POST", f"/dashboard/databases/{database_id}/rows", json=payload)

    def database_row_update(self, database_id: str, row_id: str, payload: dict[str, Any]) -> dict:
        return self._request(
            "PATCH", f"/dashboard/databases/{database_id}/rows/{row_id}", json=payload
        )

    def board_backlog_buckets(self) -> Any:
        return self._request("GET", "/personal-board/backlog-buckets")

    def wiki_update_candidate(self, slug: str, note: str, evidence_urls: list[str]) -> dict:
        return self._request(
            "POST",
            "/wiki/tasks",
            json={"slug": slug, "note": note, "evidence_urls": evidence_urls},
        )

    def agent_work_list(self, *, state: str | None, limit: int) -> list:
        params: dict[str, Any] = {}
        if state:
            params["state"] = state
        # /agent-work has no limit query param; slice client-side.
        result = self._request("GET", "/agent-work", params=params)
        items = result or []
        return items[:limit]

    def agent_work_get(self, work_id: str) -> dict:
        return self._request("GET", f"/agent-work/{work_id}")

    def whoami(self) -> dict:
        return self._request("GET", "/collector/whoami")

    def connector_list(self) -> dict:
        return self._request("GET", "/collector/connectors")

    def connector_config(
        self,
        slug: str,
        *,
        settings: dict[str, Any],
        secrets: dict[str, str],
        label: str | None,
    ) -> dict:
        body: dict[str, Any] = {"settings": settings, "secrets": secrets}
        if label is not None:
            body["account_label"] = label
        return self._request("PUT", f"/collector/connectors/{slug}/config", json=body)

    def connector_ensure(self, slug: str) -> dict:
        return self._request("POST", f"/collector/connectors/{slug}/ensure")

    def connector_delete(self, slug: str, account_id: str) -> dict:
        return self._request("DELETE", f"/collector/connectors/{slug}/accounts/{account_id}")

    def kg_query(
        self,
        *,
        relation: str,
        slug: str | None = None,
        slug_b: str | None = None,
        name: str | None = None,
        depth: int = 1,
        max_hops: int = 4,
    ) -> dict:
        body: dict[str, Any] = {"relation": relation, "depth": depth, "max_hops": max_hops}
        if slug:
            body["slug"] = slug
        if slug_b:
            body["slug_b"] = slug_b
        if name:
            body["name"] = name
        return self._request("POST", "/wiki/kg/query", json=body)

    def mail_inbox(self, *, limit: int, search: str | None = None) -> dict:
        """P10 mail read tool: the caller's own unified company inbox (dual-auth
        read route; the server binds rows to the token's owner)."""
        params: dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        return self._request("GET", "/mail/inbox", params=params)

    def mail_message(
        self, *, backend: str, external_id: str, account_id: str | None = None
    ) -> dict:
        params: dict[str, Any] = {"backend": backend, "id": external_id}
        if account_id:
            params["account_id"] = account_id
        return self._request("GET", "/mail/message", params=params)

    def inbox_summary(self) -> dict:
        return self._request("GET", "/agent-work/inbox-summary")

    def data_gaps(self, slug: str | None = None) -> Any:
        # slug 없으면 노드 스코프 전체 open 백로그, 있으면 그 위키 페이지에 묶인 갭만.
        if slug:
            return self._request("GET", f"/wiki/pages/{slug}/data-gaps")
        return self._request("GET", "/gaps", params={"status": "open"})
