"""P6.7.2 pull-ingest pagination (`docs/p6-unified-mail.md` §12.3-4).

The previous `_list_inbox` did a single `offset=0` read, silently dropping every
message past the first page. `_list_inbox` now pages until a short page or the
`_PULL_MAX_TOTAL` cap is reached. Idempotency is unchanged (callers de-dup).
"""

from __future__ import annotations

import httpx

from orthus.mail.backends import MailBackendConfig
from orthus.mail.pull import _PULL_MAX_TOTAL, _list_inbox


def _cfg() -> MailBackendConfig:
    return MailBackendConfig(
        backend="nova",
        base_url="https://api.nova.example/v0",
        owner="owner@nova.example",
        bearer_token="nova-key",
    )


def _paged_transport(total: int, page_size: int) -> tuple[httpx.MockTransport, list[dict]]:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        limit = int(request.url.params.get("limit", str(page_size)))
        calls.append({"offset": offset, "limit": limit})
        window = [
            {"id": f"m{i}", "from_addr": "x@nova.example"}
            for i in range(offset, min(offset + limit, total))
        ]
        return httpx.Response(200, json={"emails": window, "total": total})

    return httpx.MockTransport(handler), calls


def test_list_inbox_pages_past_first_page():
    transport, calls = _paged_transport(total=130, page_size=50)
    with httpx.Client(transport=transport) as client:
        rows = _list_inbox(_cfg(), client, limit=50)

    # All 130 messages fetched, not just the first 50.
    assert len(rows) == 130
    assert {row["id"] for row in rows} == {f"m{i}" for i in range(130)}
    # Paged at 0, 50, 100 (the 100-page returns 30 < 50 -> stop).
    assert [c["offset"] for c in calls] == [0, 50, 100]


def test_list_inbox_single_short_page_stops():
    transport, calls = _paged_transport(total=20, page_size=50)
    with httpx.Client(transport=transport) as client:
        rows = _list_inbox(_cfg(), client, limit=50)

    assert len(rows) == 20
    assert len(calls) == 1


def test_list_inbox_respects_total_cap():
    # Exactly-full pages would loop forever without the cap; assert we stop at cap.
    transport, calls = _paged_transport(total=10_000, page_size=200)
    with httpx.Client(transport=transport) as client:
        rows = _list_inbox(_cfg(), client, limit=200)

    assert len(rows) == _PULL_MAX_TOTAL
    # Fetched ceil(500/200) = 3 pages, then the cap stops the loop.
    assert len(calls) == _PULL_MAX_TOTAL // 200 + (1 if _PULL_MAX_TOTAL % 200 else 0)
