"""The EdgeBoxExecutor (edge island, Phase 2, Task 10, svitgrid#1230).

An inverter read by an adopted edge box has no local write path: its commands
go into the edge command queue, the box drains them on its next poll and acks
them. The executor waits for that ack and answers like any other executor, so
the scheduler's `_reason_for` and the commands view need no edge branch. The
phone's signature travels untouched; a write the box would refuse unsigned is
never queued.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue
from custom_components.svitgrid.executors.edge_box import EdgeBoxExecutor
from custom_components.svitgrid.harvest.event_scheduler_loop import _reason_for

SIGNING = {"signedEventData": '{"a":1}', "signature": "MEUC", "signingKeyId": "phone-1"}


async def _queue(tmp_path) -> EdgeCommandQueue:
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)
    # Create the database before the executor and the box open it at once.
    await queue.async_pending_count("EC-1")
    return queue


async def _box_acks(queue: EdgeCommandQueue, device_id: str, **ack) -> dict:
    """Play the box: wait for the next poll to hand a command out, then ack it."""
    for _ in range(200):
        row = await queue.async_next_pending(device_id)
        if row is not None:
            await queue.async_ack(row["id"], device_id=device_id, **ack)
            return row
        await asyncio.sleep(0.01)
    raise AssertionError("the executor never queued a command")


async def test_dispatch_enqueues_with_inverter_id_and_signing(tmp_path):
    queue = await _queue(tmp_path)
    executor = EdgeBoxExecutor(queue, "EC-1", "inv-1")

    _, row = await asyncio.gather(
        executor.dispatch("set_work_mode", {"mode": 1}, signing=SIGNING),
        _box_acks(queue, "EC-1", success=True, result={}, rejected=None, reason=None),
    )

    assert row["device_id"] == "EC-1"
    assert row["inverter_id"] == "inv-1"
    assert row["command"] == "set_work_mode"
    assert row["payload"] == {"mode": 1, "inverterId": "inv-1"}
    assert row["signing"] == SIGNING
    assert row["source"] == "executor"
    assert executor.relays_signature is True


async def test_success_ack_returns_result(tmp_path):
    queue = await _queue(tmp_path)
    executor = EdgeBoxExecutor(queue, "EC-1", "inv-1")

    result, row = await asyncio.gather(
        executor.dispatch("set_work_mode", {"mode": 1}, signing=SIGNING),
        _box_acks(queue, "EC-1", success=True, result={"written": 1}, rejected=None, reason=None),
    )

    # The ack's result, plus the queued command's id (svitgrid#1254): a
    # restore_priority_profile names the apply it undoes.
    assert result == {"written": 1, "commandId": row["id"]}


async def test_unsupported_ack_raises_not_implemented(tmp_path):
    queue = await _queue(tmp_path)
    executor = EdgeBoxExecutor(queue, "EC-1", "inv-1")

    with pytest.raises(NotImplementedError) as info:
        await asyncio.gather(
            executor.dispatch("set_inverter_setting", {"key": "x"}, signing=SIGNING),
            _box_acks(
                queue,
                "EC-1",
                success=False,
                result=None,
                rejected=True,
                reason="unsupported_model",
            ),
        )

    assert str(info.value) == "unsupported_model"
    assert _reason_for(info.value) == "unsupported_field"


async def test_other_failed_ack_raises_runtime_error(tmp_path):
    queue = await _queue(tmp_path)
    executor = EdgeBoxExecutor(queue, "EC-1", "inv-1")

    with pytest.raises(RuntimeError) as info:
        await asyncio.gather(
            executor.dispatch("set_work_mode", {"mode": 1}, signing=SIGNING),
            _box_acks(
                queue, "EC-1", success=False, result=None, rejected=False, reason="modbus_timeout"
            ),
        )

    assert str(info.value) == "modbus_timeout"
    assert _reason_for(info.value) == "write_failed"


async def test_timeout_raises_timeout_error(tmp_path):
    queue = await _queue(tmp_path)
    executor = EdgeBoxExecutor(queue, "EC-1", "inv-1", ack_timeout_s=0.05)

    with pytest.raises(TimeoutError) as info:
        await executor.dispatch("set_work_mode", {"mode": 1}, signing=SIGNING)

    assert str(info.value) == "edge_ack_timeout"
    assert _reason_for(info.value) == "write_failed"


async def test_unsigned_write_is_refused_before_enqueue(tmp_path):
    queue = await _queue(tmp_path)
    executor = EdgeBoxExecutor(queue, "EC-1", "inv-1", ack_timeout_s=0.05)

    with pytest.raises(PermissionError) as info:
        await executor.dispatch("set_work_mode", {"mode": 1})

    assert str(info.value) == "unsigned_write"
    assert await queue.async_recent("EC-1") == []


async def test_unsigned_read_command_is_allowed(tmp_path):
    queue = await _queue(tmp_path)
    executor = EdgeBoxExecutor(queue, "EC-1", "inv-1")

    result, row = await asyncio.gather(
        executor.dispatch("read_inverter_settings", {}),
        _box_acks(queue, "EC-1", success=True, result={"settings": {}}, rejected=None, reason=None),
    )

    assert result == {"settings": {}, "commandId": row["id"]}
    assert row["signing"] is None
