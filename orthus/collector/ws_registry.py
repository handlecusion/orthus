"""In-process registry of connected collector-daemon WebSockets (slice 8).

central is a single-worker uvicorn process, so a plain module-level singleton of
``(node_id, user_id) -> set[asyncio.Queue]`` is enough to fan a "command
available" notify out to the daemons of one owner. ``create_command`` runs in a
threadpool (sync DB), not on the event loop, so ``notify`` is cross-thread: it
hands each queue ``put_nowait`` to the captured loop via
``loop.call_soon_threadsafe``.

Deliberately pure asyncio with no DB / settings imports so it stays free of
circular imports (``orthus.collector.commands`` imports it). Fail-closed: when the
loop has not been captured (lifespan never ran) or no daemon of that owner is
connected, ``notify`` is a silent no-op so it can never break enqueue.

Multi-worker note: a module singleton only reaches daemons connected to THIS
worker. uvicorn defaults to one worker; if central ever scales to multiple
workers, replace the in-process fan-out with Postgres ``LISTEN``/``NOTIFY`` so
any worker's enqueue reaches the worker holding the daemon socket. Out of scope
for this slice.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from uuid import UUID


# A daemon poll-drains all pending rows per notify, so a notify is only an
# idempotent nudge. Bound the per-connection queue and drop on overflow: a
# stalled/slow daemon can never grow the queue without limit, and it still
# catches up via its next drain / safety poll.
_QUEUE_MAXSIZE = 256


class CollectorWsRegistry:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: dict[tuple[str, UUID], set[asyncio.Queue]] = defaultdict(set)
        # Per-owner connected-device counts (best-effort, in-process single
        # worker). Keyed by (user_id, device_id) so delegate-targets can mark a
        # node "realtime" when its device's daemon holds a live socket. A legacy
        # deviceless token uses "" and is tracked under that empty key.
        self._device_counts: dict[tuple[UUID, str], int] = defaultdict(int)

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the running event loop at app startup (lifespan)."""
        self._loop = loop

    def clear_loop(self) -> None:
        """Drop the captured loop at shutdown so a stale loop is never used."""
        self._loop = None

    def register(self, node_id: str, user_id: UUID, device_id: str = "") -> asyncio.Queue:
        """Create a per-connection queue and track it under (node, owner).

        Also bumps the connected-device count for (owner, device_id) so
        delegate-targets can report which devices have a live socket. The notify
        fan-out is unchanged — it still reaches every daemon of the owner.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._queues[(node_id, user_id)].add(queue)
        self._device_counts[(user_id, device_id)] += 1
        return queue

    def unregister(
        self, node_id: str, user_id: UUID, queue: asyncio.Queue, device_id: str = ""
    ) -> None:
        """Remove one connection's queue; drop the owner key when empty."""
        key = (node_id, user_id)
        conns = self._queues.get(key)
        if conns is not None:
            conns.discard(queue)
            if not conns:
                self._queues.pop(key, None)
        device_key = (user_id, device_id)
        count = self._device_counts.get(device_key, 0)
        if count <= 1:
            self._device_counts.pop(device_key, None)
        else:
            self._device_counts[device_key] = count - 1

    def connected_device_ids(self, user_id: UUID) -> frozenset[str]:
        """Return the device_ids of the owner with a live socket right now.

        Best-effort, in-process single-worker view (a daemon connected to
        another worker is not seen). Includes "" for legacy deviceless tokens.
        """
        return frozenset(
            device_id
            for (uid, device_id), count in self._device_counts.items()
            if uid == user_id and count > 0
        )

    def notify(self, node_id: str, user_id: UUID, message: dict) -> None:
        """Thread-safe push of ``message`` to every connection of one owner.

        No-op when the loop is not captured or no connection matches. Exceptions
        are swallowed so a notify failure can never break the enqueue path that
        calls this from a threadpool.
        """
        loop = self._loop
        if loop is None:
            return
        conns = self._queues.get((node_id, user_id))
        if not conns:
            return
        for queue in tuple(conns):
            try:
                loop.call_soon_threadsafe(self._safe_put, queue, message)
            except Exception:  # noqa: BLE001 - notify must never break enqueue
                continue

    @staticmethod
    def _safe_put(queue: asyncio.Queue, message: dict) -> None:
        """Enqueue on the loop thread; drop on a full bounded queue.

        Scheduled via call_soon_threadsafe so put_nowait runs on the event loop.
        A full queue means a stalled daemon; dropping the nudge is safe because
        poll_commands drains all pending rows, so its next drain catches up.
        """
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            return


WS_REGISTRY = CollectorWsRegistry()
