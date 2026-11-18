"""The island's operations journal (svitgrid D-2128).

On an island nothing kept a record of what the station was told to do: the
add-on's ActivityTracker holds ten events in memory, LAN commands left no
trace, and the board queue had no route that read it. The journal keeps one
row per inverter command, board command and rule run, survives a restart, is
pruned to 90 days, and `GET /api/svitgrid/operations` pages it in the cloud's
`{operations, nextBefore, retentionDays}` shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid import operations_journal
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue
from custom_components.svitgrid.operations_journal import OperationsJournal
from tests.test_commands_endpoint import (
    INVERTER_ID,
    ISLAND_KEY,
    _FakeRequest,
    _install_executor,
    _install_keystore,
    _make_executor,
    _make_signed_body,
)
from tests.test_event_scheduler_loop import (
    _NOW_IN_WINDOW,
    _NOW_OUT_WINDOW,
    FakeEventStore,
    FakeStore,
    _make_use_battery_event,
    _reading_at,
)


def _journal(tmp_path) -> OperationsJournal:
    return OperationsJournal(str(tmp_path / "operations.db"), None)


async def _all(journal: OperationsJournal) -> list[dict]:
    return (await journal.async_list(limit=200))["operations"]


# ── a LAN command ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lan_command_produces_a_row(hass, tmp_path):
    from custom_components.svitgrid.http_views import SvitgridCommandsView
    from custom_components.svitgrid.signing import generate_keypair

    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"phone-1": pub_hex})
    _install_executor(hass, INVERTER_ID, _make_executor(result={"chargeW": 3000}))
    journal = _journal(tmp_path)
    hass.data[DOMAIN]["operations_journal"] = journal

    payload = {"inverterId": INVERTER_ID, "chargeW": 3000}
    body = _make_signed_body(private_key, "phone-1", "set_battery_charge", payload)
    resp = await SvitgridCommandsView().post(
        _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    )

    assert resp.status == 200
    [row] = await _all(journal)
    assert row["source"] == "inverter"
    assert row["objectId"] == INVERTER_ID
    assert row["command"] == "set_battery_charge"
    assert row["status"] == "executed"
    assert row["payload"] == payload
    assert row["result"] == {"chargeW": 3000}
    assert row["createdBy"] == "phone-1"
    assert row["createdAt"] and row["executedAt"]


@pytest.mark.asyncio
async def test_failed_lan_command_is_a_failed_row(hass, tmp_path):
    from custom_components.svitgrid.http_views import SvitgridCommandsView
    from custom_components.svitgrid.signing import generate_keypair

    private_key, pub_hex = generate_keypair()
    _install_keystore(hass, trusted_public_keys_hex={"phone-1": pub_hex})
    _install_executor(hass, INVERTER_ID, _make_executor(side_effect=RuntimeError("timeout")))
    journal = _journal(tmp_path)
    hass.data[DOMAIN]["operations_journal"] = journal

    body = _make_signed_body(
        private_key, "phone-1", "set_battery_charge", {"inverterId": INVERTER_ID}
    )
    resp = await SvitgridCommandsView().post(
        _FakeRequest(hass, island_key_header=ISLAND_KEY, body=body)
    )

    assert resp.status == 502
    [row] = await _all(journal)
    assert row["status"] == "failed"
    assert row["result"] == {"error": "executor_error", "detail": "timeout"}


# ── a rule run ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rule_run_start_and_end_produce_rows(tmp_path):
    from custom_components.svitgrid.harvest.event_scheduler_loop import _tick

    event = _make_use_battery_event()
    event["name"] = "Morning battery"
    event["signing"] = {"signingKeyId": "phone-1"}
    event_store = FakeEventStore([event])
    store = FakeStore([_reading_at()])
    executor = AsyncMock()
    executor.dispatch = AsyncMock(return_value={"verified": True})
    journal = _journal(tmp_path)

    await _tick(store, event_store, lambda _i: executor, "UTC", _NOW_IN_WINDOW, journal=journal)
    started = await _all(journal)
    assert started, "an activation journals the commands it sent"
    assert {r["result"]["rulePhase"] for r in started} == {"start"}

    await _tick(store, event_store, lambda _i: executor, "UTC", _NOW_OUT_WINDOW, journal=journal)
    rows = await _all(journal)
    ended = [r for r in rows if r["result"]["rulePhase"] == "end"]
    assert ended, "a deactivation journals the restore it sent"
    # The mock's ACKs report no prior values, so the end writes nothing back
    # and is journaled as one row of its own.
    assert [r["command"] for r in ended] == ["rule_end"]
    for row in rows:
        assert row["source"] == "inverter"
        assert row["objectId"] == "inv-1"
        assert row["status"] == "executed"
        assert row["calendarEventName"] == "Morning battery"
        assert row["createdBy"] == "phone-1"
    assert len(started) == executor.dispatch.await_count


@pytest.mark.asyncio
async def test_rule_run_skip_produces_one_row(tmp_path):
    from custom_components.svitgrid.harvest import event_scheduler_loop

    event = _make_use_battery_event()
    event_store = FakeEventStore([event])
    journal = _journal(tmp_path)

    with patch.object(event_scheduler_loop, "needs_cloud", return_value=True):
        for _ in range(2):  # the second tick changes nothing: no second row
            await event_scheduler_loop._tick(
                FakeStore([]), event_store, lambda _i: None, "UTC", _NOW_IN_WINDOW, journal=journal
            )

    [row] = await _all(journal)
    assert row["command"] == "rule_skipped"
    assert row["status"] == "skipped"
    assert row["result"] == {"rulePhase": "skip", "reason": "needs_cloud"}


# ── a board command ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_board_command_produces_a_row_that_follows_the_ack(tmp_path):
    journal = _journal(tmp_path)
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None, journal=journal)

    command_id = await queue.async_enqueue(
        "box-1",
        "set_state",
        {"state": "on"},
        signing={"signingKeyId": "phone-1"},
        inverter_id=None,
        source="lan",
    )
    [row] = await _all(journal)
    assert row["source"] == "edge_device"
    assert row["objectId"] == "box-1"
    assert row["command"] == "set_state"
    assert row["status"] == "pending"
    assert row["createdBy"] == "phone-1"

    await queue.async_next_pending("box-1")
    await queue.async_ack(command_id, success=True, result={"ok": 1}, rejected=None, reason=None)
    [row] = await _all(journal)
    assert row["status"] == "executed"
    assert row["result"] == {"ok": 1}
    assert row["executedAt"]


@pytest.mark.asyncio
async def test_rejected_board_command_reads_as_rejected(tmp_path):
    journal = _journal(tmp_path)
    queue = EdgeCommandQueue(str(tmp_path / "edge_commands.db"), None, journal=journal)
    command_id = await queue.async_enqueue(
        "box-1", "set_state", {}, signing=None, inverter_id=None, source="lan"
    )
    await queue.async_next_pending("box-1")
    await queue.async_ack(
        command_id, success=False, result=None, rejected=True, reason="bad_signature"
    )
    [row] = await _all(journal)
    assert row["status"] == "rejected"
    assert row["result"] == {"reason": "bad_signature"}


# ── paging, retention, restart ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_paging_with_next_before_neither_repeats_nor_skips(tmp_path):
    journal = _journal(tmp_path)
    ids = [
        await journal.async_record(
            source="inverter", object_id="inv-1", command=f"c{i}", status="executed"
        )
        for i in range(7)
    ]

    seen: list[str] = []
    before = None
    while True:
        page = await journal.async_list(before=before, limit=3)
        seen += [r["id"] for r in page["operations"]]
        before = page["nextBefore"]
        if before is None:
            break

    assert seen == list(reversed(ids)), "newest first, each row once"


@pytest.mark.asyncio
async def test_a_page_on_the_boundary_ends_the_feed(tmp_path):
    journal = _journal(tmp_path)
    for i in range(3):
        await journal.async_record(
            source="inverter", object_id="inv-1", command=f"c{i}", status="executed"
        )
    page = await journal.async_list(limit=3)
    assert len(page["operations"]) == 3
    assert page["nextBefore"] is None


@pytest.mark.asyncio
async def test_object_id_filters_to_one_inverter_or_board(tmp_path):
    journal = _journal(tmp_path)
    await journal.async_record(source="inverter", object_id="inv-1", command="a", status="executed")
    await journal.async_record(
        source="edge_device", object_id="box-1", command="b", status="pending"
    )
    page = await journal.async_list(object_id="box-1")
    assert [r["command"] for r in page["operations"]] == ["b"]


@pytest.mark.asyncio
async def test_rows_older_than_90_days_are_gone(tmp_path, monkeypatch):
    journal = _journal(tmp_path)
    now = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
    clock = {"t": now - timedelta(days=91)}
    monkeypatch.setattr(operations_journal, "_utcnow", lambda: clock["t"])
    await journal.async_record(source="inverter", object_id="inv-1", command="old", status="x")
    clock["t"] = now - timedelta(days=89)
    await journal.async_record(source="inverter", object_id="inv-1", command="kept", status="x")
    clock["t"] = now

    page = await journal.async_list()
    assert [r["command"] for r in page["operations"]] == ["kept"]
    assert page["retentionDays"] == {"inverter": 90, "edge_device": 90}


@pytest.mark.asyncio
async def test_journal_survives_a_restart(tmp_path):
    first = _journal(tmp_path)
    await first.async_record(source="inverter", object_id="inv-1", command="a", status="executed")
    operations_journal._INITIALIZED.clear()  # a fresh process
    assert [r["command"] for r in await _all(_journal(tmp_path))] == ["a"]


# ── the endpoint ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_operations_endpoint_answers_the_cloud_shape(hass, tmp_path):
    from custom_components.svitgrid.http_views import SvitgridOperationsView

    _install_keystore(hass)
    journal = _journal(tmp_path)
    hass.data[DOMAIN]["operations_journal"] = journal
    for i in range(3):
        await journal.async_record(
            source="inverter", object_id="inv-1", command=f"c{i}", status="executed"
        )

    request = _FakeRequest(hass, island_key_header=ISLAND_KEY)
    request.query = {"limit": "2"}
    resp = await SvitgridOperationsView().get(request)
    assert resp.status == 200
    data = json.loads(resp.body)
    assert [r["command"] for r in data["operations"]] == ["c2", "c1"]
    assert data["nextBefore"]
    assert data["retentionDays"] == {"inverter": 90, "edge_device": 90}

    request = _FakeRequest(hass, island_key_header=ISLAND_KEY)
    request.query = {"limit": "2", "before": data["nextBefore"]}
    data = json.loads((await SvitgridOperationsView().get(request)).body)
    assert [r["command"] for r in data["operations"]] == ["c0"]
    assert data["nextBefore"] is None


@pytest.mark.asyncio
async def test_operations_endpoint_needs_the_island_key(hass, tmp_path):
    from custom_components.svitgrid.http_views import SvitgridOperationsView

    _install_keystore(hass)
    hass.data[DOMAIN]["operations_journal"] = _journal(tmp_path)
    request = _FakeRequest(hass, island_key_header="wrong")
    request.query = {}
    resp = await SvitgridOperationsView().get(request)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_operations_endpoint_rejects_a_foreign_cursor(hass, tmp_path):
    from custom_components.svitgrid.http_views import SvitgridOperationsView

    _install_keystore(hass)
    hass.data[DOMAIN]["operations_journal"] = _journal(tmp_path)
    request = _FakeRequest(hass, island_key_header=ISLAND_KEY)
    request.query = {"before": "1731900000.123456789"}
    resp = await SvitgridOperationsView().get(request)
    assert resp.status == 400
