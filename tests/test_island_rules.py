"""Rules on a true island (owner, 2026-10-15).

A rule is a custom calendar event with `schedule: {recurrence: "always"}`
(the app's «Постійно»), or a custom event with hours from the Rules tab.
These tests pin what the API's rules path does and the island now does too:
the run itself, the 15-minute stale-reading guard, the empty-rule skip, the
grid_voltage condition, the run timestamps the app shows as history, and
the precedence between a rule and a windowed event on one register.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.svitgrid.harvest.event_evaluator import evaluate_event, restore_decision

TZ = "Europe/Kyiv"
_NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)  # 15:00 Kyiv
_ALWAYS = {"recurrence": "always"}
_ALL_DAY = {"recurrence": "daily", "startTime": "00:00", "endTime": "23:59"}
_EVENING = {"recurrence": "daily", "startTime": "20:00", "endTime": "22:00"}


def _ago(minutes: float) -> str:
    return (_NOW - timedelta(minutes=minutes)).isoformat()


def _rule(conditions, actions=None, sustain=0, **extra) -> dict:
    return {
        "id": extra.pop("id", "rule-1"),
        "inverterId": "inv-1",
        "mode": "custom",
        "enabled": True,
        "schedule": extra.pop("schedule", _ALWAYS),
        "config": {
            "customConditions": conditions,
            "customActions": [{"type": "grid_charge_toggle", "enabled": True}]
            if actions is None
            else actions,
            "sustainMinutes": sustain,
            **extra.pop("config", {}),
        },
        **extra,
    }


_LOW_SOC = [{"type": "battery_soc", "op": "lte", "threshold": 30, "releaseThreshold": 40}]


def _reading(**fields) -> dict:
    return {"timestamp": _ago(0.2), "batterySoc": 25, "pvPower": 0, "loadPower": 500, **fields}


# ── A rule runs ────────────────────────────────────────────────────────────────


def test_a_rule_arms_and_activates():
    rule = _rule(_LOW_SOC, sustain=0)
    armed = evaluate_event(rule, _reading(), {"status": "idle"}, _NOW, TZ)
    assert armed.action == "hold"
    assert armed.new_state["status"] == "pending_condition"
    fired = evaluate_event(rule, _reading(), armed.new_state, _NOW, TZ)
    assert fired.action == "activate"
    assert fired.commands == [("set_grid_charge_toggle", {"gridChargeEnabled": True})]


def test_a_rule_releases_and_rearms():
    rule = _rule(_LOW_SOC)
    active = {"status": "active", "lastActivatedAt": _ago(30), "previousTimerEnabled": False}
    released = evaluate_event(rule, _reading(batterySoc=45), active, _NOW, TZ)
    assert released.action == "deactivate"
    assert released.commands == [("set_grid_charge_toggle", {"gridChargeEnabled": False})]
    assert released.new_state["status"] == "idle"
    again = evaluate_event(rule, _reading(batterySoc=20), released.new_state, _NOW, TZ)
    assert again.new_state["status"] == "pending_condition"


# ── Stale reading guard (evaluator.ts evaluateCustom, 15 min) ─────────────────


def test_a_stale_reading_changes_nothing():
    rule = _rule(_LOW_SOC)
    stale = _reading(timestamp=_ago(16))
    decision = evaluate_event(rule, stale, {"status": "idle"}, _NOW, TZ)
    assert decision.action == "hold"
    assert decision.commands == []
    assert decision.new_state == {"status": "idle"}


def test_a_stale_reading_does_not_release_an_active_rule():
    rule = _rule(_LOW_SOC)
    active = {"status": "active", "previousTimerEnabled": False}
    decision = evaluate_event(rule, _reading(timestamp=_ago(20), batterySoc=90), active, _NOW, TZ)
    assert decision.action == "hold"
    assert decision.new_state["status"] == "active"


def test_a_stale_reading_still_runs_a_grid_only_rule():
    """Only soc, pv, load and voltage conditions read the reading's numbers."""
    rule = _rule([{"type": "grid", "state": "up"}])
    decision = evaluate_event(
        rule, _reading(timestamp=_ago(30), gridRelayClosed=True), {"status": "idle"}, _NOW, TZ
    )
    assert decision.new_state["status"] == "pending_condition"


# ── A rule with nothing to do ─────────────────────────────────────────────────


def test_a_rule_with_no_setting_and_no_notification_does_nothing():
    rule = _rule(_LOW_SOC, actions=[])
    decision = evaluate_event(rule, _reading(), {"status": "idle"}, _NOW, TZ)
    assert decision.action == "hold"
    assert decision.new_state == {"status": "idle"}


# ── grid_voltage (custom-event-decision.ts) ───────────────────────────────────

_SAG = [{"type": "grid_voltage", "op": "lte", "thresholdV": 200, "releaseThresholdV": 210}]


def test_a_voltage_sag_arms_on_the_lowest_phase():
    rule = _rule(_SAG)
    decision = evaluate_event(
        rule, _reading(phaseVoltages=[231, 198, 229]), {"status": "idle"}, _NOW, TZ
    )
    assert decision.new_state["status"] == "pending_condition"


def test_a_voltage_sag_releases_above_the_release_threshold():
    rule = _rule(_SAG)
    active = {"status": "active", "previousTimerEnabled": False}
    held = evaluate_event(rule, _reading(phaseVoltages=[205, 209, 230]), active, _NOW, TZ)
    assert held.action == "hold"
    released = evaluate_event(rule, _reading(phaseVoltages=[215, 220, 230]), active, _NOW, TZ)
    assert released.action == "deactivate"


def test_a_high_voltage_rule_reads_the_highest_phase():
    rule = _rule([{"type": "grid_voltage", "op": "gte", "thresholdV": 250}])
    decision = evaluate_event(rule, _reading(gridVoltage=253), {"status": "idle"}, _NOW, TZ)
    assert decision.new_state["status"] == "pending_condition"


def test_no_voltage_holds_the_latch():
    """Silence is not a sag, and it never clears a latch."""
    rule = _rule(_SAG)
    idle = evaluate_event(rule, _reading(phaseVoltages=[0, None]), {"status": "idle"}, _NOW, TZ)
    assert idle.new_state["status"] == "idle"
    active = {"status": "active", "previousTimerEnabled": False}
    still = evaluate_event(rule, _reading(), active, _NOW, TZ)
    assert still.action == "hold"
    assert still.new_state["status"] == "active"


# ── Run timestamps: what the app shows as a rule's history ────────────────────


def test_activation_and_release_are_stamped():
    rule = _rule(_LOW_SOC)
    fired = evaluate_event(rule, _reading(), {"status": "pending_condition"}, _NOW, TZ)
    assert fired.new_state["lastActivatedAt"] == _NOW.isoformat()
    later = _NOW + timedelta(minutes=45)
    state = {**fired.new_state, "previousTimerEnabled": False}
    released = evaluate_event(
        rule, {**_reading(batterySoc=60), "timestamp": later.isoformat()}, state, later, TZ
    )
    assert released.action == "deactivate"
    assert released.new_state["lastActivatedAt"] == _NOW.isoformat()
    assert released.new_state["lastDeactivatedAt"] == later.isoformat()


def test_a_window_end_keeps_the_activation_time():
    event = {
        "id": "ev",
        "mode": "use_battery",
        "schedule": _EVENING,
        "config": {},
    }
    state = {
        "status": "active",
        "lastActivatedAt": _ago(90),
        "previousWorkMode": 1,
        "previousTimerEnabled": True,
    }
    ended = evaluate_event(event, _reading(), state, _NOW, TZ)
    assert ended.action == "deactivate"
    assert ended.new_state["lastActivatedAt"] == _ago(90)
    assert ended.new_state["lastDeactivatedAt"] == _NOW.isoformat()


def test_a_cancel_stamps_the_release():
    rule = _rule(_LOW_SOC)
    state = {"status": "active", "lastActivatedAt": _ago(5), "previousTimerEnabled": False}
    _commands, done = restore_decision(rule, state, _NOW)
    assert done["lastDeactivatedAt"] == _NOW.isoformat()
    assert done["lastActivatedAt"] == _ago(5)


# ── A windowed event pauses a rule on its register (rule-pause.ts) ────────────


def _charge_window(**extra) -> dict:
    """A windowed custom event on the TOU master bit, in window now."""
    return {
        "id": extra.pop("id", "event-1"),
        "inverterId": "inv-1",
        "mode": "custom",
        "enabled": True,
        "schedule": _ALL_DAY,
        "config": {
            "customConditions": [],
            "customActions": [{"type": "grid_charge_toggle", "enabled": False}],
            "sustainMinutes": 0,
        },
        "executionState": extra.pop("executionState", {"status": "idle"}),
        **extra,
    }


def test_an_in_window_event_pauses_an_active_rule_and_takes_its_restore():
    rule = _rule(_LOW_SOC)
    active = {"status": "active", "lastActivatedAt": _ago(30), "previousTimerEnabled": True}
    decision = evaluate_event(rule, _reading(), active, _NOW, TZ, others=[_charge_window()])
    assert decision.action == "deactivate"
    assert decision.commands == [("set_grid_charge_toggle", {"gridChargeEnabled": True})]
    assert decision.new_state["status"] == "idle"
    assert decision.new_state["pausedBy"] == "event-1"


def test_a_paused_rule_stays_idle_while_the_event_holds():
    rule = _rule(_LOW_SOC)
    paused = {"status": "idle", "pausedBy": "event-1"}
    event = _charge_window(executionState={"status": "active", "previousTimerEnabled": True})
    decision = evaluate_event(rule, _reading(), paused, _NOW, TZ, others=[event])
    assert decision.action == "hold"
    assert decision.new_state["status"] == "idle"
    assert decision.new_state["pausedBy"] == "event-1"


def test_a_paused_rule_rearms_when_the_window_ends():
    rule = _rule(_LOW_SOC)
    paused = {"status": "idle", "pausedBy": "event-1"}
    event = _charge_window(schedule=_EVENING)
    decision = evaluate_event(rule, _reading(), paused, _NOW, TZ, others=[event])
    assert "pausedBy" not in decision.new_state
    assert decision.new_state["status"] == "pending_condition"


def test_an_event_on_another_register_pauses_nothing():
    rule = _rule(_LOW_SOC)
    event = _charge_window()
    event["config"]["customActions"] = [{"type": "work_mode", "value": 2}]
    decision = evaluate_event(rule, _reading(), {"status": "idle"}, _NOW, TZ, others=[event])
    assert decision.new_state["status"] == "pending_condition"


def test_a_disabled_or_cloud_event_pauses_nothing():
    rule = _rule(_LOW_SOC)
    disabled = _charge_window(enabled=False)
    cloud = _charge_window(id="event-2")
    cloud["config"]["customConditions"] = [
        {"type": "forecast_today", "op": "gte", "thresholdKwh": 10}
    ]
    decision = evaluate_event(
        rule, _reading(), {"status": "idle"}, _NOW, TZ, others=[disabled, cloud]
    )
    assert decision.new_state["status"] == "pending_condition"


def test_a_rule_the_owner_placed_above_the_event_is_not_paused():
    rule = _rule(_LOW_SOC, sortOrder=0)
    event = _charge_window(sortOrder=1)
    decision = evaluate_event(rule, _reading(), {"status": "idle"}, _NOW, TZ, others=[event])
    assert decision.new_state["status"] == "pending_condition"


def test_the_event_waits_while_the_rule_it_pauses_still_holds_the_register():
    """The rule restores first, then the event reads the owner's value."""
    event = _charge_window()
    rule = _rule(_LOW_SOC)
    rule["executionState"] = {"status": "active", "previousTimerEnabled": True}
    decision = evaluate_event(event, _reading(), {"status": "idle"}, _NOW, TZ, others=[rule])
    assert decision.action == "hold"
    assert decision.commands == []
    assert decision.new_state.get("status", "idle") == "idle"


def test_the_event_stands_down_for_a_rule_placed_above_it():
    event = _charge_window(sortOrder=1)
    rule = _rule(_LOW_SOC, sortOrder=0)
    rule["executionState"] = {"status": "active", "previousTimerEnabled": True}
    decision = evaluate_event(event, _reading(), {"status": "idle"}, _NOW, TZ, others=[rule])
    assert decision.action == "hold"
    assert decision.new_state["skipReason"] == "paused_by_event:rule-1"


def test_the_event_runs_once_the_rule_has_restored():
    event = _charge_window()
    rule = _rule(_LOW_SOC)
    rule["executionState"] = {"status": "idle", "pausedBy": "event-1"}
    decision = evaluate_event(event, _reading(), {"status": "idle"}, _NOW, TZ, others=[rule])
    assert decision.new_state["status"] in ("pending_condition", "active")
    assert "skipReason" not in decision.new_state


# ── Through the scheduler ─────────────────────────────────────────────────────

from unittest.mock import AsyncMock

import pytest

from custom_components.svitgrid.harvest.event_scheduler_loop import _tick
from tests.test_event_scheduler_loop import FakeEventStore, FakeStore


@pytest.mark.asyncio
async def test_the_scheduler_runs_a_rule():
    """Before the 'always' window it logged a KeyError every tick instead."""
    rule = _rule(_LOW_SOC)
    event_store = FakeEventStore([rule])
    executor = AsyncMock()
    executor.dispatch.return_value = {"prevGridChargeEnabled": False}
    snapshot = [{"inverterId": "inv-1", "ts": _NOW.isoformat(), "payload": _reading()}]

    await _tick(FakeStore(snapshot), event_store, lambda _i: executor, TZ, _NOW)
    await _tick(FakeStore(snapshot), event_store, lambda _i: executor, TZ, _NOW)

    executor.dispatch.assert_awaited_with("set_grid_charge_toggle", {"gridChargeEnabled": True})
    [stored] = await event_store.async_list_events()
    assert stored["executionState"]["status"] == "active"
    assert stored["executionState"]["lastActivatedAt"] == _NOW.isoformat()
