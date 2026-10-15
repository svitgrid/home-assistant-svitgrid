"""Island scheduler, round 4: custom events, sell-to-grid floor and overlaps,
partial activations.

1. Custom events wrote nothing. The app stores a custom action as
   `{type, value}` (custom_action.dart, the API's CustomActionSchema), and the
   evaluator read `{command, payload, restorePayload}`, so an event showed
   active and emitted no command. Each action now maps to its command as the
   API's buildActivatePayload does, its prior value comes from the write's
   ACK, and the restore writes that value back.
3. sell_to_grid ignored `minSocPercent`, `sellPowerCapW` and
   `disableTouDuringEvent`, restored Selling First after sunset, and an event
   that started while another event held the same register took that event's
   write as the owner's value.
4. An activation that failed partway left the writes that landed on the
   inverter when its window ended or the event was cancelled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.svitgrid.harvest.event_evaluator import evaluate_event, restore_decision
from custom_components.svitgrid.harvest.event_scheduler_loop import (
    _tick,
    request_removal,
    settle_removal,
)
from tests.test_island_prior_restores import _TABLE, _event, _no_prior_error, _Readings, _Store

TZ = "UTC"
_IN = datetime(2026, 10, 15, 10, 30, tzinfo=UTC)
_OUT = datetime(2026, 10, 15, 11, 30, tzinfo=UTC)
_READING = {"batterySoc": 50, "pvPower": 2000, "gridPower": 200, "loadPower": 200}


class _Inverter:
    """Registers by payload field; reports `prev<Field>` like WriteExecutor."""

    def __init__(self, fail=(), **regs):
        self.regs = {
            "workMode": 1,
            "gridChargeEnabled": 0,
            "solarSell": 1,
            "sellPowerCapW": 9000,
            "on": 0,
            "genPortMode": 0,
            "energyPattern": 0,
        }
        self.regs.update(regs)
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
                i = payload["slotIndex"]
                self.table[i].update(soc=payload["gridChargeSoc"], enable=1)
            return {"written": [], "verified": True, "prevSlots": prev}
        result = {"written": [], "verified": True}
        for field, value in payload.items():
            if field in self.regs:
                result["prev" + field[0].upper() + field[1:]] = self.regs[field]
                self.regs[field] = int(value)
        return result


def _for(inv):
    return lambda _i: inv


_OWNER = {
    "workMode": 1,
    "gridChargeEnabled": 0,
    "solarSell": 1,
    "sellPowerCapW": 9000,
    "on": 0,
    "genPortMode": 0,
    "energyPattern": 0,
}

# Every register action of the app's catalog that a Deye recipe can write.
_APP_ACTIONS = [
    {"type": "work_mode", "value": 0},
    {"type": "solar_sell", "value": 0},
    {"type": "sell_power_cap", "valueW": 3000},
    {"type": "grid_charge_toggle", "enabled": True},
    {"type": "gen_force", "on": True},
    {"type": "gen_port_mode", "value": 2},
    {"type": "energy_pattern", "value": 1},
    {
        "type": "tou_slots",
        "slotIndex": 2,
        "startTime": "10:00",
        "endTime": "11:00",
        "powerW": 4000,
        "socTarget": 80,
        "gridChargeEnabled": True,
    },
]


def _custom(actions, conditions=None, state=None):
    return _event(
        "custom",
        {"customActions": actions, "customConditions": conditions or [], "sustainMinutes": 0},
        state,
    )


# ═══════════════════════════ 1. Custom events ═════════════════════════════════


def test_custom_app_actions_activate_the_api_commands():
    decision = evaluate_event(
        _custom(_APP_ACTIONS), _READING, {"status": "pending_condition"}, _IN, TZ
    )
    assert decision.action == "activate"
    assert decision.commands == [
        ("set_work_mode", {"workMode": 0}),
        ("set_solar_sell", {"solarSell": 0}),
        ("set_sell_power_cap", {"sellPowerCapW": 3000}),
        ("set_grid_charge_toggle", {"gridChargeEnabled": True}),
        ("set_gen_force", {"on": True}),
        ("set_gen_port_mode", {"genPortMode": 2}),
        ("set_energy_pattern", {"energyPattern": 1}),
        (
            "set_battery_charge",
            {
                "slotIndex": 2,
                "gridChargeEnabled": True,
                "gridChargeSoc": 80,
                "powerLimit": 4000,
                "slotStart": 1000,
                "slotEnd": 1100,
                "disableOtherSlots": False,
            },
        ),
    ]


def test_custom_actions_the_island_cannot_write_are_skipped():
    actions = [
        {"type": "smart_device", "deviceIds": ["d1"], "activeState": True, "restoreState": False},
        {"type": "grid_charge_current", "currentA": 40},
        {"type": "work_mode", "value": 0},
    ]
    decision = evaluate_event(_custom(actions), _READING, {"status": "pending_condition"}, _IN, TZ)
    assert decision.commands == [("set_work_mode", {"workMode": 0})]


def test_custom_app_actions_restore_the_captured_values():
    state = {
        "status": "active",
        "previousWorkMode": 1,
        "maintenancePriorSolarSell": True,
        "previousSellPowerCapW": 9000,
        "previousTimerEnabled": False,
        "previousGenForce": False,
        "previousGenPortMode": 0,
        "previousEnergyPattern": 0,
        "prevSlots": _TABLE,
    }
    commands, new_state = restore_decision(_custom(_APP_ACTIONS), state, _OUT)
    assert commands == [
        ("set_work_mode", {"workMode": 1}),
        ("set_solar_sell", {"solarSell": 1}),
        ("restore_sell_power_cap", {"sellPowerCapW": 9000}),
        ("set_grid_charge_toggle", {"gridChargeEnabled": False}),
        ("set_gen_force", {"on": False}),
        ("set_gen_port_mode", {"genPortMode": 0}),
        ("set_energy_pattern", {"energyPattern": 0}),
        ("restore_battery_charge", {"prevSlots": _TABLE}),
    ]
    assert "lastCommandError" not in new_state
    assert "previousWorkMode" not in new_state


def test_custom_app_action_without_a_captured_value_writes_nothing():
    state = {"status": "active", "previousWorkMode": 1}
    actions = [{"type": "work_mode", "value": 0}, {"type": "solar_sell", "value": 0}]
    commands, new_state = restore_decision(_custom(actions), state, _OUT)
    assert commands == [("set_work_mode", {"workMode": 1})]
    _no_prior_error(new_state, "set_solar_sell")


def test_legacy_custom_actions_keep_their_explicit_restore():
    actions = [
        {"command": "set_work_mode", "payload": {"workMode": 0}, "restorePayload": {"workMode": 2}}
    ]
    state = {"status": "active", "previousWorkMode": 1}
    commands, _ = restore_decision(_custom(actions), state, _OUT)
    assert commands == [("set_work_mode", {"workMode": 2})]


def test_legacy_custom_action_without_a_restore_payload_restores_the_captured_value():
    actions = [{"command": "set_work_mode", "payload": {"workMode": 0}}]
    commands, _ = restore_decision(
        _custom(actions), {"status": "active", "previousWorkMode": 1}, _OUT
    )
    assert commands == [("set_work_mode", {"workMode": 1})]


@pytest.mark.asyncio
async def test_custom_event_writes_and_puts_every_register_back():
    inv = _Inverter()
    original_table = [dict(s) for s in inv.table]
    conditions = [{"type": "battery_soc", "op": "gte", "threshold": 40}]
    store = _Store([_custom(_APP_ACTIONS, conditions)])
    readings = _Readings({"batterySoc": 50})

    await _tick(readings, store, _for(inv), TZ, _IN)  # idle -> pending
    await _tick(readings, store, _for(inv), TZ, _IN + timedelta(seconds=15))  # activate
    state = store.events["evt-1"]["executionState"]
    assert state["status"] == "active"
    assert inv.regs == {
        "workMode": 0,
        "gridChargeEnabled": 1,
        "solarSell": 0,
        "sellPowerCapW": 3000,
        "on": 1,
        "genPortMode": 2,
        "energyPattern": 1,
    }
    assert inv.table[2]["enable"] == 1

    readings.payload = {"batterySoc": 30}  # condition released
    await _tick(readings, store, _for(inv), TZ, _IN + timedelta(seconds=30))
    assert inv.regs == _OWNER
    assert inv.table == original_table
    state = store.events["evt-1"]["executionState"]
    assert state["status"] == "idle"
    assert "lastCommandError" not in state


# ═══════════════════════════ 3. sell_to_grid ══════════════════════════════════


def _sell(config=None, state=None):
    return _event("sell_to_grid", config or {}, state)


def test_sell_stops_at_the_battery_floor_and_restores():
    event = _sell({"minSocPercent": 30})
    state = {"status": "active", "previousWorkMode": 1}
    decision = evaluate_event(event, {"batterySoc": 30, "pvPower": 2000}, state, _IN, TZ)
    assert decision.action == "deactivate"
    assert decision.commands == [("set_work_mode", {"workMode": 1})]
    assert decision.new_state["status"] == "pending_condition"
    assert decision.new_state["belowMinSoc"] is True


def test_sell_at_the_floor_does_not_restart_until_floor_plus_5():
    event = _sell({"minSocPercent": 30})
    held = {
        "status": "pending_condition",
        "belowMinSoc": True,
        "previousWorkMode": 1,
        "restoredInWindow": True,
    }
    for soc in (31, 34):
        decision = evaluate_event(event, {"batterySoc": soc, "pvPower": 2000}, held, _IN, TZ)
        assert decision.action == "hold", soc
        assert decision.commands == []
    decision = evaluate_event(event, {"batterySoc": 35, "pvPower": 2000}, held, _IN, TZ)
    assert decision.action == "activate"
    assert decision.commands == [("set_work_mode", {"workMode": 0})]
    assert decision.new_state["belowMinSoc"] is False


def test_sell_below_the_floor_does_not_activate():
    decision = evaluate_event(_sell({"minSocPercent": 30}), {"batterySoc": 25}, {}, _IN, TZ)
    assert decision.action == "hold"
    assert decision.commands == []


def test_sell_writes_the_cap_and_turns_tou_off_and_puts_both_back():
    event = _sell({"sellPowerCapW": 5000, "disableTouDuringEvent": True})
    decision = evaluate_event(event, _READING, {}, _IN, TZ)
    assert decision.commands == [
        ("set_work_mode", {"workMode": 0}),
        ("set_sell_power_cap", {"sellPowerCapW": 5000}),
        ("set_grid_charge_toggle", {"gridChargeEnabled": False}),
    ]
    state = {
        "status": "active",
        "previousWorkMode": 2,
        "previousSellPowerCapW": 12000,
        "previousTimerEnabled": True,
    }
    commands, _ = restore_decision(event, state, _OUT, reading=_READING)
    assert commands == [
        ("set_work_mode", {"workMode": 2}),
        ("restore_sell_power_cap", {"sellPowerCapW": 12000}),
        ("set_grid_charge_toggle", {"gridChargeEnabled": True}),
    ]


@pytest.mark.parametrize(("pv", "restored"), [(100, 1), (499, 1), (500, 0), (None, 0)])
def test_sell_does_not_restore_selling_first_after_sunset(pv, restored):
    """sunset-restore-guard.ts (#576): Selling First with no sun sells the battery."""
    state = {"status": "active", "previousWorkMode": 0}
    reading = {"batterySoc": 60} if pv is None else {"batterySoc": 60, "pvPower": pv}
    decision = evaluate_event(_sell(), reading, state, _OUT, TZ)
    assert decision.commands == [("set_work_mode", {"workMode": restored})]


@pytest.mark.asyncio
async def test_sell_floor_through_the_scheduler_puts_the_mode_back_and_resumes():
    inv = _Inverter(workMode=1)
    store = _Store([_sell({"minSocPercent": 30})])
    readings = _Readings({"batterySoc": 60, "pvPower": 2000})
    await _tick(readings, store, _for(inv), TZ, _IN)
    assert inv.regs["workMode"] == 0

    readings.payload = {"batterySoc": 29, "pvPower": 2000}
    await _tick(readings, store, _for(inv), TZ, _IN + timedelta(minutes=1))
    assert inv.regs["workMode"] == 1

    readings.payload = {"batterySoc": 36, "pvPower": 2000}
    await _tick(readings, store, _for(inv), TZ, _IN + timedelta(minutes=2))
    assert inv.regs["workMode"] == 0
    state = store.events["evt-1"]["executionState"]
    assert state["previousWorkMode"] == 1  # still the owner's

    await _tick(readings, store, _for(inv), TZ, _OUT)
    assert inv.regs["workMode"] == 1


def _two(first: dict, second: dict) -> _Store:
    first = dict(first, id="evt-a")
    second = dict(second, id="evt-b")
    return _Store([first, second])


@pytest.mark.asyncio
async def test_an_overlapping_event_takes_the_owners_value_not_the_other_events_write():
    """Event B starts while A holds work mode 0: B's own read says 0, but the
    owner's value is A's prior, 1 (the API's sell-to-grid handoff)."""
    inv = _Inverter(workMode=1)
    late = {"startTime": "10:15", "endTime": "11:15", "recurrence": "daily"}
    store = _two(_sell(), _event("sell_to_grid", schedule=late))

    await _tick(_Readings(_READING), store, _for(inv), TZ, _IN.replace(minute=5))
    assert store.events["evt-a"]["executionState"]["previousWorkMode"] == 1
    await _tick(_Readings(_READING), store, _for(inv), TZ, _IN)
    assert store.events["evt-b"]["executionState"]["previousWorkMode"] == 1


@pytest.mark.asyncio
async def test_an_ending_event_does_not_clobber_an_event_still_active():
    """use_battery (work mode 2) runs over a shorter sell_to_grid (0). When the
    sale ends, the inverter goes back to use_battery's 2, not to the owner's
    1; when use_battery ends, to the owner's 1."""
    inv = _Inverter(workMode=1, gridChargeEnabled=1)
    long = {"startTime": "10:00", "endTime": "12:00", "recurrence": "daily"}
    store = _two(_event("use_battery", schedule=long), _sell())

    await _tick(_Readings(_READING), store, _for(inv), TZ, _IN)
    assert inv.regs["workMode"] in (0, 2)
    assert store.events["evt-b"]["executionState"]["previousWorkMode"] == 1
    assert store.events["evt-a"]["executionState"]["previousWorkMode"] == 1

    await _tick(_Readings(_READING), store, _for(inv), TZ, _OUT)  # 11:30, sale over
    assert inv.regs["workMode"] == 2
    assert inv.regs["gridChargeEnabled"] == 0

    await _tick(_Readings(_READING), store, _for(inv), TZ, _OUT + timedelta(hours=1))
    assert inv.regs["workMode"] == 1
    assert inv.regs["gridChargeEnabled"] == 1


# ═══════════════════════════ 4. Partial activation ════════════════════════════


def test_window_end_after_a_partial_activation_restores_what_landed():
    state = {"status": "idle", "previousWorkMode": 1, "dispatchFailures": 3}
    decision = evaluate_event(_event("use_battery"), _READING, state, _OUT, TZ)
    assert decision.action == "deactivate"
    assert decision.commands == [("set_work_mode", {"workMode": 1})]
    # The toggle never landed: there is nothing to report missing.
    assert "lastCommandError" not in decision.new_state
    assert "previousWorkMode" not in decision.new_state


@pytest.mark.asyncio
async def test_partial_activation_is_undone_at_window_end():
    inv = _Inverter(workMode=1, gridChargeEnabled=1, fail={"set_grid_charge_toggle"})
    store = _Store([_event("use_battery")])
    await _tick(_Readings(_READING), store, _for(inv), TZ, _IN)
    assert inv.regs["workMode"] == 2
    assert store.events["evt-1"]["executionState"].get("status", "idle") != "active"

    await _tick(_Readings(_READING), store, _for(inv), TZ, _OUT)
    assert inv.regs["workMode"] == 1
    assert inv.calls[-1] == ("set_work_mode", {"workMode": 1})
    state = store.events["evt-1"]["executionState"]
    assert state["status"] == "idle"
    assert "previousWorkMode" not in state


@pytest.mark.asyncio
async def test_cancelling_a_partial_activation_undoes_it():
    inv = _Inverter(workMode=1, gridChargeEnabled=1, fail={"set_grid_charge_toggle"})
    store = _Store([_event("use_battery")])
    await _tick(_Readings(_READING), store, _for(inv), TZ, _IN)
    assert inv.regs["workMode"] == 2

    assert await request_removal(store, "evt-1", "delete") == "pending"
    await settle_removal(store, _for(inv), "evt-1", _IN + timedelta(minutes=5))
    assert inv.regs["workMode"] == 1
    assert "evt-1" not in store.events


@pytest.mark.asyncio
async def test_custom_partial_activation_is_undone_when_its_condition_goes():
    inv = _Inverter(workMode=1, fail={"set_solar_sell"})
    conditions = [{"type": "battery_soc", "op": "gte", "threshold": 40}]
    actions = [{"type": "work_mode", "value": 0}, {"type": "solar_sell", "value": 0}]
    store = _Store([_custom(actions, conditions)])
    readings = _Readings({"batterySoc": 50})
    await _tick(readings, store, _for(inv), TZ, _IN)
    await _tick(readings, store, _for(inv), TZ, _IN + timedelta(seconds=15))
    assert inv.regs["workMode"] == 0

    readings.payload = {"batterySoc": 30}
    await _tick(readings, store, _for(inv), TZ, _IN + timedelta(minutes=5))
    assert inv.regs["workMode"] == 1
    state = store.events["evt-1"]["executionState"]
    assert "previousWorkMode" not in state


def test_cancel_of_an_event_that_never_wrote_is_immediate():
    """No prior held: nothing at the inverter, so the delete needs no restore."""
    commands, _ = restore_decision(_event("use_battery"), {"status": "idle"}, _OUT)
    assert commands == []


def test_a_prior_held_for_a_write_the_edited_config_dropped_is_still_restored():
    """The owner removed the sell cap while the sale ran: the cap the event
    wrote is still on the inverter, and its prior is still held."""
    state = {"status": "active", "previousWorkMode": 1, "previousSellPowerCapW": 9000}
    commands, _ = restore_decision(_sell(), state, _OUT)
    assert commands == [
        ("set_work_mode", {"workMode": 1}),
        ("restore_sell_power_cap", {"sellPowerCapW": 9000}),
    ]
