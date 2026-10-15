"""Island restores put back the values the inverter held, never defaults.

sell_to_grid and use_battery used to restore `previousWorkMode` (default 2)
and `previousTimerEnabled` (default true), and battery_maintenance restored
`maintenancePriorSolarSell` (default false), with nothing ever reading them
from the inverter. The values now come from the activation's own write: the
WriteExecutor reads each register before it writes and reports it in the ACK
(`prevWorkMode`, `prevGridChargeEnabled`, `prevSolarSell`, as the edge
firmware does), and the scheduler keeps them in the execution state. A restore
whose value was never read writes nothing and reports `prev_read_failed`.

battery_maintenance also returned its in-window commands as `hold`, which the
scheduler never dispatches, so a grid fallback whose hour came after
activation never reached the inverter. It now returns `update`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from custom_components.svitgrid.harvest.event_evaluator import (
    evaluate_event,
    restore_decision,
)
from custom_components.svitgrid.harvest.event_scheduler_loop import (
    _tick,
    request_removal,
    settle_removal,
)

TZ = "UTC"
_IN = datetime(2026, 10, 15, 10, 30, tzinfo=UTC)
_OUT = datetime(2026, 10, 15, 11, 30, tzinfo=UTC)
_SCHEDULE = {"startTime": "10:00", "endTime": "11:00", "recurrence": "daily"}
_READING = {"batterySoc": 50, "pvPower": 0, "gridPower": 200, "loadPower": 200}

_TABLE = [
    {
        "start": h * 400,
        "end": (h + 1) * 400 % 2400,
        "power": 5000,
        "voltage": 5300,
        "soc": 20,
        "enable": 0,
    }
    for h in range(6)
]


def _event(mode: str, config: dict | None = None, state: dict | None = None, **kw) -> dict:
    return {
        "id": "evt-1",
        "inverterId": "inv-1",
        "enabled": True,
        "mode": mode,
        "schedule": dict(kw.get("schedule") or _SCHEDULE),
        "config": config or {},
        "executionState": state or {},
    }


def _no_prior_error(state: dict, command: str) -> None:
    assert state["lastCommandError"]["reason"] == "prev_read_failed"
    assert state["lastCommandError"]["command"] == command


# ═══════════════════════════ Evaluator ═══════════════════════════════════════


@pytest.mark.parametrize("mode", ["sell_to_grid", "use_battery"])
def test_activation_invents_no_prior_work_mode(mode):
    decision = evaluate_event(_event(mode), _READING, {}, _IN, TZ)
    assert decision.action == "activate"
    assert "previousWorkMode" not in decision.new_state
    assert "previousTimerEnabled" not in decision.new_state


def test_sell_to_grid_restores_the_captured_work_mode():
    state = {"status": "active", "previousWorkMode": 1}
    decision = evaluate_event(_event("sell_to_grid"), _READING, state, _OUT, TZ)
    assert decision.action == "deactivate"
    assert decision.commands == [("set_work_mode", {"workMode": 1})]
    assert "lastCommandError" not in decision.new_state
    assert "previousWorkMode" not in decision.new_state


def test_sell_to_grid_without_a_captured_mode_writes_nothing():
    decision = evaluate_event(_event("sell_to_grid"), _READING, {"status": "active"}, _OUT, TZ)
    assert decision.action == "deactivate"
    assert decision.commands == []
    assert decision.new_state["status"] == "idle"
    _no_prior_error(decision.new_state, "set_work_mode")


def test_sell_to_grid_smart_release_restores_the_captured_mode():
    event = _event("sell_to_grid", {"sellMode": "smart", "pvThresholdW": 500})
    state = {
        "status": "active",
        "previousWorkMode": 1,
        "conditionLostSince": (_IN - timedelta(minutes=3)).isoformat(),
    }
    decision = evaluate_event(event, {"pvPower": 0}, state, _IN, TZ)
    assert decision.action == "deactivate"
    assert decision.commands == [("set_work_mode", {"workMode": 1})]
    # Kept for the next activation in this window, as the cloud keeps it.
    assert decision.new_state["previousWorkMode"] == 1


def test_sell_to_grid_smart_release_without_a_captured_mode_writes_nothing():
    event = _event("sell_to_grid", {"sellMode": "smart", "pvThresholdW": 500})
    state = {"status": "active", "conditionLostSince": (_IN - timedelta(minutes=3)).isoformat()}
    decision = evaluate_event(event, {"pvPower": 0}, state, _IN, TZ)
    assert decision.action == "deactivate"
    assert decision.commands == []
    _no_prior_error(decision.new_state, "set_work_mode")


def test_use_battery_restores_both_captured_values():
    state = {"status": "active", "previousWorkMode": 0, "previousTimerEnabled": False}
    decision = evaluate_event(_event("use_battery"), _READING, state, _OUT, TZ)
    assert decision.commands == [
        ("set_work_mode", {"workMode": 0}),
        ("set_grid_charge_toggle", {"gridChargeEnabled": False}),
    ]
    assert "lastCommandError" not in decision.new_state


def test_use_battery_restores_only_what_was_read():
    state = {"status": "active", "previousWorkMode": 1}
    decision = evaluate_event(_event("use_battery"), _READING, state, _OUT, TZ)
    assert decision.commands == [("set_work_mode", {"workMode": 1})]
    _no_prior_error(decision.new_state, "set_grid_charge_toggle")


def test_maintenance_activation_invents_no_prior_solar_sell():
    decision = evaluate_event(
        _event("battery_maintenance", {"gridFallbackHour": 23}), _READING, {}, _IN, TZ
    )
    assert decision.action == "activate"
    assert decision.commands == [("set_solar_sell", {"solarSell": 0})]
    assert "maintenancePriorSolarSell" not in decision.new_state


def test_maintenance_restores_the_captured_solar_sell():
    state = {"status": "active", "maintenancePriorSolarSell": True}
    decision = evaluate_event(_event("battery_maintenance"), _READING, state, _OUT, TZ)
    assert decision.commands == [("set_solar_sell", {"solarSell": 1})]
    assert "lastCommandError" not in decision.new_state


def test_maintenance_without_a_captured_solar_sell_writes_nothing():
    decision = evaluate_event(
        _event("battery_maintenance"), _READING, {"status": "active"}, _OUT, TZ
    )
    assert decision.commands == []
    _no_prior_error(decision.new_state, "set_solar_sell")


def test_maintenance_late_fallback_is_a_dispatchable_update():
    event = _event("battery_maintenance", {"gridFallbackHour": 10})
    state = {"status": "active", "maintenancePriorSolarSell": False}
    decision = evaluate_event(event, {"batterySoc": 60}, state, _IN, TZ)
    assert decision.action == "update"
    [(name, payload)] = decision.commands
    assert name == "set_battery_charge"
    assert payload["gridChargeEnabled"] is True
    assert payload["gridChargeSoc"] == 100
    assert decision.new_state["gridFallbackFired"] is True


def test_maintenance_fallback_already_written_holds():
    event = _event("battery_maintenance", {"gridFallbackHour": 10})
    state = {"status": "active", "gridFallbackFired": True, "prevSlots": _TABLE}
    decision = evaluate_event(event, {"batterySoc": 60}, state, _IN, TZ)
    assert decision.action == "hold"
    assert decision.commands == []


def test_maintenance_completion_hands_the_fallback_table_back():
    """As the API's endMaintenanceSlotHold at SOC 99: the run is over in-window."""
    event = _event("battery_maintenance", {"gridFallbackHour": 10})
    state = {"status": "active", "gridFallbackFired": True, "prevSlots": _TABLE}
    decision = evaluate_event(event, {"batterySoc": 99}, state, _IN, TZ)
    assert decision.action == "update"
    assert decision.commands == [("restore_battery_charge", {"prevSlots": _TABLE})]
    assert decision.new_state["completed"] is True
    assert "gridFallbackFired" not in decision.new_state
    assert "prevSlots" not in decision.new_state


def test_maintenance_completion_without_a_table_writes_nothing():
    event = _event("battery_maintenance", {"gridFallbackHour": 10})
    state = {"status": "active", "gridFallbackFired": True}
    decision = evaluate_event(event, {"batterySoc": 99}, state, _IN, TZ)
    assert decision.commands == []
    assert decision.new_state["completed"] is True
    assert "gridFallbackFired" not in decision.new_state
    _no_prior_error(decision.new_state, "restore_battery_charge")


def test_maintenance_completed_run_does_not_rearm_the_fallback():
    event = _event("battery_maintenance", {"gridFallbackHour": 10})
    state = {"status": "active", "completed": True}
    decision = evaluate_event(event, {"batterySoc": 97}, state, _IN, TZ)
    assert decision.action == "hold"
    assert decision.commands == []


def test_window_end_clears_the_run_markers_for_the_next_day():
    event = _event("battery_maintenance")
    state = {"status": "active", "completed": True, "maintenancePriorSolarSell": False}
    _cmds, new_state = restore_decision(event, state, _OUT)
    for key in ("completed", "gridFallbackFired", "maintenancePriorSolarSell", "prevSlots"):
        assert key not in new_state


def test_an_idle_event_with_no_landed_write_holds_at_window_end():
    """A partial activation's writes are put back (test_island_events_round4);
    an event that wrote nothing has nothing to restore."""
    state = {"status": "idle", "dispatchFailures": 3}
    decision = evaluate_event(_event("sell_to_grid"), _READING, state, _OUT, TZ)
    assert decision.action == "hold"
    assert decision.commands == []


# ═══════════════════════════ Scheduler ═══════════════════════════════════════


class _Store:
    def __init__(self, events):
        self.events = {e["id"]: dict(e) for e in events}

    async def async_list_events(self):
        return [dict(e) for e in self.events.values()]

    async def async_set_execution_state(self, event_id, state):
        if event_id in self.events:
            self.events[event_id]["executionState"] = dict(state)

    async def async_get_event(self, event_id):
        e = self.events.get(event_id)
        return dict(e) if e else None

    async def async_delete_event(self, event_id):
        return self.events.pop(event_id, None) is not None


class _Readings:
    def __init__(self, payload=None):
        self.payload = payload or _READING

    async def live_snapshot(self):
        return [{"inverterId": "inv-1", "payload": self.payload}]


class _Inverter:
    """A WriteExecutor stand-in with registers, reporting priors as WriteExecutor does."""

    def __init__(self, work_mode=1, timer=False, solar_sell=True, fail=()):
        self.regs = {
            "workMode": work_mode,
            "gridChargeEnabled": int(timer),
            "solarSell": int(solar_sell),
        }
        self.table = [dict(s) for s in _TABLE]
        self.fail = set(fail)
        self.calls: list[tuple[str, dict]] = []

    async def dispatch(self, name, payload):
        self.calls.append((name, dict(payload)))
        if name in self.fail:
            raise RuntimeError("write timeout")
        if name in ("set_battery_charge", "restore_battery_charge"):
            prev = [dict(s) for s in self.table]
            if "prevSlots" in payload:
                self.table = [dict(s) for s in payload["prevSlots"]]
            else:
                self.table[0].update(soc=payload["gridChargeSoc"], enable=1)
            return {"written": [], "verified": True, "prevSlots": prev}
        result = {"written": [], "verified": True}
        for field, value in payload.items():
            result["prev" + field[0].upper() + field[1:]] = self.regs[field]
            self.regs[field] = int(value)
        return result


def _executor_for(inv):
    return lambda _i: inv


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("sell_to_grid", {"workMode": 1, "gridChargeEnabled": 0, "solarSell": 1}),
        ("use_battery", {"workMode": 1, "gridChargeEnabled": 0, "solarSell": 1}),
    ],
)
async def test_window_end_puts_back_what_the_inverter_held(mode, expected):
    inv = _Inverter(work_mode=1, timer=False)
    store = _Store([_event(mode)])

    await _tick(_Readings(), store, _executor_for(inv), TZ, _IN)
    state = store.events["evt-1"]["executionState"]
    assert state["status"] == "active"
    assert state["previousWorkMode"] == 1
    if mode == "use_battery":
        assert state["previousTimerEnabled"] is False
        assert inv.regs["workMode"] == 2

    await _tick(_Readings(), store, _executor_for(inv), TZ, _OUT)
    assert inv.regs == expected
    state = store.events["evt-1"]["executionState"]
    assert state["status"] == "idle"
    assert "lastCommandError" not in state


@pytest.mark.asyncio
async def test_cancel_puts_back_what_the_inverter_held():
    inv = _Inverter(work_mode=1, timer=True)
    store = _Store([_event("use_battery")])
    await _tick(_Readings(), store, _executor_for(inv), TZ, _IN)
    assert inv.regs["workMode"] == 2 and inv.regs["gridChargeEnabled"] == 0

    assert await request_removal(store, "evt-1", "delete") == "pending"
    await settle_removal(store, _executor_for(inv), "evt-1", _IN + timedelta(minutes=1))

    assert inv.regs == {"workMode": 1, "gridChargeEnabled": 1, "solarSell": 1}
    assert "evt-1" not in store.events


@pytest.mark.asyncio
async def test_a_held_prior_is_not_replaced_by_a_retry_reading_our_own_write():
    """use_battery's first command lands and the second fails: the retry's
    set_work_mode reads 2 (our own write), which must not become the prior."""
    inv = _Inverter(work_mode=1, timer=True, fail={"set_grid_charge_toggle"})
    store = _Store([_event("use_battery")])
    await _tick(_Readings(), store, _executor_for(inv), TZ, _IN)
    state = store.events["evt-1"]["executionState"]
    assert state.get("status", "idle") != "active"
    assert state["previousWorkMode"] == 1

    inv.fail.clear()
    await _tick(_Readings(), store, _executor_for(inv), TZ, _IN + timedelta(minutes=2))
    state = store.events["evt-1"]["executionState"]
    assert state["status"] == "active"
    assert state["previousWorkMode"] == 1
    assert state["previousTimerEnabled"] is True

    await _tick(_Readings(), store, _executor_for(inv), TZ, _OUT)
    assert inv.regs["workMode"] == 1 and inv.regs["gridChargeEnabled"] == 1


@pytest.mark.asyncio
async def test_an_executor_reporting_no_prior_restores_nothing():
    """A YAML preset executor reports no prev* fields: nothing true to write."""
    inv = AsyncMock()
    inv.dispatch = AsyncMock(return_value={"ok": True})
    store = _Store([_event("sell_to_grid")])
    await _tick(_Readings(), store, _executor_for(inv), TZ, _IN)
    await _tick(_Readings(), store, _executor_for(inv), TZ, _OUT)

    assert inv.dispatch.await_count == 1
    state = store.events["evt-1"]["executionState"]
    assert state["status"] == "idle"
    _no_prior_error(state, "set_work_mode")


@pytest.mark.asyncio
async def test_a_late_maintenance_fallback_is_written_and_restored():
    inv = _Inverter(solar_sell=True)
    original_table = [dict(s) for s in inv.table]
    event = _event(
        "battery_maintenance",
        {"gridFallbackHour": 11},
        schedule={"startTime": "10:00", "endTime": "12:00", "recurrence": "daily"},
    )
    store = _Store([event])
    readings = _Readings({"batterySoc": 60})

    await _tick(readings, store, _executor_for(inv), TZ, _IN)  # 10:30, before the hour
    assert [c[0] for c in inv.calls] == ["set_solar_sell"]
    assert inv.regs["solarSell"] == 0

    await _tick(readings, store, _executor_for(inv), TZ, _IN + timedelta(minutes=45))  # 11:15
    assert [c[0] for c in inv.calls] == ["set_solar_sell", "set_battery_charge"]
    state = store.events["evt-1"]["executionState"]
    assert state["gridFallbackFired"] is True
    assert state["prevSlots"] == original_table
    assert inv.table != original_table

    # Not written again while it holds.
    await _tick(readings, store, _executor_for(inv), TZ, _IN + timedelta(minutes=50))
    assert len(inv.calls) == 2

    await _tick(readings, store, _executor_for(inv), TZ, _IN + timedelta(minutes=95))  # 12:05
    assert inv.calls[-2:] == [
        ("set_solar_sell", {"solarSell": 1}),
        ("restore_battery_charge", {"prevSlots": original_table}),
    ]
    assert inv.regs["solarSell"] == 1
    assert inv.table == original_table
    state = store.events["evt-1"]["executionState"]
    assert state["status"] == "idle"
    assert "gridFallbackFired" not in state


@pytest.mark.asyncio
async def test_a_failed_late_fallback_is_retried():
    inv = _Inverter(fail={"set_battery_charge"})
    event = _event(
        "battery_maintenance",
        {"gridFallbackHour": 10},
        state={"status": "active", "maintenancePriorSolarSell": True},
    )
    store = _Store([event])
    readings = _Readings({"batterySoc": 60})

    await _tick(readings, store, _executor_for(inv), TZ, _IN)
    state = store.events["evt-1"]["executionState"]
    assert "gridFallbackFired" not in state
    assert state["lastCommandError"]["command"] == "set_battery_charge"
    assert state["status"] == "active"

    inv.fail.clear()
    await _tick(readings, store, _executor_for(inv), TZ, _IN + timedelta(minutes=2))
    state = store.events["evt-1"]["executionState"]
    assert state["gridFallbackFired"] is True
    assert state["prevSlots"]
