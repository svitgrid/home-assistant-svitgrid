"""The edge command queue (edge island, Phase 1, Task 4, svitgrid#1206).

The integration holds the commands an adopted edge box drains through
`GET /api/v3/executors/commands`. The box takes one command per poll and acks
it; a command delivered and never acked goes back once, then expires, so a
lost ack neither loses the command nor repeats it forever. Nothing enqueues
yet (Phase 2's `EdgeBoxExecutor` does), so these tests call `async_enqueue`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.svitgrid import edge_command_queue
from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue

T0 = datetime(2026, 10, 29, 10, 0, 0, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch):
    now = {"t": T0}

    def _advance(seconds: float) -> None:
        now["t"] += timedelta(seconds=seconds)

    monkeypatch.setattr(edge_command_queue, "_utcnow", lambda: now["t"])
    return _advance


def _queue(tmp_path) -> EdgeCommandQueue:
    return EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None)


SIGNING = {"signedEventData": '{"a":1}', "signature": "MEUC", "signingKeyId": "phone-1"}


async def test_enqueue_then_next_pending_marks_delivered(tmp_path, clock):
    q = _queue(tmp_path)
    first = await q.async_enqueue(
        "EC-1", "set_work_mode", {"mode": 1}, signing=SIGNING, inverter_id="inv-1", source="test"
    )
    clock(1)
    second = await q.async_enqueue(
        "EC-1", "read_inverter_settings", {}, signing=None, inverter_id="inv-1", source="test"
    )
    assert first != second
    assert await q.async_pending_count("EC-1") == 2

    row = await q.async_next_pending("EC-1")
    assert row["id"] == first
    assert row["state"] == "delivered"
    assert row["command"] == "set_work_mode"
    assert row["payload"] == {"mode": 1}
    assert row["signing"] == SIGNING
    assert row["inverter_id"] == "inv-1"
    assert row["delivered_at"] is not None
    assert await q.async_pending_count("EC-1") == 1

    assert (await q.async_next_pending("EC-1"))["id"] == second
    assert await q.async_next_pending("EC-1") is None


async def test_queue_is_per_box(tmp_path, clock):
    q = _queue(tmp_path)
    await q.async_enqueue("EC-1", "a", {}, signing=None, inverter_id=None, source="test")
    assert await q.async_next_pending("EC-2") is None
    assert await q.async_pending_count("EC-2") == 0
    assert (await q.async_next_pending("EC-1"))["command"] == "a"


async def test_ack_sets_state_and_wakes_waiter(tmp_path, clock):
    q = _queue(tmp_path)
    cid = await q.async_enqueue("EC-1", "a", {}, signing=None, inverter_id=None, source="test")
    await q.async_next_pending("EC-1")

    waiter = asyncio.create_task(q.async_wait(cid, 5))
    await asyncio.sleep(0)
    assert not waiter.done()

    row = await q.async_ack(cid, success=True, result={"ok": 1}, rejected=False, reason=None)
    assert row["state"] == "success"
    assert row["result"] == {"ok": 1}
    assert row["acked_at"] is not None

    waited = await asyncio.wait_for(waiter, 1)
    assert waited["id"] == cid
    assert waited["state"] == "success"


async def test_ack_failure_records_reason(tmp_path, clock):
    q = _queue(tmp_path)
    cid = await q.async_enqueue("EC-1", "a", {}, signing=None, inverter_id=None, source="test")
    await q.async_next_pending("EC-1")
    row = await q.async_ack(cid, success=False, result=None, rejected=True, reason="bad_signature")
    assert row["state"] == "failed"
    assert row["rejected"] is True
    assert row["reason"] == "bad_signature"


async def test_ack_unknown_id_is_none(tmp_path, clock):
    q = _queue(tmp_path)
    assert await q.async_ack("nope", success=True, result=None, rejected=None, reason=None) is None


async def test_ack_from_another_box_is_ignored(tmp_path, clock):
    q = _queue(tmp_path)
    cid = await q.async_enqueue("EC-1", "a", {}, signing=None, inverter_id=None, source="test")
    await q.async_next_pending("EC-1")
    assert (
        await q.async_ack(
            cid, success=True, result=None, rejected=None, reason=None, device_id="EC-2"
        )
        is None
    )
    assert (await q.async_recent("EC-1"))[0]["state"] == "delivered"


async def test_second_ack_does_not_overwrite_the_first(tmp_path, clock):
    q = _queue(tmp_path)
    cid = await q.async_enqueue("EC-1", "a", {}, signing=None, inverter_id=None, source="test")
    await q.async_next_pending("EC-1")
    await q.async_ack(cid, success=True, result=None, rejected=None, reason=None)
    row = await q.async_ack(cid, success=False, result=None, rejected=None, reason="late")
    assert row["state"] == "success"


async def test_wait_times_out_with_the_current_row(tmp_path, clock):
    q = _queue(tmp_path)
    cid = await q.async_enqueue("EC-1", "a", {}, signing=None, inverter_id=None, source="test")
    row = await q.async_wait(cid, 0.01)
    assert row["id"] == cid
    assert row["state"] == "pending"


async def test_undelivered_for_120s_returns_to_pending_once_then_expires(tmp_path, clock):
    q = _queue(tmp_path)
    cid = await q.async_enqueue("EC-1", "a", {}, signing=None, inverter_id=None, source="test")
    assert (await q.async_next_pending("EC-1"))["id"] == cid

    clock(119)
    assert await q.async_next_pending("EC-1") is None

    clock(2)
    again = await q.async_next_pending("EC-1")
    assert again["id"] == cid
    assert again["state"] == "delivered"

    clock(121)
    assert await q.async_next_pending("EC-1") is None
    row = (await q.async_recent("EC-1"))[0]
    assert row["state"] == "expired"
    assert row["reason"] == "not_acked"
    assert await q.async_pending_count("EC-1") == 0


async def test_ttl_expires_pending(tmp_path, clock):
    q = _queue(tmp_path)
    cid = await q.async_enqueue(
        "EC-1", "a", {}, signing=None, inverter_id=None, source="test", ttl_s=60
    )
    clock(61)
    assert await q.async_pending_count("EC-1") == 0
    assert await q.async_next_pending("EC-1") is None
    row = (await q.async_recent("EC-1"))[0]
    assert row["id"] == cid
    assert row["state"] == "expired"
    assert row["reason"] == "ttl"


async def test_expiry_wakes_waiter(tmp_path, clock):
    q = _queue(tmp_path)
    cid = await q.async_enqueue(
        "EC-1", "a", {}, signing=None, inverter_id=None, source="test", ttl_s=60
    )
    waiter = asyncio.create_task(q.async_wait(cid, 5))
    await asyncio.sleep(0)
    clock(61)
    await q.async_next_pending("EC-1")
    assert (await asyncio.wait_for(waiter, 1))["state"] == "expired"


async def test_recent_is_newest_first_and_limited(tmp_path, clock):
    q = _queue(tmp_path)
    ids = []
    for i in range(3):
        ids.append(
            await q.async_enqueue("EC-1", f"c{i}", {}, signing=None, inverter_id=None, source="t")
        )
        clock(1)
    recent = await q.async_recent("EC-1", limit=2)
    assert [r["id"] for r in recent] == [ids[2], ids[1]]


async def test_queue_survives_a_new_instance(tmp_path, clock):
    cid = await _queue(tmp_path).async_enqueue(
        "EC-1", "a", {"x": 1}, signing=SIGNING, inverter_id="inv-1", source="test"
    )
    row = await _queue(tmp_path).async_next_pending("EC-1")
    assert row["id"] == cid
    assert row["signing"] == SIGNING


async def test_forget_device_drops_only_that_box_and_wakes_its_waiters(tmp_path, clock):
    q = _queue(tmp_path)
    gone = await q.async_enqueue(
        "EC-1", "set_work_mode", {}, signing=None, inverter_id="inv-1", source="test"
    )
    kept = await q.async_enqueue(
        "EC-2", "set_work_mode", {}, signing=None, inverter_id="inv-2", source="test"
    )
    waiter = asyncio.create_task(q.async_wait(gone, 5))
    await asyncio.sleep(0.05)

    assert await q.async_forget_device("EC-1") == 1

    assert await asyncio.wait_for(waiter, 1) is None
    assert await q.async_pending_count("EC-1") == 0
    assert (await q.async_wait(kept, 0))["state"] == "pending"
    assert await q.async_forget_device("EC-1") == 0


async def test_a_queue_written_before_target_gains_it(tmp_path):
    """A database from an earlier version has no `target_json`: it is added,
    and the rows already queued read back with no target."""
    import sqlite3

    db = str(tmp_path / "edge_commands.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE edge_commands (id TEXT PRIMARY KEY, device_id TEXT NOT NULL, "
        "inverter_id TEXT, command TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "signing_json TEXT, state TEXT NOT NULL, source TEXT NOT NULL, "
        "created_at TEXT NOT NULL, delivered_at TEXT, acked_at TEXT, result_json TEXT, "
        "reason TEXT, expires_at TEXT NOT NULL, deliveries INTEGER NOT NULL DEFAULT 0, "
        "rejected INTEGER)"
    )
    conn.execute(
        "INSERT INTO edge_commands (id, device_id, command, payload_json, state, source, "
        "created_at, expires_at) VALUES ('old', 'EC-1', 'list_devices', '{}', 'pending', "
        "'zigbee', '2026-10-30T10:00:00.000Z', '2999-01-01T00:00:00.000Z')"
    )
    conn.commit()
    conn.close()
    queue = EdgeCommandQueue(db, None)

    new_id = await queue.async_enqueue(
        "EC-1",
        "set_state",
        {"on": True},
        signing=None,
        inverter_id=None,
        source="zigbee",
        target={"zigbeeAddr": "0x1234", "zigbeeEndpoint": 1},
    )

    assert (await queue.async_get("old"))["target"] is None
    assert (await queue.async_get(new_id))["target"] == {
        "zigbeeAddr": "0x1234",
        "zigbeeEndpoint": 1,
    }
