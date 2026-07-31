"""Unit tests for the work_id-keyed agent-output pub/sub (SSE backend)."""

from __future__ import annotations

import asyncio

from orthus.agentwork import stream as agent_stream


def _out(work_id: str, chunk: str) -> dict:
    return {"type": "agent_output", "work_item_id": work_id, "chunk": chunk}


def test_subscribe_receives_published_frame():
    async def run():
        q = agent_stream.subscribe("w1")
        agent_stream.publish("w1", _out("w1", "hello"))
        frame = await asyncio.wait_for(q.get(), timeout=1)
        agent_stream.unsubscribe("w1", q)
        return frame

    frame = asyncio.run(run())
    assert frame["chunk"] == "hello"


def test_multiple_subscribers_each_receive():
    async def run():
        a = agent_stream.subscribe("w2")
        b = agent_stream.subscribe("w2")
        agent_stream.publish("w2", _out("w2", "x"))
        fa = await asyncio.wait_for(a.get(), timeout=1)
        fb = await asyncio.wait_for(b.get(), timeout=1)
        agent_stream.unsubscribe("w2", a)
        agent_stream.unsubscribe("w2", b)
        return fa, fb

    fa, fb = asyncio.run(run())
    assert fa["chunk"] == "x" and fb["chunk"] == "x"


def test_recent_replays_backlog_for_late_subscriber():
    async def run():
        agent_stream.publish("w3", _out("w3", "one"))
        agent_stream.publish("w3", _out("w3", "two"))
        # A browser that connects late replays via recent() before subscribing live.
        return agent_stream.recent("w3")

    backlog = asyncio.run(run())
    assert [f["chunk"] for f in backlog] == ["one", "two"]


def test_unsubscribe_drops_key_and_publish_is_safe():
    async def run():
        q = agent_stream.subscribe("w4")
        agent_stream.unsubscribe("w4", q)
        # No subscribers left: publish must not raise.
        agent_stream.publish("w4", _out("w4", "ignored"))

    asyncio.run(run())  # no exception == pass


def test_turn_complete_schedules_ring_drop():
    async def run():
        agent_stream.publish("w5", _out("w5", "progress"))
        agent_stream.publish(
            "w5", {"type": "turn_complete", "work_item_id": "w5", "status": "done"}
        )
        # Immediately after completion the ring is still replayable (TTL not elapsed).
        return agent_stream.recent("w5")

    backlog = asyncio.run(run())
    assert backlog[-1]["type"] == "turn_complete"
