"""Owner-scoped P8 collector rate limits.

In-process guard for the single central runtime. A multi-process deployment
must move the same owner-keyed buckets to a shared store.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    reason: str = ""
    retry_after_seconds: int = 0


class OwnerWindowLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[UUID, deque[tuple[float, int, int]]] = {}

    def check(
        self,
        owner_id: UUID,
        *,
        count: int,
        bytes_count: int = 0,
        count_limit: int = 0,
        bytes_limit: int = 0,
        window_seconds: int,
        count_reason: str = "count",
        now: float | None = None,
    ) -> RateLimitResult:
        if window_seconds <= 0:
            return RateLimitResult(allowed=True)
        now = time.monotonic() if now is None else now
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._hits.setdefault(owner_id, deque())
            while bucket and bucket[0][0] <= cutoff:
                bucket.popleft()
            current_count = sum(item_count for _, item_count, _ in bucket)
            current_bytes = sum(item_bytes for _, _, item_bytes in bucket)
            retry_after = self._retry_after(bucket, now, window_seconds)
            if count_limit > 0 and current_count + count > count_limit:
                return RateLimitResult(
                    allowed=False,
                    reason=count_reason,
                    retry_after_seconds=retry_after,
                )
            if bytes_limit > 0 and current_bytes + bytes_count > bytes_limit:
                return RateLimitResult(
                    allowed=False,
                    reason="bytes",
                    retry_after_seconds=retry_after,
                )
            bucket.append((now, count, bytes_count))
            return RateLimitResult(allowed=True)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    @staticmethod
    def _retry_after(bucket: deque[tuple[float, int, int]], now: float, window_seconds: int) -> int:
        if not bucket:
            return max(1, window_seconds)
        return max(1, int(bucket[0][0] + window_seconds - now) + 1)


ingest_limiter = OwnerWindowLimiter()
compile_limiter = OwnerWindowLimiter()


def reset_collector_owner_rate_limits() -> None:
    ingest_limiter.reset()
    compile_limiter.reset()
