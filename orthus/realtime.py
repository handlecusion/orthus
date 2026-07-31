"""실시간 협업용 in-process pub/sub(SSE 백엔드).

누군가 계획·회고를 저장하면 publish()로 변경 이벤트를 쏘고, SSE 구독자(다른
브라우저)는 즉시 받아 그 자리에서 갱신한다(6초 폴링 대신 즉시 반영). 저장 핸들러는
스레드풀에서 도는 sync 함수라, 이벤트 루프로 안전하게 넘기기 위해
loop.call_soon_threadsafe를 쓴다. 단일 프로세스 dev용이며 멀티워커/노드 확장 시
Redis 등 공유 브로커로 교체한다.
"""

from __future__ import annotations

import asyncio

# SSE 구독자 큐 집합과, sync→async 전달용 이벤트 루프 핸들.
_subscribers: set[asyncio.Queue] = set()
_loop: asyncio.AbstractEventLoop | None = None


def register_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def publish(event: dict) -> None:
    """저장 핸들러(sync/threadpool)에서 호출. 이벤트 루프로 안전하게 넘긴다."""
    loop = _loop
    if loop is None:
        return

    def _deliver() -> None:
        for q in list(_subscribers):
            try:
                q.put_nowait(event)
            except Exception:
                pass

    try:
        loop.call_soon_threadsafe(_deliver)
    except RuntimeError:
        pass
