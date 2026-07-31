"""CentralClient HTTPS request shaping (no network, no Postgres).

Lives outside test_mcp_server.py on purpose: that module is gated by
`pytest.importorskip("mcp")`, and CI installs only the `dev` extra (not `mcp`),
so it is skipped in CI. `orthus.mcp.central` imports without the `mcp` SDK
(httpx only), so these request-body assertions run in CI.
"""

from __future__ import annotations

import json

import httpx
import pytest

from orthus.mcp import central
from orthus.mcp.central import CentralClient, CentralConfig, CentralError


def _capture(calls: list[dict]):
    """A MockTransport handler that records method/path/json-body of each request."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "params": dict(request.url.params),
                "body": json.loads(request.content.decode() or "{}") if request.content else {},
            }
        )
        return httpx.Response(200, json={"mode": "wiki"})

    return handler


def _run(client: CentralClient, transport: httpx.MockTransport, fn) -> None:
    """Run `fn(client)` with the per-request httpx.Client pinned to `transport`."""
    real_client = httpx.Client

    def _patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    httpx.Client = _patched  # type: ignore[assignment]
    try:
        fn(client)
    finally:
        httpx.Client = real_client  # type: ignore[assignment]


def test_wiki_ask_pins_wiki_route():
    """wiki_ask pins route='wiki' in the POST /ask body so central never auto-routes a
    knowledge question to the structured NL→SQL backend (which would run the wrong SQL
    or gate-reject 422). context_wiki_slug still rides along when provided; route stays
    pinned regardless."""
    calls: list[dict] = []
    client = CentralClient(CentralConfig(base_url="https://central.example", token="dct_x"))
    transport = httpx.MockTransport(_capture(calls))

    def _do(c: CentralClient) -> None:
        c.wiki_ask("회사가 하는 주요 서비스 3가지", scope="company", context_wiki_slug=None)
        c.wiki_ask("문서 요약", scope="all", context_wiki_slug="회사-개요")

    _run(client, transport, _do)

    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/ask"
    assert calls[0]["body"] == {
        "question": "회사가 하는 주요 서비스 3가지",
        "scope": "company",
        "route": "wiki",
    }
    # context_wiki_slug rides along and route stays pinned to wiki.
    assert calls[1]["body"]["route"] == "wiki"
    assert calls[1]["body"]["context_wiki_slug"] == "회사-개요"


def test_structured_tool_does_not_force_route():
    """The `structured` tool hits its own /ask/structured endpoint and must NOT carry a
    `route` field — route forcing is a wiki_ask concern only (pins the boundary so the
    change stays scoped)."""
    calls: list[dict] = []
    client = CentralClient(CentralConfig(base_url="https://central.example", token="dct_x"))
    transport = httpx.MockTransport(_capture(calls))

    _run(client, transport, lambda c: c.structured("티켓 개수", scope="company", project=None))

    assert calls[0]["method"] == "POST"
    assert calls[0]["path"] == "/ask/structured"
    assert "route" not in calls[0]["body"]


def test_read_retries_transient_530_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(530, text="upstream unavailable")
        return httpx.Response(200, json=[{"task_id": "task-1"}])

    monkeypatch.setattr(central.time, "sleep", sleeps.append)
    client = CentralClient(CentralConfig(base_url="https://central.example", token="dct_x"))
    transport = httpx.MockTransport(handler)
    result: list[dict] = []

    _run(client, transport, lambda c: result.extend(c.board_tasks_list(limit=1)))

    assert attempts == 3
    assert sleeps == list(central.READ_RETRY_DELAYS_SECONDS)
    assert result == [{"task_id": "task-1"}]


def test_write_does_not_retry_transient_530(monkeypatch: pytest.MonkeyPatch):
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(530, text="upstream unavailable")

    monkeypatch.setattr(central.time, "sleep", sleeps.append)
    client = CentralClient(CentralConfig(base_url="https://central.example", token="dct_x"))
    transport = httpx.MockTransport(handler)

    with pytest.raises(CentralError, match="temporarily unavailable") as exc_info:
        _run(client, transport, lambda c: c.board_task_create({"title": "no duplicate"}))

    assert exc_info.value.status == 530
    assert attempts == 1
    assert sleeps == []


def test_read_reports_persistent_transient_error_after_retries(
    monkeypatch: pytest.MonkeyPatch,
):
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="restarting")

    monkeypatch.setattr(central.time, "sleep", lambda _delay: None)
    client = CentralClient(CentralConfig(base_url="https://central.example", token="dct_x"))
    transport = httpx.MockTransport(handler)

    with pytest.raises(CentralError, match=r"temporarily unavailable \(HTTP 503") as exc_info:
        _run(client, transport, lambda c: c.board_tasks_list(limit=1))

    assert exc_info.value.status == 503
    assert attempts == 3


def test_read_retries_transport_error_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("tunnel handoff", request=request)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(central.time, "sleep", sleeps.append)
    client = CentralClient(CentralConfig(base_url="https://central.example", token="dct_x"))
    transport = httpx.MockTransport(handler)

    _run(client, transport, lambda c: c.board_tasks_list(limit=1))

    assert attempts == 2
    assert sleeps == [central.READ_RETRY_DELAYS_SECONDS[0]]
