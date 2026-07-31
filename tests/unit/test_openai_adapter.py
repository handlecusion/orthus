"""OpenAI-compatible adapter retry/backoff (429 / 5xx / Retry-After)."""

import httpx
import pytest

from orthus.models.adapters import openai_compat


def test_retry_after_prefers_header():
    resp = httpx.Response(429, headers={"retry-after": "2"})
    # Header wins over the exponential backoff base.
    assert openai_compat._retry_after_seconds(resp, attempt=5) == 2.0


def test_retry_after_caps_header():
    resp = httpx.Response(429, headers={"retry-after": "9999"})
    assert openai_compat._retry_after_seconds(resp, attempt=0) == openai_compat._BACKOFF_CAP


def test_retry_after_backoff_grows_and_is_bounded():
    # No header → exponential backoff + jitter, never above the cap.
    d0 = openai_compat._retry_after_seconds(None, attempt=0)
    d_big = openai_compat._retry_after_seconds(None, attempt=10)
    assert d0 >= openai_compat._BACKOFF_BASE
    assert d_big <= openai_compat._BACKOFF_CAP * 1.25


def test_post_json_retries_429_then_succeeds(monkeypatch):
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "rate"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client  # capture before patching to avoid recursion
    monkeypatch.setattr(
        openai_compat.httpx,
        "Client",
        lambda *a, **k: real_client(transport=transport),
    )
    monkeypatch.setattr(openai_compat.time, "sleep", lambda s: slept.append(s))

    out = openai_compat._post_json("https://api.test", "/x", "key", {"a": 1}, 5.0)
    assert out == {"ok": True}
    assert calls["n"] == 2  # one 429, one success
    assert len(slept) == 1  # backed off exactly once


def test_post_json_raises_after_exhausting_retries(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client  # capture before patching to avoid recursion
    monkeypatch.setattr(
        openai_compat.httpx,
        "Client",
        lambda *a, **k: real_client(transport=transport),
    )
    monkeypatch.setattr(openai_compat.time, "sleep", lambda s: None)

    with pytest.raises(httpx.HTTPStatusError):
        openai_compat._post_json("https://api.test", "/x", "key", {"a": 1}, 5.0)


def test_post_json_insufficient_quota_fails_without_retry(monkeypatch):
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "You exceeded your current quota",
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                }
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client  # capture before patching to avoid recursion
    monkeypatch.setattr(
        openai_compat.httpx,
        "Client",
        lambda *a, **k: real_client(transport=transport),
    )
    monkeypatch.setattr(openai_compat.time, "sleep", lambda s: slept.append(s))

    with pytest.raises(httpx.HTTPStatusError):
        openai_compat._post_json("https://api.test", "/x", "key", {"a": 1}, 5.0)

    assert calls["n"] == 1
    assert slept == []
