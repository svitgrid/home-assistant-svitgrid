"""An event the island cannot run says why: `skippedReason: "needs_cloud"`.

Day planning, and any event that waits on a forecast, a day-ahead price or a
scheduled outage, needs data only the cloud has. The island scheduler skips
such an event. It records `executionState.skippedReason = "needs_cloud"` so
`GET /api/svitgrid/events` can tell the app, which shows «Не працює без
хмарної копії». The reason is cleared once the event no longer needs the
cloud. (true-island-contract.md, "Event skip reason", 2026-10-15.)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.svitgrid.harvest.event_evaluator import evaluate_event, needs_cloud
from custom_components.svitgrid.harvest.event_scheduler_loop import _tick
from tests.test_event_scheduler_loop import FakeEventStore, FakeStore, _reading_at

TZ = "Europe/Kyiv"
_NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)
_ALL_DAY = {"recurrence": "daily", "startTime": "00:00", "endTime": "23:59"}
_EVENING = {"recurrence": "daily", "startTime": "20:00", "endTime": "22:00"}


def _custom(condition_type: str) -> dict:
    return {
        "mode": "custom",
        "config": {
            "customConditions": [{"type": condition_type}],
            "customActions": [{"command": "set_work_mode", "payload": {"workMode": 0}}],
        },
    }


_CLOUD_EVENTS = [
    pytest.param({"mode": "day_planning", "config": {}}, id="day_planning"),
    pytest.param(
        {"mode": "gen_force", "config": {"requireScheduledOutage": True}},
        id="gen_force waits for a scheduled outage",
    ),
    pytest.param(_custom("forecast_today"), id="forecast_today"),
    pytest.param(_custom("dam_price"), id="dam_price"),
    pytest.param(_custom("scheduled_outage"), id="scheduled_outage"),
]


@pytest.mark.parametrize("event", _CLOUD_EVENTS)
@pytest.mark.parametrize("schedule", [_ALL_DAY, _EVENING], ids=["in window", "out of window"])
def test_the_evaluator_records_needs_cloud(event, schedule):
    event = {**event, "schedule": schedule}
    decision = evaluate_event(event, {"batterySoc": 50}, {"status": "idle"}, _NOW, TZ)
    assert decision.action == "skip"
    assert decision.new_state["skippedReason"] == "needs_cloud"
    assert needs_cloud(event) is True


def test_an_event_the_island_can_run_carries_no_skip_reason():
    event = {
        "mode": "custom",
        "schedule": _ALL_DAY,
        "config": {
            "customConditions": [{"type": "battery_soc", "op": "gte", "threshold": 90}],
            "customActions": [{"command": "set_work_mode", "payload": {"workMode": 0}}],
        },
    }
    assert needs_cloud(event) is False
    stale = {"status": "idle", "skippedReason": "needs_cloud"}
    decision = evaluate_event(event, {"batterySoc": 50}, stale, _NOW, TZ)
    assert "skippedReason" not in decision.new_state


def test_the_skip_reason_clears_out_of_window_too():
    event = {"mode": "use_battery", "schedule": _EVENING, "config": {}}
    stale = {"status": "idle", "skippedReason": "needs_cloud"}
    decision = evaluate_event(event, {"batterySoc": 50}, stale, _NOW, TZ)
    assert "skippedReason" not in decision.new_state


@pytest.mark.asyncio
async def test_the_scheduler_persists_the_reason_where_the_app_reads_it():
    event = {
        "id": "ev-1",
        "inverterId": "inv-1",
        "enabled": True,
        "mode": "day_planning",
        "schedule": _ALL_DAY,
        "config": {},
    }
    event_store = FakeEventStore([event])
    executor = AsyncMock()
    await _tick(FakeStore([_reading_at("inv-1")]), event_store, lambda _i: executor, TZ, _NOW)

    [stored] = await event_store.async_list_events()
    assert stored["executionState"]["skippedReason"] == "needs_cloud"
    executor.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_the_scheduler_records_the_reason_with_no_reading_yet():
    """The event needs the cloud whatever the inverter reads, so the app learns
    why it does not run even before the first reading arrives."""
    event = {
        "id": "ev-1",
        "inverterId": "inv-quiet",
        "enabled": True,
        "mode": "day_planning",
        "schedule": _ALL_DAY,
        "config": {},
    }
    event_store = FakeEventStore([event])
    await _tick(FakeStore([]), event_store, lambda _i: None, TZ, _NOW)

    [stored] = await event_store.async_list_events()
    assert stored["executionState"]["skippedReason"] == "needs_cloud"


@pytest.mark.asyncio
async def test_the_scheduler_does_not_rewrite_an_unchanged_reason():
    event = {
        "id": "ev-1",
        "inverterId": "inv-quiet",
        "enabled": True,
        "mode": "day_planning",
        "schedule": _ALL_DAY,
        "config": {},
        "executionState": {"status": "idle", "skippedReason": "needs_cloud"},
    }
    event_store = FakeEventStore([event])
    await _tick(FakeStore([]), event_store, lambda _i: None, TZ, _NOW)
    assert event_store.state_calls == []


@pytest.mark.asyncio
async def test_the_scheduler_clears_the_reason_once_the_event_changes():
    """The app edited the event so it no longer waits on a forecast."""
    event = {
        "id": "ev-1",
        "inverterId": "inv-1",
        "enabled": True,
        "mode": "use_battery",
        "schedule": _EVENING,
        "config": {},
        "executionState": {"status": "idle", "skippedReason": "needs_cloud"},
    }
    event_store = FakeEventStore([event])
    await _tick(FakeStore([_reading_at("inv-1")]), event_store, lambda _i: AsyncMock(), TZ, _NOW)

    [stored] = await event_store.async_list_events()
    assert "skippedReason" not in stored["executionState"]


@pytest.mark.asyncio
async def test_a_stale_reason_clears_with_no_reading_too():
    event = {
        "id": "ev-1",
        "inverterId": "inv-quiet",
        "enabled": True,
        "mode": "use_battery",
        "schedule": _EVENING,
        "config": {},
        "executionState": {"status": "idle", "skippedReason": "needs_cloud"},
    }
    event_store = FakeEventStore([event])
    await _tick(FakeStore([]), event_store, lambda _i: None, TZ, _NOW)

    [stored] = await event_store.async_list_events()
    assert "skippedReason" not in stored["executionState"]
