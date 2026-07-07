"""TDD tests for harvest/event_evaluator.evaluate_event (Task 4).

Per-mode local condition evaluator with activation guards.
One+ test per mode; covers:
  - condition met in-window → activate + correct (command, payload)
  - already-active + still-met → hold (no re-dispatch)
  - condition drops / out-of-window while active → deactivate + restore
  - gen_force requireScheduledOutage → skip:requires_scheduled_outage
  - custom with forecast_today condition → skip:cloud_only_condition
  - day_planning → skip:cloud_only_mode
  - smart-mode / sustain hysteresis prevents flapping
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.svitgrid.harvest.event_evaluator import (
    evaluate_event,
)

# ──────────────────────────── Helpers ─────────────────────────────────────────

TZ = "Europe/Kyiv"
_NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)  # 15:00 Kyiv (UTC+3)


def _ago(minutes: float) -> str:
    return (_NOW - timedelta(minutes=minutes)).isoformat()


def _in_window_schedule() -> dict:
    """Daily all-day window — always in-window."""
    return {
        "recurrence": "daily",
        "startTime": "00:00",
        "endTime": "23:59",
        "startDate": "2026-01-01",
    }


def _out_window_schedule() -> dict:
    """20:00–22:00 Kyiv (UTC+3) — 12:00 UTC is outside."""
    return {"recurrence": "daily", "startTime": "20:00", "endTime": "22:00"}


def _idle() -> dict:
    return {"status": "idle"}


def _active(since_minutes: float = 60.0, **extra) -> dict:
    return {"status": "active", "lastActivatedAt": _ago(since_minutes), **extra}


# ══════════════════════════════════════════════════════════════════════════════
# cloud-only skip guards
# ══════════════════════════════════════════════════════════════════════════════


def test_day_planning_skips():
    event = {"mode": "day_planning", "schedule": _in_window_schedule(), "config": {}}
    result = evaluate_event(event, {}, _idle(), _NOW, TZ)
    assert result.action == "skip"
    assert result.skip_reason == "cloud_only_mode"


def test_gen_force_require_scheduled_outage_skips():
    event = {
        "mode": "gen_force",
        "schedule": _in_window_schedule(),
        "config": {"requireScheduledOutage": True},
    }
    result = evaluate_event(event, {"batterySoc": 20}, _idle(), _NOW, TZ)
    assert result.action == "skip"
    assert result.skip_reason == "requires_scheduled_outage"


def test_custom_forecast_today_condition_skips():
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "forecast_today", "op": "gte", "thresholdKwh": 10}],
            "customActions": [{"command": "set_work_mode", "payload": {"workMode": 0}}],
        },
    }
    result = evaluate_event(event, {"batterySoc": 80}, _idle(), _NOW, TZ)
    assert result.action == "skip"
    assert result.skip_reason == "cloud_only_condition"


def test_custom_dam_price_condition_skips():
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "dam_price", "op": "lte", "thresholdUahKwh": 3.0}],
            "customActions": [{"command": "set_work_mode", "payload": {"workMode": 0}}],
        },
    }
    result = evaluate_event(event, {"batterySoc": 80}, _idle(), _NOW, TZ)
    assert result.action == "skip"
    assert result.skip_reason == "cloud_only_condition"


def test_custom_scheduled_outage_condition_skips():
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "scheduled_outage", "inWindow": True}],
            "customActions": [{"command": "set_gen_force", "payload": {"on": True}}],
        },
    }
    result = evaluate_event(event, {"batterySoc": 80}, _idle(), _NOW, TZ)
    assert result.action == "skip"
    assert result.skip_reason == "cloud_only_condition"


# ══════════════════════════════════════════════════════════════════════════════
# out-of-window handling
# ══════════════════════════════════════════════════════════════════════════════


def test_out_of_window_idle_returns_hold():
    event = {"mode": "sell_to_grid", "schedule": _out_window_schedule(), "config": {}}
    result = evaluate_event(event, {}, _idle(), _NOW, TZ)
    assert result.action == "hold"
    assert result.commands == []


def test_out_of_window_active_returns_deactivate():
    event = {
        "mode": "sell_to_grid",
        "schedule": _out_window_schedule(),
        "config": {},
    }
    state = _active(previousWorkMode=2)
    result = evaluate_event(event, {}, state, _NOW, TZ)
    assert result.action == "deactivate"
    assert result.new_state["status"] == "idle"


# ══════════════════════════════════════════════════════════════════════════════
# battery_charge
# ══════════════════════════════════════════════════════════════════════════════


def test_battery_charge_activate_in_window():
    event = {
        "mode": "battery_charge",
        "schedule": _in_window_schedule(),
        "config": {"chargeSource": "grid", "targetSoc": 90},
    }
    result = evaluate_event(event, {"batterySoc": 50}, _idle(), _NOW, TZ)
    assert result.action == "activate"
    assert result.new_state["status"] == "active"
    cmds = dict(result.commands)
    assert "set_battery_charge" in cmds
    p = cmds["set_battery_charge"]
    assert p["gridChargeEnabled"] is True
    assert p["gridChargeSoc"] == 90
    assert p["slotIndex"] == 0
    assert p["disableOtherSlots"] is True


def test_battery_charge_hold_when_active():
    event = {
        "mode": "battery_charge",
        "schedule": _in_window_schedule(),
        "config": {"chargeSource": "grid", "targetSoc": 90},
    }
    result = evaluate_event(event, {"batterySoc": 50}, _active(), _NOW, TZ)
    assert result.action == "hold"
    assert result.commands == []


def test_battery_charge_deactivate_out_of_window():
    event = {
        "mode": "battery_charge",
        "schedule": _out_window_schedule(),
        "config": {"chargeSource": "grid", "targetSoc": 90},
    }
    result = evaluate_event(event, {"batterySoc": 50}, _active(), _NOW, TZ)
    assert result.action == "deactivate"
    assert result.new_state["status"] == "idle"
    # restore command disables the slot
    cmd_names = [c[0] for c in result.commands]
    assert "set_battery_charge" in cmd_names


def test_battery_charge_solar_source_no_grid_charge():
    event = {
        "mode": "battery_charge",
        "schedule": _in_window_schedule(),
        "config": {"chargeSource": "solar", "targetSoc": 80},
    }
    result = evaluate_event(event, {}, _idle(), _NOW, TZ)
    assert result.action == "activate"
    p = dict(result.commands)["set_battery_charge"]
    assert p["gridChargeEnabled"] is False


# ══════════════════════════════════════════════════════════════════════════════
# sell_to_grid
# ══════════════════════════════════════════════════════════════════════════════


def test_sell_to_grid_simple_activate():
    event = {
        "mode": "sell_to_grid",
        "schedule": _in_window_schedule(),
        "config": {"sellMode": "simple"},
    }
    result = evaluate_event(event, {"pvPower": 2000}, _idle(), _NOW, TZ)
    assert result.action == "activate"
    assert result.new_state["status"] == "active"
    cmds = dict(result.commands)
    assert "set_work_mode" in cmds
    assert cmds["set_work_mode"]["workMode"] == 0


def test_sell_to_grid_simple_hold_when_active():
    event = {
        "mode": "sell_to_grid",
        "schedule": _in_window_schedule(),
        "config": {"sellMode": "simple"},
    }
    result = evaluate_event(event, {"pvPower": 2000}, _active(), _NOW, TZ)
    assert result.action == "hold"
    assert result.commands == []


def test_sell_to_grid_simple_deactivate_out_of_window():
    event = {
        "mode": "sell_to_grid",
        "schedule": _out_window_schedule(),
        "config": {"sellMode": "simple"},
    }
    state = _active(previousWorkMode=2)
    result = evaluate_event(event, {}, state, _NOW, TZ)
    assert result.action == "deactivate"
    cmds = dict(result.commands)
    assert "set_work_mode" in cmds
    assert cmds["set_work_mode"]["workMode"] == 2


def test_sell_to_grid_smart_activate_pv_above_threshold():
    event = {
        "mode": "sell_to_grid",
        "schedule": _in_window_schedule(),
        "config": {"sellMode": "smart", "pvThresholdW": 500},
    }
    result = evaluate_event(event, {"pvPower": 1200}, _idle(), _NOW, TZ)
    assert result.action == "activate"
    cmds = dict(result.commands)
    assert cmds["set_work_mode"]["workMode"] == 0


def test_sell_to_grid_smart_hold_pv_below_threshold():
    event = {
        "mode": "sell_to_grid",
        "schedule": _in_window_schedule(),
        "config": {"sellMode": "smart", "pvThresholdW": 500},
    }
    result = evaluate_event(event, {"pvPower": 100}, _idle(), _NOW, TZ)
    assert result.action == "hold"
    assert result.commands == []


def test_sell_to_grid_smart_hysteresis_prevents_immediate_deactivate():
    """A brief dip below pvThreshold does NOT immediately deactivate (2-min hysteresis)."""
    event = {
        "mode": "sell_to_grid",
        "schedule": _in_window_schedule(),
        "config": {"sellMode": "smart", "pvThresholdW": 500},
    }
    # Active — PV just dropped (conditionLostSince = 30 sec ago, within 2-min window)
    state = _active(conditionLostSince=_ago(0.5))
    result = evaluate_event(event, {"pvPower": 100}, state, _NOW, TZ)
    assert result.action == "hold", "should hold during 2-min hysteresis window"


def test_sell_to_grid_smart_deactivates_after_hysteresis():
    """After 2+ minutes below pvThreshold, deactivate."""
    event = {
        "mode": "sell_to_grid",
        "schedule": _in_window_schedule(),
        "config": {"sellMode": "smart", "pvThresholdW": 500},
    }
    # PV has been below threshold for 3 minutes
    state = _active(previousWorkMode=2, conditionLostSince=_ago(3))
    result = evaluate_event(event, {"pvPower": 100}, state, _NOW, TZ)
    assert result.action == "deactivate"
    cmds = dict(result.commands)
    assert cmds["set_work_mode"]["workMode"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# lower_consumption
# ══════════════════════════════════════════════════════════════════════════════


def test_lower_consumption_skips_device_control_when_soc_low():
    """lower_consumption actions are smart-device-only; island WriteExecutor can't
    perform them → skip:device_control_unavailable instead of fake-activate."""
    event = {
        "mode": "lower_consumption",
        "schedule": _in_window_schedule(),
        "config": {"socThreshold": 30},
    }
    result = evaluate_event(event, {"batterySoc": 20}, _idle(), _NOW, TZ)
    assert result.action == "skip"
    assert result.skip_reason == "device_control_unavailable"
    # State stays idle — no spurious activate→deactivate lifecycle
    assert result.new_state.get("status", "idle") == "idle"


def test_lower_consumption_hold_when_active_and_soc_still_low():
    event = {
        "mode": "lower_consumption",
        "schedule": _in_window_schedule(),
        "config": {"socThreshold": 30},
    }
    result = evaluate_event(event, {"batterySoc": 20}, _active(), _NOW, TZ)
    assert result.action == "hold"


def test_lower_consumption_deactivate_when_soc_recovers():
    event = {
        "mode": "lower_consumption",
        "schedule": _in_window_schedule(),
        "config": {"socThreshold": 30},
    }
    result = evaluate_event(event, {"batterySoc": 60}, _active(), _NOW, TZ)
    assert result.action == "deactivate"
    assert result.new_state["status"] == "idle"


# ══════════════════════════════════════════════════════════════════════════════
# consume_from_sun
# ══════════════════════════════════════════════════════════════════════════════


def test_consume_from_sun_starts_sustain_when_conditions_met():
    event = {
        "mode": "consume_from_sun",
        "schedule": _in_window_schedule(),
        "config": {"socOnThreshold": 85, "solarFloorW": 500, "minDurationMinutes": 5},
    }
    reading = {"batterySoc": 95, "pvPower": 1000, "batteryPower": 0}
    result = evaluate_event(event, reading, _idle(), _NOW, TZ)
    assert result.new_state["status"] == "pending_condition"
    assert "conditionMetSince" in result.new_state


def test_consume_from_sun_skips_device_control_when_sustain_elapsed():
    """consume_from_sun actions are smart-device-only; island WriteExecutor can't
    perform them → skip:device_control_unavailable instead of fake-activate."""
    event = {
        "mode": "consume_from_sun",
        "schedule": _in_window_schedule(),
        "config": {"socOnThreshold": 85, "solarFloorW": 500, "minDurationMinutes": 5},
    }
    # conditionMetSince 10 minutes ago → sustain elapsed
    state = {"status": "pending_condition", "conditionMetSince": _ago(10)}
    reading = {"batterySoc": 95, "pvPower": 1200, "batteryPower": 0}
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "skip"
    assert result.skip_reason == "device_control_unavailable"
    # State reset to idle — no spurious activate→deactivate lifecycle
    assert result.new_state.get("status", "idle") == "idle"


def test_consume_from_sun_holds_during_sustain():
    event = {
        "mode": "consume_from_sun",
        "schedule": _in_window_schedule(),
        "config": {"socOnThreshold": 85, "solarFloorW": 500, "minDurationMinutes": 5},
    }
    # conditionMetSince only 2 minutes ago — sustain not yet elapsed
    state = {"status": "pending_condition", "conditionMetSince": _ago(2)}
    reading = {"batterySoc": 95, "pvPower": 1200, "batteryPower": 0}
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "hold"


def test_consume_from_sun_deactivates_after_off_sustain():
    event = {
        "mode": "consume_from_sun",
        "schedule": _in_window_schedule(),
        "config": {
            "socOnThreshold": 85,
            "socOffThreshold": 70,
            "solarFloorW": 500,
            "minDurationMinutes": 5,
        },
    }
    # Active, SOC dropped below socOff, conditionLostSince 10 min ago
    state = _active(conditionLostSince=_ago(10))
    reading = {"batterySoc": 60, "pvPower": 100, "batteryPower": -200}
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "deactivate"
    assert result.new_state["status"] == "idle"


def test_consume_from_sun_hysteresis_prevents_flap():
    event = {
        "mode": "consume_from_sun",
        "schedule": _in_window_schedule(),
        "config": {
            "socOnThreshold": 85,
            "socOffThreshold": 70,
            "solarFloorW": 500,
            "minDurationMinutes": 5,
        },
    }
    # Active, off condition just triggered (30 sec ago — within hysteresis window)
    state = _active(conditionLostSince=_ago(0.5))
    reading = {"batterySoc": 60, "pvPower": 100, "batteryPower": -200}
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "hold", "should hold during off-hysteresis window"


# ══════════════════════════════════════════════════════════════════════════════
# battery_maintenance
# ══════════════════════════════════════════════════════════════════════════════


def test_battery_maintenance_dispatches_solar_sell_suppress():
    event = {
        "mode": "battery_maintenance",
        "schedule": _in_window_schedule(),
        "config": {"gridFallbackHour": 17},
    }
    reading = {"batterySoc": 80}
    result = evaluate_event(event, reading, _idle(), _NOW, TZ)
    # set_solar_sell{solarSell:0} always dispatched in-window
    cmd_names = [c[0] for c in result.commands]
    assert "set_solar_sell" in cmd_names
    solar_sell_cmd = next(c for c in result.commands if c[0] == "set_solar_sell")
    assert solar_sell_cmd[1]["solarSell"] == 0
    assert result.new_state["status"] == "active"


def test_battery_maintenance_marks_completed_at_soc_99():
    event = {
        "mode": "battery_maintenance",
        "schedule": _in_window_schedule(),
        "config": {"gridFallbackHour": 17},
    }
    reading = {"batterySoc": 99}
    result = evaluate_event(event, reading, _active(), _NOW, TZ)
    assert result.new_state.get("completed") is True
    assert result.action == "hold"


def test_battery_maintenance_grid_fallback_after_hour():
    """After gridFallbackHour (17:00 Kyiv = 14:00 UTC), dispatch set_battery_charge."""
    # _NOW = 12:00 UTC = 15:00 Kyiv → hour 15 ≥ fallbackHour 14
    event = {
        "mode": "battery_maintenance",
        "schedule": {"recurrence": "daily", "startTime": "10:00", "endTime": "23:00"},
        "config": {"gridFallbackHour": 14},  # 14:00 Kyiv = 11:00 UTC; _NOW=15:00 Kyiv > 14
    }
    reading = {"batterySoc": 70}
    result = evaluate_event(event, reading, _idle(), _NOW, TZ)
    cmd_names = [c[0] for c in result.commands]
    assert "set_battery_charge" in cmd_names
    charge_cmd = next(c for c in result.commands if c[0] == "set_battery_charge")
    assert charge_cmd[1]["gridChargeEnabled"] is True
    assert charge_cmd[1]["gridChargeSoc"] == 100


def test_battery_maintenance_no_fallback_before_hour():
    """Before gridFallbackHour, no set_battery_charge dispatched."""
    # 12:00 UTC = 15:00 Kyiv; fallbackHour=20 (Kyiv) = not yet reached
    event = {
        "mode": "battery_maintenance",
        "schedule": _in_window_schedule(),
        "config": {"gridFallbackHour": 20},
    }
    reading = {"batterySoc": 50}
    result = evaluate_event(event, reading, _idle(), _NOW, TZ)
    cmd_names = [c[0] for c in result.commands]
    assert "set_battery_charge" not in cmd_names


# ══════════════════════════════════════════════════════════════════════════════
# use_battery
# ══════════════════════════════════════════════════════════════════════════════


def test_use_battery_activate_in_window():
    event = {
        "mode": "use_battery",
        "schedule": _in_window_schedule(),
        "config": {},
    }
    result = evaluate_event(event, {"batterySoc": 70}, _idle(), _NOW, TZ)
    assert result.action == "activate"
    assert result.new_state["status"] == "active"
    cmds = dict(result.commands)
    assert cmds["set_work_mode"]["workMode"] == 2
    assert cmds["set_grid_charge_toggle"]["enabled"] is False


def test_use_battery_hold_when_active():
    event = {
        "mode": "use_battery",
        "schedule": _in_window_schedule(),
        "config": {},
    }
    result = evaluate_event(event, {"batterySoc": 70}, _active(), _NOW, TZ)
    assert result.action == "hold"
    assert result.commands == []


def test_use_battery_deactivate_out_of_window():
    event = {
        "mode": "use_battery",
        "schedule": _out_window_schedule(),
        "config": {},
    }
    state = _active(previousWorkMode=0, previousTimerEnabled=True)
    result = evaluate_event(event, {}, state, _NOW, TZ)
    assert result.action == "deactivate"
    cmds = dict(result.commands)
    assert "set_work_mode" in cmds
    assert cmds["set_work_mode"]["workMode"] == 0
    assert "set_grid_charge_toggle" in cmds


# ══════════════════════════════════════════════════════════════════════════════
# gen_force
# ══════════════════════════════════════════════════════════════════════════════


def test_gen_force_activate_after_sustain_soc_below_threshold():
    """SOC below startSocPercent + 1+ min in pending_condition → activate."""
    event = {
        "mode": "gen_force",
        "schedule": _in_window_schedule(),
        "config": {"startSocPercent": 30, "targetSocPercent": 80},
    }
    state = {
        "status": "pending_condition",
        "conditionMetSince": _ago(2),  # 2 minutes → sustain elapsed
        "lastActivatedAt": None,
        "lastDeactivatedAt": None,
    }
    reading = {"batterySoc": 20, "gridPower": 0, "gridFrequency": 10}
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "activate"
    assert result.new_state["status"] == "active"
    cmds = dict(result.commands)
    assert cmds["set_gen_force"]["on"] is True


def test_gen_force_hold_when_already_active_and_soc_not_target():
    event = {
        "mode": "gen_force",
        "schedule": _in_window_schedule(),
        "config": {"startSocPercent": 30, "targetSocPercent": 80},
    }
    state = _active(lastActivatedAt=_ago(5))
    reading = {"batterySoc": 50}
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "hold"


def test_gen_force_deactivate_when_soc_reaches_target():
    event = {
        "mode": "gen_force",
        "schedule": _in_window_schedule(),
        "config": {"startSocPercent": 30, "targetSocPercent": 80},
    }
    state = _active(lastActivatedAt=_ago(60))
    reading = {"batterySoc": 85}  # above target
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "deactivate"
    cmds = dict(result.commands)
    assert cmds["set_gen_force"]["on"] is False


def test_gen_force_starts_sustain_from_idle():
    """SOC below threshold from idle → start_sustain (pending_condition)."""
    event = {
        "mode": "gen_force",
        "schedule": _in_window_schedule(),
        "config": {"startSocPercent": 30},
    }
    reading = {"batterySoc": 20}
    result = evaluate_event(event, reading, _idle(), _NOW, TZ)
    assert result.new_state["status"] == "pending_condition"
    assert result.action == "hold"


def test_gen_force_grid_down_needed_but_grid_up_stays_idle():
    """requireGridDown=True and grid is up → no activation."""
    event = {
        "mode": "gen_force",
        "schedule": _in_window_schedule(),
        "config": {"requireGridDown": True, "startSocPercent": 30},
    }
    reading = {
        "batterySoc": 10,
        "phaseVoltages": [230, 230, 230],
        "gridFrequency": 50,
        "gridPower": 500,
    }
    result = evaluate_event(event, reading, _idle(), _NOW, TZ)
    assert result.action == "hold"
    assert result.new_state["status"] == "idle"


# ══════════════════════════════════════════════════════════════════════════════
# custom (local conditions only)
# ══════════════════════════════════════════════════════════════════════════════


def test_custom_battery_soc_lte_activates_after_sustain():
    """battery_soc lte condition + sustainMinutes=0 → pending on first call, active on second."""
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "battery_soc", "op": "lte", "threshold": 30}],
            "customActions": [{"command": "set_work_mode", "payload": {"workMode": 0}}],
            "sustainMinutes": 0,
        },
    }
    reading = {"batterySoc": 20, "pvPower": 0, "loadPower": 500, "gridPower": 100}

    # First call: idle → pending_condition
    r1 = evaluate_event(event, reading, _idle(), _NOW, TZ)
    assert r1.new_state["status"] == "pending_condition"

    # Second call: pending_condition → activate (sustainMs=0, elapsed ≥ 0)
    r2 = evaluate_event(event, reading, r1.new_state, _NOW, TZ)
    assert r2.action == "activate"
    cmds = dict(r2.commands)
    assert "set_work_mode" in cmds
    assert cmds["set_work_mode"]["workMode"] == 0


def test_custom_hold_when_already_active_conditions_still_met():
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "battery_soc", "op": "lte", "threshold": 30}],
            "customActions": [{"command": "set_work_mode", "payload": {"workMode": 0}}],
            "sustainMinutes": 0,
        },
    }
    reading = {"batterySoc": 20, "pvPower": 0, "loadPower": 500, "gridPower": 0}
    result = evaluate_event(event, reading, _active(), _NOW, TZ)
    assert result.action == "hold"


def test_custom_deactivate_when_conditions_drop():
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "battery_soc", "op": "lte", "threshold": 30}],
            "customActions": [
                {
                    "command": "set_work_mode",
                    "payload": {"workMode": 0},
                    "restorePayload": {"workMode": 2},
                }
            ],
            "sustainMinutes": 0,
        },
    }
    reading = {"batterySoc": 80, "pvPower": 0, "loadPower": 500, "gridPower": 0}
    state = _active()
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "deactivate"
    cmds = dict(result.commands)
    assert "set_work_mode" in cmds
    assert cmds["set_work_mode"]["workMode"] == 2


def test_custom_pv_power_gte_activates():
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "pv_power", "op": "gte", "thresholdW": 1000}],
            "customActions": [{"command": "set_solar_sell", "payload": {"solarSell": 1}}],
            "sustainMinutes": 0,
        },
    }
    reading = {"batterySoc": 70, "pvPower": 1500, "loadPower": 300, "gridPower": 0}

    # idle → pending
    r1 = evaluate_event(event, reading, _idle(), _NOW, TZ)
    assert r1.new_state["status"] == "pending_condition"

    # pending → activate
    r2 = evaluate_event(event, reading, r1.new_state, _NOW, TZ)
    assert r2.action == "activate"
    cmds = dict(r2.commands)
    assert "set_solar_sell" in cmds


def test_custom_grid_condition_activates_when_grid_up():
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "grid", "state": "up"}],
            "customActions": [{"command": "set_work_mode", "payload": {"workMode": 0}}],
            "sustainMinutes": 0,
        },
    }
    reading = {
        "batterySoc": 70,
        "pvPower": 0,
        "loadPower": 1000,
        "gridPower": 500,
        "phaseVoltages": [230.0],
        "gridFrequency": 50.0,
    }
    r1 = evaluate_event(event, reading, _idle(), _NOW, TZ)
    r2 = evaluate_event(event, reading, r1.new_state, _NOW, TZ)
    assert r2.action == "activate"


def test_custom_sustain_hysteresis_prevents_immediate_deactivate():
    """With sustainMinutes=2, active event doesn't immediately deactivate when conditions drop."""
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "battery_soc", "op": "lte", "threshold": 30}],
            "customActions": [{"command": "set_work_mode", "payload": {"workMode": 0}}],
            "sustainMinutes": 2,
        },
    }
    reading = {"batterySoc": 80, "pvPower": 0, "loadPower": 500, "gridPower": 0}
    # Active, conditions just dropped (30 sec ago — within 2-min sustain)
    state = _active(conditionLostSince=_ago(0.5))
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "hold", "should hold during release sustain window"


def test_custom_deactivates_after_release_sustain():
    """After sustainMinutes of conditions being false, deactivate."""
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "battery_soc", "op": "lte", "threshold": 30}],
            "customActions": [
                {
                    "command": "set_work_mode",
                    "payload": {"workMode": 0},
                    "restorePayload": {"workMode": 2},
                }
            ],
            "sustainMinutes": 2,
        },
    }
    reading = {"batterySoc": 80, "pvPower": 0, "loadPower": 500, "gridPower": 0}
    # Conditions lost 3 minutes ago → beyond 2-min sustain
    state = _active(conditionLostSince=_ago(3))
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "deactivate"


# ══════════════════════════════════════════════════════════════════════════════
# Review-fix wave tests
# ══════════════════════════════════════════════════════════════════════════════


def test_one_holds_unknown_condition_type_returns_false():
    """Unknown/future condition type → _one_holds is fail-closed (False), not fail-open.

    A cloud condition type not yet in _CLOUD_CONDITION_TYPES would pass the
    top-level guard, reach _one_holds, and must NOT automatically 'hold'.
    This prevents a future unknown type from always activating.
    """
    from custom_components.svitgrid.harvest.event_evaluator import _one_holds

    unknown = {"type": "future_ai_condition", "param": 42}
    signals = {"batterySoc": 80, "pvPowerW": 1000, "loadPowerW": 500}
    # fail-closed: unknown type → False regardless of held state
    assert _one_holds(unknown, signals, False) is False
    assert _one_holds(unknown, signals, True) is False


def test_custom_unknown_condition_type_does_not_activate():
    """Custom event with an unrecognized condition type never activates."""
    event = {
        "mode": "custom",
        "schedule": _in_window_schedule(),
        "config": {
            "customConditions": [{"type": "unknown_future_type", "someParam": True}],
            "customActions": [{"command": "set_work_mode", "payload": {"workMode": 0}}],
            "sustainMinutes": 0,
        },
    }
    reading = {"batterySoc": 80, "pvPower": 0, "loadPower": 500, "gridPower": 0}
    # First call: condition never holds → stays idle (no start_sustain)
    r1 = evaluate_event(event, reading, _idle(), _NOW, TZ)
    assert r1.action != "activate"
    assert r1.new_state.get("status", "idle") == "idle"


def test_battery_maintenance_completed_redispatches_solar_sell():
    """When completed=True and still in-window, hold is returned and set_solar_sell{0}
    is re-dispatched every tick to keep solar-sell suppressed.

    Matches TS evaluateBatteryMaintenanceCharge lines 1814-1841: step 3
    ('Always: set_solar_sell {solarSell:0}') runs BEFORE the SOC≥99 completed
    check, so re-dispatch happens unconditionally in-window even post-completion.
    """
    event = {
        "mode": "battery_maintenance",
        "schedule": _in_window_schedule(),
        "config": {"gridFallbackHour": 20},  # 20:00 Kyiv, not reached at 15:00
    }
    reading = {"batterySoc": 99}
    # State already marks completed from a previous tick
    state = _active(completed=True, maintenancePriorSolarSell=False)
    result = evaluate_event(event, reading, state, _NOW, TZ)
    assert result.action == "hold"
    assert result.new_state.get("completed") is True
    cmd_names = [c[0] for c in result.commands]
    assert "set_solar_sell" in cmd_names
    solar_sell_cmd = next(c for c in result.commands if c[0] == "set_solar_sell")
    assert solar_sell_cmd[1]["solarSell"] == 0
    # Grid-charge NOT dispatched (SOC >= 99 → early return before fallback logic)
    assert "set_battery_charge" not in cmd_names


def test_sell_to_grid_smart_reactivates_after_pv_recovery():
    """ON-edge recovery: pending_condition + conditionMetSince > 2min + PV back above
    threshold → activate (mirrors the tested OFF-edge hysteresis).

    State machine: active→(pv drops)→deactivate→pending_condition→(pv recovers
    + 2min sustain)→activate.
    """
    event = {
        "mode": "sell_to_grid",
        "schedule": _in_window_schedule(),
        "config": {"sellMode": "smart", "pvThresholdW": 500},
    }
    state = {
        "status": "pending_condition",
        "conditionMetSince": _ago(3),  # 3 min elapsed → ON hysteresis met
        "previousWorkMode": 2,
    }
    result = evaluate_event(event, {"pvPower": 1200}, state, _NOW, TZ)
    assert result.action == "activate"
    assert result.new_state["status"] == "active"
    cmds = dict(result.commands)
    assert cmds["set_work_mode"]["workMode"] == 0
