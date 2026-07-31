"""Unit tests for the in-process collector WebSocket notify registry (slice 8)."""

from __future__ import annotations

import asyncio
import uuid

from orthus.collector.ws_registry import CollectorWsRegistry


def test_notify_delivers_to_matching_owner_queue():
    async def run() -> None:
        registry = CollectorWsRegistry()
        registry.set_loop(asyncio.get_running_loop())
        user_id = uuid.uuid4()
        queue = registry.register("company", user_id)

        message = {"type": "command_available", "command_id": "c1", "kind": "agent_task"}
        registry.notify("company", user_id, message)
        # call_soon_threadsafe schedules on the loop; yield so it runs.
        delivered = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert delivered == message

    asyncio.run(run())


def test_notify_fans_out_to_all_connections_of_one_owner():
    async def run() -> None:
        registry = CollectorWsRegistry()
        registry.set_loop(asyncio.get_running_loop())
        user_id = uuid.uuid4()
        q1 = registry.register("company", user_id)
        q2 = registry.register("company", user_id)

        registry.notify("company", user_id, {"type": "command_available"})
        first = await asyncio.wait_for(q1.get(), timeout=1.0)
        second = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert first == second == {"type": "command_available"}

    asyncio.run(run())


def test_notify_does_not_cross_owner_boundary():
    async def run() -> None:
        registry = CollectorWsRegistry()
        registry.set_loop(asyncio.get_running_loop())
        owner_a = uuid.uuid4()
        owner_b = uuid.uuid4()
        q_a = registry.register("company", owner_a)
        q_b = registry.register("company", owner_b)

        registry.notify("company", owner_a, {"command_id": "for-a"})
        delivered = await asyncio.wait_for(q_a.get(), timeout=1.0)
        assert delivered == {"command_id": "for-a"}
        # owner_b's queue stays empty.
        assert q_b.empty()

    asyncio.run(run())


def test_notify_does_not_cross_node_boundary():
    async def run() -> None:
        registry = CollectorWsRegistry()
        registry.set_loop(asyncio.get_running_loop())
        user_id = uuid.uuid4()
        company_q = registry.register("company", user_id)
        personal_q = registry.register("personal-a", user_id)

        registry.notify("company", user_id, {"command_id": "company-only"})
        delivered = await asyncio.wait_for(company_q.get(), timeout=1.0)
        assert delivered == {"command_id": "company-only"}
        assert personal_q.empty()

    asyncio.run(run())


def test_notify_is_noop_when_loop_unset():
    registry = CollectorWsRegistry()
    user_id = uuid.uuid4()
    queue = registry.register("company", user_id)
    # No loop captured -> notify silently no-ops; nothing is enqueued.
    registry.notify("company", user_id, {"command_id": "x"})
    assert queue.empty()


def test_notify_is_noop_when_no_matching_connection():
    async def run() -> None:
        registry = CollectorWsRegistry()
        registry.set_loop(asyncio.get_running_loop())
        # No register call for this owner; notify must not raise.
        registry.notify("company", uuid.uuid4(), {"command_id": "x"})
        # Give the loop a tick; nothing should have been scheduled.
        await asyncio.sleep(0)

    asyncio.run(run())


def test_unregister_removes_connection_and_drops_empty_owner_key():
    async def run() -> None:
        registry = CollectorWsRegistry()
        registry.set_loop(asyncio.get_running_loop())
        user_id = uuid.uuid4()
        queue = registry.register("company", user_id)
        registry.unregister("company", user_id, queue)
        # After the only connection is removed, a notify is a no-op.
        registry.notify("company", user_id, {"command_id": "x"})
        await asyncio.sleep(0)
        assert queue.empty()
        assert ("company", user_id) not in registry._queues

    asyncio.run(run())


def test_unregister_unknown_queue_is_safe():
    registry = CollectorWsRegistry()
    # Unregistering a queue that was never registered must not raise.
    registry.unregister("company", uuid.uuid4(), asyncio.Queue())


def test_connected_device_ids_tracks_registered_devices():
    registry = CollectorWsRegistry()
    user_id = uuid.uuid4()
    q_a = registry.register("company", user_id, "device-a")
    q_b = registry.register("company", user_id, "device-b")
    assert registry.connected_device_ids(user_id) == frozenset({"device-a", "device-b"})
    # Another owner's devices are not reported.
    assert registry.connected_device_ids(uuid.uuid4()) == frozenset()

    registry.unregister("company", user_id, q_a, "device-a")
    assert registry.connected_device_ids(user_id) == frozenset({"device-b"})
    registry.unregister("company", user_id, q_b, "device-b")
    assert registry.connected_device_ids(user_id) == frozenset()


def test_connected_device_ids_counts_duplicate_device_connections():
    registry = CollectorWsRegistry()
    user_id = uuid.uuid4()
    q1 = registry.register("company", user_id, "device-a")
    q2 = registry.register("company", user_id, "device-a")
    assert registry.connected_device_ids(user_id) == frozenset({"device-a"})
    # One of two connections closes -> the device is still connected.
    registry.unregister("company", user_id, q1, "device-a")
    assert registry.connected_device_ids(user_id) == frozenset({"device-a"})
    registry.unregister("company", user_id, q2, "device-a")
    assert registry.connected_device_ids(user_id) == frozenset()


def test_connected_device_ids_includes_legacy_deviceless():
    registry = CollectorWsRegistry()
    user_id = uuid.uuid4()
    registry.register("company", user_id)  # device_id defaults to ""
    assert registry.connected_device_ids(user_id) == frozenset({""})
