"""Unit tests for the central WS reader's agent-output fan-out + fail-closed gate.

`_ws_reader` drains inbound frames and, when streaming is enabled, routes
`agent_output`/`turn_complete` frames to the per-work pub/sub by work_item_id. When
streaming is off it ignores frames entirely (fail-closed slice-8 drain-only reader).
"""

from __future__ import annotations

import asyncio
import json

from fastapi import WebSocketDisconnect

from orthus.agentwork import stream as agent_stream
from orthus.api.routes import collector as collector_routes
from orthus.settings import Settings


class _FakeWS:
    def __init__(self, frames: list[str]):
        self._frames = list(frames)

    async def receive_text(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        raise WebSocketDisconnect()


def _run_reader(monkeypatch, *, streaming: bool, frames: list[str]) -> list[tuple[str, dict]]:
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        collector_routes, "get_settings", lambda: Settings(agent_task_streaming_enabled=streaming)
    )
    monkeypatch.setattr(
        agent_stream, "publish", lambda work_id, frame: published.append((work_id, frame))
    )

    async def run():
        try:
            await collector_routes._ws_reader(_FakeWS(frames))
        except WebSocketDisconnect:
            pass

    asyncio.run(run())
    return published


def test_streaming_on_routes_agent_output_and_turn_complete(monkeypatch):
    frames = [
        json.dumps({"type": "agent_output", "work_item_id": "w1", "chunk": "a"}),
        json.dumps({"type": "turn_complete", "work_item_id": "w1", "status": "done"}),
    ]
    published = _run_reader(monkeypatch, streaming=True, frames=frames)
    assert [p[0] for p in published] == ["w1", "w1"]
    assert published[0][1]["chunk"] == "a"
    assert published[1][1]["type"] == "turn_complete"


def test_streaming_on_ignores_other_types_and_bad_json(monkeypatch):
    frames = [
        "{not json",
        json.dumps({"type": "ping"}),
        json.dumps({"type": "agent_output"}),  # missing work_item_id
        json.dumps(["not", "a", "dict"]),
    ]
    published = _run_reader(monkeypatch, streaming=True, frames=frames)
    assert published == []


def test_streaming_off_publishes_nothing(monkeypatch):
    frames = [json.dumps({"type": "agent_output", "work_item_id": "w1", "chunk": "a"})]
    published = _run_reader(monkeypatch, streaming=False, frames=frames)
    assert published == []
