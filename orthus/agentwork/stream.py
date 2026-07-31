"""work_id-keyed in-process pub/sub for live agent_task output (SSE backend).

The collector WS reader publishes ``agent_output`` / ``turn_complete`` frames here
keyed by ``work_item_id``; the per-work SSE endpoint subscribes for one work_id and
streams them to the owner's browser as the dispatched agent runs. A small bounded
ring per work_id lets a late-joining browser (it opens the SSE a beat after the
delegation is created) replay the lines already emitted.

Single-process by design — it mirrors ``orthus.collector.ws_registry``: central runs
one uvicorn worker, so a module-level dict is enough to fan one owner's frames to
their open SSE connection. If central ever scales to multiple workers, replace this
with a shared broker (Redis pub/sub or Postgres LISTEN/NOTIFY) so a frame received
on the worker holding the daemon socket reaches the worker holding the browser SSE.

Best-effort feedback ONLY. The authoritative result still flows through the HTTP
``complete_command`` -> ``resolve_agent_task_work_item_result`` path, so a dropped
frame, a restart, or no subscriber never affects correctness — it only affects how
live the in-flight view looks.

``publish`` is always called from the event-loop thread (the collector WS reader is
an async coroutine), so it touches the subscriber queues directly without
``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque

# Recent frames retained per work_id for late-join replay. Bounded so a chatty agent
# can never grow a ring without limit; the oldest lines roll off.
_RING_MAX = 400
# How long a completed work_id's ring lingers so a browser that connects right at
# completion still replays the transcript, before it is dropped to free memory.
_RING_TTL_SECONDS = 60.0

_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
_rings: dict[str, deque] = {}

# The running event loop, captured at API lifespan startup. The collector WS reader
# publishes from the loop thread (so it calls `publish` directly), but the inline
# agentic /ask loop runs in FastAPI's sync-endpoint threadpool — it must hand frames
# back to the loop via `publish_threadsafe`. None until lifespan sets it (then
# `publish_threadsafe` is a best-effort no-op, matching the feedback-only contract).
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the running loop at lifespan startup (see orthus/api/main.py)."""
    global _loop
    _loop = loop


def clear_loop() -> None:
    global _loop
    _loop = None


def publish_threadsafe(work_id: str, frame: dict) -> None:
    """Publish from a non-loop thread (the sync-endpoint threadpool).

    `asyncio.Queue` is not thread-safe, so the actual `publish` is marshalled onto
    the captured loop via `call_soon_threadsafe`. No captured loop -> silent no-op
    (streaming is feedback-only and must never raise into the request handler)."""
    loop = _loop
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(publish, work_id, frame)
    except RuntimeError:
        # Loop closed/closing during shutdown — drop the frame, never raise.
        pass


def recent(work_id: str) -> list[dict]:
    """Return the retained recent frames for a work_id (replay on SSE connect)."""
    return list(_rings.get(work_id, ()))


def subscribe(work_id: str) -> asyncio.Queue:
    """Register a per-connection queue for one work_id's live frames."""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers[work_id].add(q)
    return q


def unsubscribe(work_id: str, q: asyncio.Queue) -> None:
    """Remove one connection's queue; drop the work_id key when empty."""
    conns = _subscribers.get(work_id)
    if conns is None:
        return
    conns.discard(q)
    if not conns:
        _subscribers.pop(work_id, None)


def publish(work_id: str, frame: dict) -> None:
    """Append a frame to the work_id ring and fan it to live subscribers.

    Must be called from the event-loop thread. A full/closed subscriber queue is
    skipped silently — streaming is feedback-only and must never raise into the
    collector WS reader.
    """
    ring = _rings.get(work_id)
    if ring is None:
        ring = deque(maxlen=_RING_MAX)
        _rings[work_id] = ring
    ring.append(frame)

    for q in tuple(_subscribers.get(work_id, ())):
        try:
            q.put_nowait(frame)
        except Exception:  # noqa: BLE001 - a bad subscriber must not break the fan-out
            continue

    if frame.get("type") == "turn_complete":
        try:
            asyncio.get_running_loop().call_later(_RING_TTL_SECONDS, _drop_ring, work_id)
        except RuntimeError:
            # No running loop (shouldn't happen on the WS reader path); drop now.
            _rings.pop(work_id, None)


def _drop_ring(work_id: str) -> None:
    _rings.pop(work_id, None)
