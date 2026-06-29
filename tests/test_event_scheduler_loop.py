"""Tests for the island event scheduler loop (Task 5).

TDD: tests written before implementation.

Covers:
  1. Tick 1 — conditions met, status=idle → executor.dispatch called + state persisted (active).
  2. Tick 2 — state persisted (active), conditions still met → NOT dispatched again (guard).
  3. Tick 3 — event goes out-of-window → deactivate/restore command dispatched, state reset.
  4. async_setup_entry with cloud_ingest_enabled=True → scheduler NOT spawned.
  5. async_setup_entry with cloud_ingest_enabled=False → scheduler IS spawned + event_store
     populated in hass.data[DOMAIN]["event_store"].
  6. No reading for an event's inverter → event skipped, no dispatch.
  7. Per-event error isolation: one bad event does not kill the tick for others.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.reading_store import ReadingStore

# ─────────────────────────────── Helpers ────────────────────────────────────


class FakeEventStore:
    """In-memory event store that persists state between async_set_execution_state calls."""

    def __init__(self, events: list[dict]):
        # Keyed by event id; each entry is a mutable dict (incl. executionState).
        self._events: dict[str, dict] = {e["id"]: dict(e) for e in events}
        self.state_calls: list[tuple[str, dict]] = []

    async def async_list_events(self) -> list[dict]:
        return [dict(e) for e in self._events.values()]

    async def async_set_execution_state(self, event_id: str, state: dict) -> None:
        self.state_calls.append((event_id, dict(state)))
        if event_id in self._events:
            self._events[event_id]["executionState"] = dict(state)


class FakeStore:
    """Minimal ReadingStore stand-in that returns a fixed live snapshot."""

    def __init__(self, snapshots: list[dict]):
        self._snapshots = snapshots

    async def live_snapshot(self) -> list[dict]:
        return list(self._snapshots)


def _make_use_battery_event(
    event_id: str = "evt-1",
    inverter_id: str = "inv-1",
    start_time: str = "10:00",
    end_time: str = "11:00",
    exec_state: dict | None = None,
) -> dict:
    """Return a use_battery calendar event dict suitable for the evaluator."""
    return {
        "id": event_id,
        "inverterId": inverter_id,
        "enabled": True,
        "mode": "use_battery",
        "config": {},
        "schedule": {
            "startTime": start_time,
            "endTime": end_time,
            "recurrence": "daily",
        },
        "executionState": exec_state or {},
    }


def _reading_at(inverter_id: str = "inv-1") -> dict:
    """Minimal live-snapshot row for inverter_id."""
    return {
        "inverterId": inverter_id,
        "ts": "2026-06-29T10:00:00Z",
        "payload": {
            "batterySoc": 50,
            "batteryPower": 0,
            "batteryVoltage": 52.0,
            "gridPower": 200,
            "loadPower": 200,
            "pvPower": 0,
        },
        "intervalS": 10,
    }


# in-window time for the 10:00–11:00 schedule
_NOW_IN_WINDOW = datetime(2026, 6, 29, 10, 30, 0, tzinfo=UTC)
# out-of-window time for the 10:00–11:00 schedule
_NOW_OUT_WINDOW = datetime(2026, 6, 29, 11, 30, 0, tzinfo=UTC)


# ──────────────────────── Scheduler loop unit tests ──────────────────────────


@pytest.mark.asyncio
async def test_tick1_activate_dispatches_and_persists_state():
    """First tick with status=idle, in-window reading → activate → executor.dispatch called,
    execution state persisted as active."""
    from custom_components.svitgrid.harvest.event_scheduler_loop import _tick

    event = _make_use_battery_event()
    event_store = FakeEventStore([event])
    store = FakeStore([_reading_at()])

    mock_executor = AsyncMock()
    mock_executor.dispatch = AsyncMock(return_value={"written": [], "verified": True})

    def executor_for(inv_id):
        return mock_executor if inv_id == "inv-1" else None

    await _tick(store, event_store, executor_for, "UTC", _NOW_IN_WINDOW)

    # Dispatcher must have been called (use_battery activates via two commands)
    assert mock_executor.dispatch.call_count >= 1, "executor.dispatch must be called on activate"

    # State must be persisted
    assert len(event_store.state_calls) == 1
    _evt_id, persisted = event_store.state_calls[0]
    assert _evt_id == "evt-1"
    assert persisted.get("status") == "active", "execution state must be persisted as active"


@pytest.mark.asyncio
async def test_tick2_guard_not_dispatched_when_already_active():
    """Second tick: state=active persisted from tick 1, conditions still met →
    no dispatch (hold). Guard survives because state is read from the store."""
    from custom_components.svitgrid.harvest.event_scheduler_loop import _tick

    # Start with status=active (as if tick 1 already ran and persisted it)
    event = _make_use_battery_event(exec_state={"status": "active", "lastActivatedAt": "2026-06-29T10:00:00+00:00"})
    event_store = FakeEventStore([event])
    store = FakeStore([_reading_at()])

    mock_executor = AsyncMock()
    mock_executor.dispatch = AsyncMock(return_value={"written": [], "verified": True})

    def executor_for(inv_id):
        return mock_executor if inv_id == "inv-1" else None

    await _tick(store, event_store, executor_for, "UTC", _NOW_IN_WINDOW)

    # Guard: already active → evaluator returns hold → no dispatch
    assert mock_executor.dispatch.call_count == 0, "executor.dispatch must NOT be called when already active (guard)"

    # State must still be persisted every tick (guard-survival — hysteresis bookkeeping)
    assert len(event_store.state_calls) == 1, "async_set_execution_state must be called every tick"
    _evt_id, persisted = event_store.state_calls[0]
    assert persisted.get("status") == "active"


@pytest.mark.asyncio
async def test_guard_survives_across_two_ticks_via_persisted_state():
    """End-to-end guard test: two consecutive _tick calls with a persistent
    FakeEventStore — state from tick 1 must flow into tick 2 so the guard fires."""
    from custom_components.svitgrid.harvest.event_scheduler_loop import _tick

    event = _make_use_battery_event()  # status=idle at start
    event_store = FakeEventStore([event])
    store = FakeStore([_reading_at()])

    mock_executor = AsyncMock()
    mock_executor.dispatch = AsyncMock(return_value={"written": [], "verified": True})

    def executor_for(inv_id):
        return mock_executor if inv_id == "inv-1" else None

    # Tick 1: activates, persists active state
    await _tick(store, event_store, executor_for, "UTC", _NOW_IN_WINDOW)
    dispatched_tick1 = mock_executor.dispatch.call_count
    assert dispatched_tick1 >= 1, "tick 1 must dispatch (activate)"

    # Tick 2: FakeEventStore returns the persisted state (active) → evaluator
    # sees status=active → returns hold → no further dispatch
    mock_executor.dispatch.reset_mock()
    await _tick(store, event_store, executor_for, "UTC", _NOW_IN_WINDOW)
    assert mock_executor.dispatch.call_count == 0, (
        "tick 2 must NOT dispatch — guard fired because persisted state (active) was read back"
    )


@pytest.mark.asyncio
async def test_tick3_deactivate_when_out_of_window():
    """After activation, going out of window → evaluator returns deactivate →
    restore commands dispatched, state reset to idle."""
    from custom_components.svitgrid.harvest.event_scheduler_loop import _tick

    # Already active (tick 1 + 2 happened before this tick)
    event = _make_use_battery_event(
        exec_state={"status": "active", "lastActivatedAt": "2026-06-29T10:00:00+00:00"}
    )
    event_store = FakeEventStore([event])
    store = FakeStore([_reading_at()])

    mock_executor = AsyncMock()
    mock_executor.dispatch = AsyncMock(return_value={"written": [], "verified": True})

    def executor_for(inv_id):
        return mock_executor if inv_id == "inv-1" else None

    # now_utc is OUTSIDE the 10:00–11:00 window
    await _tick(store, event_store, executor_for, "UTC", _NOW_OUT_WINDOW)

    # Out-of-window + active → deactivate (restore commands for use_battery mode)
    assert mock_executor.dispatch.call_count >= 1, "restore commands must be dispatched on deactivate"

    # State must be persisted — status reset to idle
    assert len(event_store.state_calls) == 1
    _evt_id, persisted = event_store.state_calls[0]
    assert persisted.get("status") == "idle", "execution state must be reset to idle after deactivate"


@pytest.mark.asyncio
async def test_tick_skips_event_when_no_reading_for_inverter():
    """If no live reading exists for an event's inverter, the event is skipped
    without dispatching or persisting (no crash)."""
    from custom_components.svitgrid.harvest.event_scheduler_loop import _tick

    event = _make_use_battery_event(inverter_id="inv-missing")
    event_store = FakeEventStore([event])
    # Snapshot for a DIFFERENT inverter — inv-missing has no reading
    store = FakeStore([_reading_at("inv-other")])

    mock_executor = AsyncMock()

    def executor_for(_inv_id):
        return mock_executor

    await _tick(store, event_store, executor_for, "UTC", _NOW_IN_WINDOW)

    assert mock_executor.dispatch.call_count == 0, "no dispatch when inverter has no reading"
    # No state calls — event was skipped entirely
    assert len(event_store.state_calls) == 0, "no state persist when event is skipped (no reading)"


@pytest.mark.asyncio
async def test_tick_disabled_event_is_skipped():
    """Disabled events (enabled=False) must not be evaluated or dispatched."""
    from custom_components.svitgrid.harvest.event_scheduler_loop import _tick

    event = _make_use_battery_event()
    event["enabled"] = False
    event_store = FakeEventStore([event])
    store = FakeStore([_reading_at()])

    mock_executor = AsyncMock()

    def executor_for(_inv_id):
        return mock_executor

    await _tick(store, event_store, executor_for, "UTC", _NOW_IN_WINDOW)

    assert mock_executor.dispatch.call_count == 0, "disabled event must not dispatch"
    assert len(event_store.state_calls) == 0, "disabled event must not persist state"


@pytest.mark.asyncio
async def test_tick_per_event_error_isolation():
    """An exception in one event's evaluation must not prevent other events
    from being processed (per-event try/except)."""
    from custom_components.svitgrid.harvest.event_evaluator import EvaluatorDecision
    from custom_components.svitgrid.harvest.event_scheduler_loop import _tick

    event_bad = _make_use_battery_event("evt-bad", "inv-bad")
    event_good = _make_use_battery_event("evt-good", "inv-good")
    event_store = FakeEventStore([event_bad, event_good])
    store = FakeStore([_reading_at("inv-bad"), _reading_at("inv-good")])

    mock_executor = AsyncMock()
    mock_executor.dispatch = AsyncMock(return_value={"written": [], "verified": True})

    def executor_for(_inv_id):
        return mock_executor

    good_decision = EvaluatorDecision(
        action="activate",
        commands=[("set_work_mode", {"workMode": 2})],
        new_state={"status": "active"},
    )

    call_count = 0

    def fake_evaluate(event, reading, exec_state, now_utc, tz):
        nonlocal call_count
        call_count += 1
        if event["id"] == "evt-bad":
            raise RuntimeError("simulated evaluation failure")
        return good_decision

    with patch(
        "custom_components.svitgrid.harvest.event_scheduler_loop.evaluate_event",
        side_effect=fake_evaluate,
    ):
        await _tick(store, event_store, executor_for, "UTC", _NOW_IN_WINDOW)

    # Good event must still be processed despite the bad one failing
    assert mock_executor.dispatch.call_count >= 1, "good event must still dispatch after bad event error"
    good_state_calls = [c for c in event_store.state_calls if c[0] == "evt-good"]
    assert len(good_state_calls) == 1, "good event state must be persisted"


# ─────────────────── async_setup_entry gate tests ─────────────────────────────

_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}

_HARVEST_INVERTER = {
    "inverter_id": "ha-island-inv",
    "entity_map": {},
    "command_recipes": [],
    "command_config": {},
    "brand": "Deye",
    "model": "SG04LP3",
    "phases": 3,
    "has_battery": True,
    "pv_strings": 2,
    "preset_id": None,
    "harvest_config": {
        "model_id": "deye_sg04lp3",
        "host": "10.0.0.5",
        "port": 8899,
        "slave_id": 1,
    },
}

_BASE_DATA = {
    "api_base": "https://api.example.com",
    "api_key": "test-key",
    "edge_device_id": "ed-1",
    "household_id": "h-island",
    "signing_key_id": "ha-home-01",
    "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    "public_key_hex": "04" + "a" * 128,
    "trusted_keys": [],
    "inverters": [_HARVEST_INVERTER],
}

_MINIMAL_SPEC = {
    "modelId": "deye_sg04lp3",
    "version": 1,
    "protocol": "solarman_v5",
    "port": 8899,
    "defaultSlaveId": 1,
    "reads": [],
    "derivations": [],
}


def _make_entry(cloud_ingest_enabled=None, entry_id="entry-island"):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    data = dict(_BASE_DATA)
    if cloud_ingest_enabled is not None:
        data["cloud_ingest_enabled"] = cloud_ingest_enabled
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (island)",
        data=data,
        entry_id=entry_id,
    )


@pytest.fixture(autouse=True)
def _stub_store_side_effects():
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
    ):
        yield


@pytest.mark.asyncio
async def test_setup_entry_cloud_ingest_true_scheduler_not_spawned(
    hass, enable_custom_integrations
):
    """cloud_ingest_enabled=True → scheduler loop must NOT be spawned."""
    from custom_components.svitgrid import async_setup_entry

    entry = _make_entry(cloud_ingest_enabled=True, entry_id="entry-cloud-on")
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch(
            "custom_components.svitgrid.run_event_scheduler_loop", new_callable=AsyncMock
        ) as mock_scheduler,
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
    ):
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(return_value=dict(_MINIMAL_SPEC))
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    assert mock_scheduler.call_count == 0, (
        "run_event_scheduler_loop must NOT be called when cloud_ingest_enabled=True"
    )


@pytest.mark.asyncio
async def test_setup_entry_cloud_ingest_false_scheduler_spawned_and_event_store_in_hass_data(
    hass, enable_custom_integrations
):
    """cloud_ingest_enabled=False → scheduler IS spawned + event_store in hass.data[DOMAIN]."""
    from custom_components.svitgrid import async_setup_entry
    from custom_components.svitgrid.island_event_store import IslandEventStore

    entry = _make_entry(cloud_ingest_enabled=False, entry_id="entry-island-off")
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock) as sender,
        patch(
            "custom_components.svitgrid.run_event_scheduler_loop", new_callable=AsyncMock
        ) as mock_scheduler,
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
    ):
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(return_value=dict(_MINIMAL_SPEC))
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True

    # Sender must NOT have been spawned (island mode)
    assert sender.call_count == 0, "run_sender_loop must not be called in island mode"

    # Scheduler must have been spawned exactly once
    assert mock_scheduler.call_count == 1, (
        "run_event_scheduler_loop must be called exactly once when cloud_ingest_enabled=False"
    )

    # event_store must be populated in hass.data[DOMAIN]
    event_store = hass.data.get(DOMAIN, {}).get("event_store")
    assert event_store is not None, "hass.data[DOMAIN]['event_store'] must be set"
    assert isinstance(event_store, IslandEventStore), (
        "event_store must be an IslandEventStore instance"
    )

    # Scheduler receives the event_store as a keyword argument
    call_kwargs = mock_scheduler.call_args
    assert call_kwargs is not None
    # The scheduler is called with keyword args; accept either positional or keyword
    all_args = list(call_kwargs.args) + list(call_kwargs.kwargs.values())
    assert event_store in all_args, (
        "run_event_scheduler_loop must receive the event_store from hass.data"
    )
