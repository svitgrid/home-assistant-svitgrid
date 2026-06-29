"""Local port of the cloud's isEventInWindow (evaluator.ts:385-427) and
evaluate_event (Task 4) — per-mode condition evaluator with activation guards.

Parity with the TypeScript cloud evaluator is the contract.
Cloud-only inputs (forecast / price / scheduled-outage) are NEVER evaluated
locally — events that depend on them are skipped with an explicit reason.

TS mapping:
  parseTimeToMinutes  → parse_time_to_minutes
  getLocalParts       → datetime.astimezone(ZoneInfo(tz)) + manual weekday remap
  isEventInWindow     → is_event_in_window
  deriveGridPresent   → _derive_grid_present   (derive-grid-present.ts)
  decideGenForce      → _decide_gen_force       (gen-force-decision.ts)
  evaluateConditions  → _evaluate_conditions    (custom-event-decision.ts)
  decideCustomEvent   → _decide_custom_event    (custom-event-decision.ts)
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

# ─────────────────────────────── Window helpers ───────────────────────────────

def parse_time_to_minutes(hhmm: str) -> int:
    """Convert "HH:mm" to minutes since midnight.

    Mirrors TS: const [h, m] = time.split(':').map(Number); return h * 60 + m;
    """
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def is_event_in_window(schedule: dict, now_utc: datetime, tz: str) -> bool:
    """Return True when now_utc falls within the event's scheduled window.

    Faithful port of TS isEventInWindow (evaluator.ts:385-427).

    Local-parts resolution (mirrors TS getLocalParts via Intl.DateTimeFormat):
      - Convert now_utc → local wall-clock via zoneinfo.ZoneInfo(tz).
      - weekday: 0=Sun..6=Sat (matches TS weekdayMap Sun:0 Mon:1 … Sat:6).
        Python datetime.weekday() is 0=Mon..6=Sun, so we remap:
        local_weekday = (python_weekday + 1) % 7

    Date bounds (TS lines 391-392):
      - startDate present → localDate >= startDate (ISO string compare)
      - endDate   present → localDate <= endDate

    Recurrence (TS lines 394-412):
      - 'none'          → only on startDate
      - 'daily'         → always active within date range
      - 'weekly'/'custom' → only when local weekday is in weekdays[];
                           d % 7 collapses both 0=Sun (legacy) and 1=Mon Dart
                           conventions onto 0=Sun..6=Sat so the compare is
                           convention-agnostic (TS comment lines 405-408).

    Time window (TS lines 415-426):
      - Normal: startMinutes <= nowMinutes < endMinutes
      - Overnight wrap (endMinutes <= startMinutes, e.g. 23:00-07:00):
        nowMinutes >= startMinutes OR nowMinutes < endMinutes
    """
    # ── Resolve local parts ────────────────────────────────────────────────────
    local = now_utc.astimezone(ZoneInfo(tz))
    local_date_str = local.strftime("%Y-%m-%d")
    local_hours = local.hour
    local_minutes = local.minute
    # Remap Python Mon=0..Sun=6 → Sun=0..Sat=6 (matches TS weekdayMap)
    local_weekday = (local.weekday() + 1) % 7

    # ── Date bounds ────────────────────────────────────────────────────────────
    start_date = schedule.get("startDate")
    end_date = schedule.get("endDate")
    if start_date and local_date_str < start_date:
        return False
    if end_date and local_date_str > end_date:
        return False

    # ── Recurrence ────────────────────────────────────────────────────────────
    recurrence = schedule.get("recurrence", "daily")
    if recurrence == "none":
        # Only fires on its startDate
        if start_date and start_date != local_date_str:
            return False
    elif recurrence == "daily":
        pass  # Always active within date range
    elif recurrence in ("weekly", "custom"):
        weekdays = schedule.get("weekdays") or []
        if weekdays and not any(d % 7 == local_weekday for d in weekdays):
            return False

    # ── Time window ───────────────────────────────────────────────────────────
    now_minutes = local_hours * 60 + local_minutes
    start_minutes = parse_time_to_minutes(schedule["startTime"])
    end_minutes = parse_time_to_minutes(schedule["endTime"])

    if end_minutes <= start_minutes:
        # Overnight wrap: [start, 24:00) ∪ [00:00, end)
        # e.g. start=23:00 end=07:00 → active at 23:30 (>=1380) or 02:00 (<420)
        return now_minutes >= start_minutes or now_minutes < end_minutes

    return now_minutes >= start_minutes and now_minutes < end_minutes


# ─────────────────────────── EvaluatorDecision ───────────────────────────────

@dataclass
class EvaluatorDecision:
    """Result of evaluate_event.

    action     : 'activate' | 'deactivate' | 'hold' | 'skip'
    commands   : list[tuple[str, dict]] — (command_name, payload) to dispatch
    new_state  : updated executionState dict (always a deep copy)
    skip_reason: non-None only when action == 'skip'
    """

    action: str
    commands: list
    new_state: dict
    skip_reason: str | None = None


# ──────────────────────── Constants (derive-grid-present.ts) ─────────────────

_VOLTAGE_DOWN_V = 30.0
_FREQ_DOWN_HZ = 30.0
_POWER_NEAR_ZERO_W = 50.0
_RELAY_GLITCH_MIN_V = 180.0
_RELAY_GLITCH_MIN_POWER_W = 200.0

# gen-force-decision.ts
_GEN_FORCE_START_SUSTAIN_MIN = 1  # minutes

# Condition types that require cloud data — NEVER evaluated locally
_CLOUD_CONDITION_TYPES: frozenset[str] = frozenset({
    "forecast_today",
    "dam_price",
    "scheduled_outage",
})


# ─────────────────── Grid presence (derive-grid-present.ts) ──────────────────

def _derive_grid_present(reading: dict) -> bool | None:
    """Port of deriveGridPresent. Returns True / False / None."""
    relay = reading.get("gridRelayClosed")
    if relay is True:
        return True

    raw = reading.get("phaseVoltages")
    phase_voltages: list[float] = [
        float(v)
        for v in (raw if isinstance(raw, list) else [])
        if isinstance(v, (int, float)) and math.isfinite(float(v))
    ]
    if not phase_voltages:
        gv = reading.get("gridVoltage")
        if isinstance(gv, (int, float)) and math.isfinite(float(gv)):
            phase_voltages = [float(gv)]

    has_voltage = bool(phase_voltages)

    freq = reading.get("gridFrequency")
    has_freq = isinstance(freq, (int, float)) and math.isfinite(float(freq))

    pwr = reading.get("gridPower")
    has_power = isinstance(pwr, (int, float)) and math.isfinite(float(pwr))

    if relay is False:
        volts_healthy = has_voltage and all(v >= _RELAY_GLITCH_MIN_V for v in phase_voltages)
        power_flowing = has_power and abs(float(pwr)) >= _RELAY_GLITCH_MIN_POWER_W  # type: ignore[arg-type]
        if not (volts_healthy and power_flowing):
            return False
        # else: relay bit contradicted by physics → fall through

    if not has_voltage and not has_freq:
        return None

    volts_down = has_voltage and all(v < _VOLTAGE_DOWN_V for v in phase_voltages)
    freq_down = has_freq and float(freq) < _FREQ_DOWN_HZ  # type: ignore[arg-type]
    power_near_zero = has_power and abs(float(pwr)) < _POWER_NEAR_ZERO_W  # type: ignore[arg-type]

    if freq_down and volts_down:
        return False
    if volts_down and power_near_zero:
        return False
    return not (freq_down and power_near_zero)


# ─────────────────── Timestamp helpers ───────────────────────────────────────

def _iso_to_ms(value: str | float | int | None) -> float | None:
    """Convert an ISO-8601 string or epoch-ms number to epoch milliseconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.timestamp() * 1000.0
    except (ValueError, TypeError):
        return None


def _now_ms(now_utc: datetime) -> float:
    return now_utc.timestamp() * 1000.0


def _local_hour(now_utc: datetime, tz: str) -> int:
    return now_utc.astimezone(ZoneInfo(tz)).hour


def _local_total_minutes(now_utc: datetime, tz: str) -> int:
    local = now_utc.astimezone(ZoneInfo(tz))
    return local.hour * 60 + local.minute


def _time_to_packed_hhmm(hhmm: str) -> int:
    """'10:45' → 1045 (matches timeToPackedHHMM in evaluator.ts)."""
    h, m = hhmm.split(":")
    return int(h) * 100 + int(m)


# ─────────────────── Gen-force decision (gen-force-decision.ts) ──────────────

def _parse_hm(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _in_quiet_hours(now_local_min: int, start: str | None, end: str | None) -> bool:
    if not start or not end:
        return False
    s, e = _parse_hm(start), _parse_hm(end)
    if s == e:
        return False
    if s < e:
        return s <= now_local_min < e
    return now_local_min >= s or now_local_min < e


def _decide_gen_force(
    now_ms: float,
    now_local_min: int,
    signals: dict,
    config: dict,
    state: dict,
) -> dict:
    """Port of decideGenForce (gen-force-decision.ts).

    requireScheduledOutage is stripped before this function is called;
    outageHourNow is always False here.
    """
    require_grid_down = bool(config.get("requireGridDown"))
    grid_gated = require_grid_down  # requireScheduledOutage already filtered
    grid_present = signals.get("gridPresent")

    grid_permitted = True if not grid_gated else require_grid_down and grid_present is False

    soc = float(signals.get("batterySoc", 100))
    soc_start = config.get("startSocPercent")
    soc_demand = True if soc_start is None else soc <= float(soc_start)
    activation_desired = grid_permitted and soc_demand

    quiet = _in_quiet_hours(now_local_min, config.get("quietHoursStart"), config.get("quietHoursEnd"))
    crit_soc = config.get("quietHoursCriticalOverrideSoc")
    crit_override = crit_soc is not None and soc <= float(crit_soc)
    quiet_blocked = quiet and not crit_override

    rest_min = float(config.get("minRestMinutes") or 0)
    last_deact_ms = _iso_to_ms(state.get("lastDeactivatedAt"))
    rest_ok = last_deact_ms is None or (now_ms - last_deact_ms) >= rest_min * 60_000.0

    status = state.get("status", "idle")

    if status == "active":
        soc_target = config.get("targetSocPercent")
        stop_on_grid = bool(config.get("stopOnGridRestored"))
        stop_reason: str | None = None
        if soc_target is not None and soc >= float(soc_target):
            stop_reason = "target_soc"
        elif stop_on_grid and grid_present is True:
            stop_reason = "grid_restored"
        if stop_reason is None:
            return {"action": "none", "reason": "running"}
        run_min = float(config.get("minRunMinutes") or 0)
        last_act_ms = _iso_to_ms(state.get("lastActivatedAt"))
        min_run_ok = last_act_ms is None or (now_ms - last_act_ms) >= run_min * 60_000.0
        if not min_run_ok:
            return {"action": "none", "reason": "min_run"}
        return {"action": "deactivate", "reason": stop_reason}

    if status == "pending_condition":
        if not activation_desired or quiet_blocked:
            return {"action": "clear_sustain", "reason": "condition_lost"}
        cms_ms = _iso_to_ms(state.get("conditionMetSince"))
        if cms_ms is not None and (now_ms - cms_ms) >= _GEN_FORCE_START_SUSTAIN_MIN * 60_000.0:
            reason = (
                "low_soc" if (soc_demand and soc_start is not None)
                else ("grid_unavailable" if grid_gated else "scheduled")
            )
            return {"action": "activate", "reason": reason}
        return {"action": "none", "reason": "sustaining"}

    # idle
    if not activation_desired:
        return {"action": "none", "reason": "no_demand"}
    if quiet_blocked:
        return {"action": "none", "reason": "quiet_hours"}
    if not rest_ok:
        return {"action": "none", "reason": "min_rest"}
    return {"action": "start_sustain", "reason": "start_condition"}


# ─────────────────── Custom conditions (custom-event-decision.ts) ─────────────

def _numeric_holds(
    op: str,
    value: float,
    on_threshold: float,
    release_threshold: float | None,
    held: bool,
) -> bool:
    rel = release_threshold if release_threshold is not None else on_threshold
    if op == "lte":
        return value <= rel if held else value <= on_threshold
    # gte
    return value >= rel if held else value >= on_threshold


def _one_holds(condition: dict, signals: dict, held: bool) -> bool:
    ctype = condition.get("type")
    if ctype == "battery_soc":
        soc = signals.get("batterySoc")
        if soc is None:
            return False
        return _numeric_holds(
            condition["op"], float(soc),
            float(condition["threshold"]), condition.get("releaseThreshold"),
            held,
        )
    if ctype == "pv_power":
        return _numeric_holds(
            condition["op"], float(signals.get("pvPowerW", 0)),
            float(condition["thresholdW"]), condition.get("releaseThresholdW"),
            held,
        )
    if ctype == "load_power":
        return _numeric_holds(
            condition["op"], float(signals.get("loadPowerW", 0)),
            float(condition["thresholdW"]), condition.get("releaseThresholdW"),
            held,
        )
    if ctype == "grid":
        gp = signals.get("gridPresent")
        if gp is None:
            return False
        return bool(gp) if condition.get("state") == "up" else not bool(gp)
    # Cloud-only types are pre-filtered; an unrecognised type must be fail-closed
    # so a future cloud condition type not yet listed in _CLOUD_CONDITION_TYPES
    # does NOT accidentally pass the guard and always-activate.
    return False


def _evaluate_conditions(conditions: list, signals: dict, held: bool) -> bool:
    if not conditions:
        return True
    return all(_one_holds(c, signals, held) for c in conditions)


def _decide_custom_event(
    now_ms: float,
    conditions: list,
    signals: dict,
    sustain_minutes: float,
    state: dict,
) -> dict:
    """Port of decideCustomEvent (custom-event-decision.ts).

    state must contain:
      status           : 'idle' | 'pending_condition' | 'active'
      conditionMetSince: ISO string or epoch ms or None
      conditionLostSince: ISO string or epoch ms or None
    """
    sustain_ms = max(0.0, sustain_minutes) * 60_000.0
    status = state.get("status", "idle")
    held = status in ("active", "pending_condition")
    conditions_hold = _evaluate_conditions(conditions, signals, held)

    if status == "idle":
        if conditions_hold:
            return {"action": "start_sustain", "reason": "conditions_met"}
        return {"action": "none", "reason": "conditions_not_met"}

    if status == "pending_condition":
        if not conditions_hold:
            return {"action": "clear_sustain", "reason": "conditions_lost_before_activation"}
        # Use nowMs as sentinel when conditionMetSince is absent (sustain=0 → immediate)
        cms_ms = _iso_to_ms(state.get("conditionMetSince")) or now_ms
        if now_ms - cms_ms >= sustain_ms:
            return {"action": "activate", "reason": "sustained"}
        return {"action": "none", "reason": "sustaining"}

    # active
    if conditions_hold:
        return {"action": "none", "reason": "conditions_still_met"}
    # Release hysteresis
    lost_ms = _iso_to_ms(state.get("conditionLostSince")) or now_ms
    if now_ms - lost_ms >= sustain_ms:
        return {"action": "deactivate", "reason": "conditions_released"}
    return {"action": "none", "reason": "release_sustaining"}


# ─────────────────── Restore-command builders ────────────────────────────────

def _restore_commands(event: dict, exec_state: dict) -> list:
    """Return the restore (de-activation) command list for the given mode."""
    mode = event.get("mode", "")
    config = event.get("config") or {}
    schedule = event.get("schedule") or {}

    if mode == "sell_to_grid":
        prev = exec_state.get("previousWorkMode", 2)
        return [("set_work_mode", {"workMode": prev})]

    if mode == "use_battery":
        prev_mode = exec_state.get("previousWorkMode", 2)
        prev_timer = exec_state.get("previousTimerEnabled", True)
        return [
            ("set_work_mode", {"workMode": prev_mode}),
            ("set_grid_charge_toggle", {"enabled": prev_timer}),
        ]

    if mode == "gen_force":
        return [("set_gen_force", {"on": False})]

    if mode == "battery_maintenance":
        cmds: list = []
        prev_sell = exec_state.get("maintenancePriorSolarSell", False)
        cmds.append(("set_solar_sell", {"solarSell": 1 if prev_sell else 0}))
        if exec_state.get("gridFallbackFired"):
            cmds.append(("set_battery_charge", {
                "gridChargeEnabled": False,
                "gridChargeSoc": 0,
                "slotIndex": 0,
                "disableOtherSlots": False,
                "slotStart": _time_to_packed_hhmm(schedule.get("startTime", "00:00")),
                "slotEnd": _time_to_packed_hhmm(schedule.get("endTime", "23:59")),
            }))
        return cmds

    if mode == "battery_charge":
        return [("set_battery_charge", {
            "gridChargeEnabled": False,
            "gridChargeSoc": 0,
            "slotIndex": 0,
            "disableOtherSlots": False,
            "slotStart": _time_to_packed_hhmm(schedule.get("startTime", "00:00")),
            "slotEnd": _time_to_packed_hhmm(schedule.get("endTime", "23:59")),
        })]

    if mode == "custom":
        actions = config.get("customActions") or []
        return [
            (a["command"], dict(a["restorePayload"]))
            for a in actions
            if a.get("command") and a.get("restorePayload")
        ]

    return []


# ─────────────────── Main entry point ────────────────────────────────────────

def evaluate_event(
    event: dict,
    reading: dict,
    exec_state: dict,
    now_utc: datetime,
    tz: str,
) -> EvaluatorDecision:
    """Evaluate a calendar event against a live reading.

    Returns EvaluatorDecision with action, commands, new_state, skip_reason.

    Cloud-only modes / conditions are NEVER evaluated — they return skip with
    an explicit reason so the scheduler can surface the reason to the user.
    """
    mode = event.get("mode", "")
    config = event.get("config") or {}

    # ── Cloud-only mode ────────────────────────────────────────────────────────
    if mode == "day_planning":
        return EvaluatorDecision(
            action="skip", commands=[], new_state=dict(exec_state),
            skip_reason="cloud_only_mode",
        )

    # ── Per-mode cloud-only config checks ─────────────────────────────────────
    if mode == "gen_force" and config.get("requireScheduledOutage"):
        return EvaluatorDecision(
            action="skip", commands=[], new_state=dict(exec_state),
            skip_reason="requires_scheduled_outage",
        )

    if mode == "custom":
        conditions = config.get("customConditions") or []
        if any(c.get("type") in _CLOUD_CONDITION_TYPES for c in conditions):
            return EvaluatorDecision(
                action="skip", commands=[], new_state=dict(exec_state),
                skip_reason="cloud_only_condition",
            )

    # ── Window check ──────────────────────────────────────────────────────────
    schedule = event.get("schedule") or {}
    in_window = is_event_in_window(schedule, now_utc, tz)
    new_state = copy.deepcopy(exec_state)

    if not in_window:
        current_status = exec_state.get("status", "idle")
        if current_status == "active":
            restore_cmds = _restore_commands(event, exec_state)
            new_state["status"] = "idle"
            new_state.pop("lastActivatedAt", None)
            new_state.pop("conditionMetSince", None)
            new_state.pop("conditionLostSince", None)
            return EvaluatorDecision(action="deactivate", commands=restore_cmds, new_state=new_state)
        if current_status == "pending_condition":
            new_state["status"] = "idle"
            new_state.pop("conditionMetSince", None)
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    # ── In-window: per-mode evaluation ────────────────────────────────────────
    return _eval_mode(mode, event, reading, exec_state, new_state, now_utc, tz)


def _eval_mode(
    mode: str,
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
    tz: str,
) -> EvaluatorDecision:
    if mode == "battery_charge":
        return _eval_battery_charge(event, reading, exec_state, new_state, now_utc)
    if mode == "sell_to_grid":
        return _eval_sell_to_grid(event, reading, exec_state, new_state, now_utc)
    if mode == "lower_consumption":
        return _eval_lower_consumption(event, reading, exec_state, new_state, now_utc)
    if mode == "consume_from_sun":
        return _eval_consume_from_sun(event, reading, exec_state, new_state, now_utc)
    if mode == "battery_maintenance":
        return _eval_battery_maintenance(event, reading, exec_state, new_state, now_utc, tz)
    if mode == "use_battery":
        return _eval_use_battery(event, exec_state, new_state, now_utc)
    if mode == "gen_force":
        return _eval_gen_force(event, reading, exec_state, new_state, now_utc, tz)
    if mode == "custom":
        return _eval_custom(event, reading, exec_state, new_state, now_utc)
    # Unknown mode → hold
    return EvaluatorDecision(action="hold", commands=[], new_state=new_state)


# ─────────────────── battery_charge ──────────────────────────────────────────

def _eval_battery_charge(
    event: dict, reading: dict, exec_state: dict, new_state: dict, now_utc: datetime,
) -> EvaluatorDecision:
    """In-window → activate set_battery_charge once; hold when already active.

    Forecast gate from the TS is STRIPPED locally (brief §lower_consumption).
    """
    if exec_state.get("status") == "active":
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    config = event.get("config") or {}
    schedule = event.get("schedule") or {}
    charge_source = config.get("chargeSource", "solar")
    grid_charge_enabled = charge_source in ("grid", "both", "solar_surplus_grid")
    target_soc = config.get("targetSoc", 100)

    payload: dict = {
        "gridChargeEnabled": grid_charge_enabled,
        "gridChargeSoc": target_soc,
        "slotIndex": 0,
        "disableOtherSlots": True,
        "slotStart": _time_to_packed_hhmm(schedule.get("startTime", "00:00")),
        "slotEnd": _time_to_packed_hhmm(schedule.get("endTime", "23:59")),
    }
    power_limit = config.get("chargePowerLimitW")
    if power_limit is not None:
        payload["powerLimit"] = power_limit

    new_state["status"] = "active"
    new_state["lastActivatedAt"] = now_utc.isoformat()
    return EvaluatorDecision(
        action="activate",
        commands=[("set_battery_charge", payload)],
        new_state=new_state,
    )


# ─────────────────── sell_to_grid ────────────────────────────────────────────

def _eval_sell_to_grid(
    event: dict, reading: dict, exec_state: dict, new_state: dict, now_utc: datetime,
) -> EvaluatorDecision:
    """Simple mode: activate once (forecast/price gates STRIPPED).
    Smart mode: PV-power-gated with 2-min hysteresis on the OFF edge.

    TS lines 2043-2348.
    """
    config = event.get("config") or {}
    sell_mode = config.get("sellMode", "simple")
    status = exec_state.get("status", "idle")
    now_ms = _now_ms(now_utc)

    # ── Smart mode ────────────────────────────────────────────────────────────
    if sell_mode == "smart":
        pv_threshold = float(config.get("pvThresholdW", 500))
        pv_power = float(reading.get("pvPower") or 0)
        pv_above = pv_power >= pv_threshold

        if status == "idle":
            if not pv_above:
                return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
            # Snapshot prior work mode for restore
            new_state["previousWorkMode"] = exec_state.get("previousWorkMode", 2)
            new_state["status"] = "active"
            new_state["lastActivatedAt"] = now_utc.isoformat()
            new_state.pop("conditionMetSince", None)
            new_state.pop("conditionLostSince", None)
            return EvaluatorDecision(
                action="activate",
                commands=[("set_work_mode", {"workMode": 0})],
                new_state=new_state,
            )

        if status == "active":
            if pv_above:
                # Clear any stale OFF timer
                new_state.pop("conditionLostSince", None)
                return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
            # PV dropped — start or continue 2-min hysteresis
            lost_str = exec_state.get("conditionLostSince")
            if not lost_str:
                new_state["conditionLostSince"] = now_utc.isoformat()
                return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
            lost_ms = _iso_to_ms(lost_str)
            if lost_ms is None or (now_ms - lost_ms) < 2 * 60_000.0:
                return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
            # Hysteresis elapsed → deactivate + restore work mode
            prev_mode = exec_state.get("previousWorkMode", 2)
            new_state["status"] = "pending_condition"
            new_state.pop("conditionLostSince", None)
            new_state["conditionMetSince"] = None
            return EvaluatorDecision(
                action="deactivate",
                commands=[("set_work_mode", {"workMode": prev_mode})],
                new_state=new_state,
            )

        # pending_condition: waiting for PV recovery + 2-min confirmation
        if status == "pending_condition":
            if not pv_above:
                new_state.pop("conditionMetSince", None)
                return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
            cms_str = exec_state.get("conditionMetSince")
            if not cms_str:
                new_state["conditionMetSince"] = now_utc.isoformat()
                return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
            cms_ms = _iso_to_ms(cms_str)
            if cms_ms is None or (now_ms - cms_ms) < 2 * 60_000.0:
                return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
            # Hysteresis met → re-activate
            new_state["status"] = "active"
            new_state["lastActivatedAt"] = now_utc.isoformat()
            new_state.pop("conditionMetSince", None)
            return EvaluatorDecision(
                action="activate",
                commands=[("set_work_mode", {"workMode": 0})],
                new_state=new_state,
            )

        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    # ── Simple mode (forecast/price gates stripped) ───────────────────────────
    if status == "active":
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    new_state["previousWorkMode"] = exec_state.get("previousWorkMode", 2)
    new_state["status"] = "active"
    new_state["lastActivatedAt"] = now_utc.isoformat()
    return EvaluatorDecision(
        action="activate",
        commands=[("set_work_mode", {"workMode": 0})],
        new_state=new_state,
    )


# ─────────────────── lower_consumption ───────────────────────────────────────

def _eval_lower_consumption(
    event: dict, reading: dict, exec_state: dict, new_state: dict, now_utc: datetime,
) -> EvaluatorDecision:
    """batterySoc < socThreshold → would activate, but device-control actions
    (turn off smart loads) are outside the island WriteExecutor's capability.
    Returns skip:device_control_unavailable instead of a fake-activate with empty
    commands, so no spurious activate→deactivate lifecycle is created.

    The event will be hidden from the island event-creation UI in a later mobile
    task (consume_from_sun has the same deferral).

    TS lines 693-782.
    """
    config = event.get("config") or {}
    threshold = float(config.get("socThreshold", 30))
    status = exec_state.get("status", "idle")
    soc = float(reading.get("batterySoc", 100) or 100)
    soc_below = soc < threshold

    if soc_below and status != "active":
        # State stays idle — no spurious lifecycle
        return EvaluatorDecision(
            action="skip",
            commands=[],
            skip_reason="device_control_unavailable",
            new_state=new_state,
        )

    if not soc_below and status == "active":
        new_state["status"] = "idle"
        return EvaluatorDecision(action="deactivate", commands=[], new_state=new_state)

    return EvaluatorDecision(action="hold", commands=[], new_state=new_state)


# ─────────────────── consume_from_sun ────────────────────────────────────────

def _eval_consume_from_sun(
    event: dict, reading: dict, exec_state: dict, new_state: dict, now_utc: datetime,
) -> EvaluatorDecision:
    """PV + SOC threshold with minDurationMinutes hysteresis on both edges.

    When conditions are met and the sustain period elapses, this mode would
    activate smart-device controls (e.g. turn on high-load appliances) which
    are outside the island WriteExecutor's capability.  Returns
    skip:device_control_unavailable instead of a fake-activate with empty
    commands, so no spurious activate→deactivate lifecycle is created.

    The event will be hidden from the island event-creation UI in a later
    mobile task (lower_consumption has the same deferral).

    TS uses pvVoltage as ON gate (curtailment-proof); locally we use pvPower
    (available from HA sensors). The config field is solarFloorW / minPvThresholdW.
    TS lines 789-1039.
    """
    config = event.get("config") or {}
    soc_on = float(config.get("socOnThreshold", config.get("socMin", 95)))
    soc_off_raw = float(config.get("socOffThreshold", 80))
    soc_off = soc_off_raw if soc_off_raw < soc_on else soc_on - 10
    solar_floor = float(config.get("solarFloorW", config.get("minPvThresholdW", 500)))
    min_duration = float(config.get("minDurationMinutes", config.get("sustainMinutes", 5)))

    status = exec_state.get("status", "idle")
    now_ms = _now_ms(now_utc)
    soc = float(reading.get("batterySoc", 0) or 0)
    pv_power = float(reading.get("pvPower") or 0)
    battery_power = float(reading.get("batteryPower") or 0)

    # ON condition: SOC ≥ socOn AND PV ≥ solar_floor
    condition_met = soc >= soc_on and pv_power >= solar_floor

    if status == "idle":
        if condition_met:
            new_state["status"] = "pending_condition"
            new_state["conditionMetSince"] = now_utc.isoformat()
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    if status == "pending_condition":
        if not condition_met:
            new_state["status"] = "idle"
            new_state["conditionMetSince"] = None
            return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
        cms_ms = _iso_to_ms(exec_state.get("conditionMetSince"))
        elapsed_min = ((now_ms - cms_ms) / 60_000.0) if cms_ms is not None else 0.0
        if elapsed_min >= min_duration:
            # Reset to idle — device_control_unavailable, no spurious lifecycle
            new_state["status"] = "idle"
            new_state["conditionMetSince"] = None
            return EvaluatorDecision(
                action="skip",
                commands=[],
                skip_reason="device_control_unavailable",
                new_state=new_state,
            )
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    # active
    # OFF condition: SOC < socOff OR (pvPower < solar_floor AND battery discharging)
    battery_discharging = battery_power < -50.0
    off_condition = soc < soc_off or (pv_power < solar_floor and battery_discharging)

    if not off_condition:
        new_state.pop("conditionLostSince", None)
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    # OFF condition — start/continue hysteresis timer
    lost_str = exec_state.get("conditionLostSince")
    if not lost_str:
        new_state["conditionLostSince"] = now_utc.isoformat()
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
    lost_ms = _iso_to_ms(lost_str)
    elapsed_min = ((now_ms - lost_ms) / 60_000.0) if lost_ms is not None else 0.0
    if elapsed_min < min_duration:
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    # Sustained → deactivate
    new_state["status"] = "idle"
    new_state["conditionMetSince"] = None
    new_state["conditionLostSince"] = None
    return EvaluatorDecision(action="deactivate", commands=[], new_state=new_state)


# ─────────────────── battery_maintenance ─────────────────────────────────────

def _eval_battery_maintenance(
    event: dict, reading: dict, exec_state: dict, new_state: dict,
    now_utc: datetime, tz: str,
) -> EvaluatorDecision:
    """Periodic full-charge cycle.

    Always dispatches set_solar_sell{solarSell:0} in-window.
    SOC ≥ 99 → mark completed.
    After gridFallbackHour (local) → dispatch set_battery_charge to 100%.
    TS lines 1756-2042.
    """
    config = event.get("config") or {}
    schedule = event.get("schedule") or {}
    fallback_hour = int(config.get("gridFallbackHour", 17))
    now_hour = _local_hour(now_utc, tz)
    soc = float(reading.get("batterySoc", 0) or 0)
    status = exec_state.get("status", "idle")

    cmds: list = [("set_solar_sell", {"solarSell": 0})]

    new_state["status"] = "active"
    if not exec_state.get("lastActivatedAt"):
        new_state["lastActivatedAt"] = now_utc.isoformat()

    # Cache prior solar sell on first invocation (we default False without inverter doc)
    if exec_state.get("maintenancePriorSolarSell") is None:
        new_state["maintenancePriorSolarSell"] = False

    # SOC ≥ 99 → cycle complete
    if soc >= 99:
        new_state["completed"] = True
        return EvaluatorDecision(action="hold", commands=cmds, new_state=new_state)

    # Grid fallback: after the configured local hour, enable grid charge to 100%
    if now_hour >= fallback_hour:
        charge_payload: dict = {
            "gridChargeEnabled": True,
            "gridChargeSoc": 100,
            "slotIndex": 0,
            "disableOtherSlots": True,
            "slotStart": _time_to_packed_hhmm(schedule.get("startTime", "00:00")),
            "slotEnd": _time_to_packed_hhmm(schedule.get("endTime", "23:59")),
        }
        power_limit = config.get("chargePowerLimitW")
        if power_limit is not None:
            charge_payload["powerLimit"] = power_limit
        cmds.append(("set_battery_charge", charge_payload))
        new_state["gridFallbackFired"] = True

    if status == "idle":
        return EvaluatorDecision(action="activate", commands=cmds, new_state=new_state)
    return EvaluatorDecision(action="hold", commands=cmds, new_state=new_state)


# ─────────────────── use_battery ─────────────────────────────────────────────

def _eval_use_battery(
    event: dict, exec_state: dict, new_state: dict, now_utc: datetime,
) -> EvaluatorDecision:
    """In-window → activate workMode=2 + disable grid charge. Idempotent.

    TS lines 2802-2855.
    """
    if exec_state.get("status") == "active":
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    new_state["previousWorkMode"] = exec_state.get("previousWorkMode", 2)
    new_state["previousTimerEnabled"] = exec_state.get("previousTimerEnabled", True)
    new_state["status"] = "active"
    new_state["lastActivatedAt"] = now_utc.isoformat()
    return EvaluatorDecision(
        action="activate",
        commands=[
            ("set_work_mode", {"workMode": 2}),
            ("set_grid_charge_toggle", {"enabled": False}),
        ],
        new_state=new_state,
    )


# ─────────────────── gen_force ───────────────────────────────────────────────

def _eval_gen_force(
    event: dict, reading: dict, exec_state: dict, new_state: dict,
    now_utc: datetime, tz: str,
) -> EvaluatorDecision:
    """SOC + grid-presence-gated generator force with 1-min sustain.

    requireScheduledOutage is filtered before this is called (skip path).
    TS lines 2863-2995.
    """
    config = event.get("config") or {}
    grid_present = _derive_grid_present(reading)
    now_ms = _now_ms(now_utc)
    now_local_min = _local_total_minutes(now_utc, tz)

    signals = {
        "batterySoc": float(reading.get("batterySoc", 100) or 100),
        "gridPresent": grid_present,
        "outageHourNow": False,  # cloud-only, requireScheduledOutage already blocked
    }
    state_view = {
        "status": exec_state.get("status", "idle"),
        "conditionMetSince": exec_state.get("conditionMetSince"),
        "lastActivatedAt": exec_state.get("lastActivatedAt"),
        "lastDeactivatedAt": exec_state.get("lastDeactivatedAt"),
    }

    decision = _decide_gen_force(now_ms, now_local_min, signals, config, state_view)
    d_action = decision["action"]

    if d_action == "start_sustain":
        new_state["status"] = "pending_condition"
        new_state["conditionMetSince"] = now_utc.isoformat()
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    if d_action == "clear_sustain":
        new_state["status"] = "idle"
        new_state["conditionMetSince"] = None
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    if d_action == "activate":
        new_state["status"] = "active"
        new_state["lastActivatedAt"] = now_utc.isoformat()
        new_state["conditionMetSince"] = None
        return EvaluatorDecision(
            action="activate",
            commands=[("set_gen_force", {"on": True})],
            new_state=new_state,
        )

    if d_action == "deactivate":
        new_state["status"] = "idle"
        new_state["conditionMetSince"] = None
        new_state["lastDeactivatedAt"] = now_utc.isoformat()
        return EvaluatorDecision(
            action="deactivate",
            commands=[("set_gen_force", {"on": False})],
            new_state=new_state,
        )

    # none
    return EvaluatorDecision(action="hold", commands=[], new_state=new_state)


# ─────────────────── custom ───────────────────────────────────────────────────

def _eval_custom(
    event: dict, reading: dict, exec_state: dict, new_state: dict, now_utc: datetime,
) -> EvaluatorDecision:
    """Evaluate custom event local conditions (battery_soc, pv_power, load_power, grid).

    forecast_today / dam_price / scheduled_outage conditions are blocked
    before this function is called — they return skip at the top level.
    TS evaluator.ts lines 3043+; custom-event-decision.ts.
    """
    config = event.get("config") or {}
    actions = config.get("customActions") or []
    conditions = config.get("customConditions") or []
    sustain_minutes = float(config.get("sustainMinutes") or 0)

    now_ms = _now_ms(now_utc)
    grid_present = _derive_grid_present(reading)

    signals = {
        "batterySoc": reading.get("batterySoc"),
        "pvPowerW": float(reading.get("pvPower") or 0),
        "loadPowerW": float(reading.get("loadPower") or 0),
        "gridPresent": grid_present,
        # Cloud-only fields unavailable locally; skip guard already fired
        "outageHourNow": False,
        "forecastTodayKwh": None,
        "damPriceUahKwh": None,
    }

    state_view = {
        "status": exec_state.get("status", "idle"),
        "conditionMetSince": exec_state.get("conditionMetSince"),
        "conditionLostSince": exec_state.get("conditionLostSince"),
    }

    decision = _decide_custom_event(now_ms, conditions, signals, sustain_minutes, state_view)
    d_action = decision["action"]

    if d_action == "start_sustain":
        new_state["status"] = "pending_condition"
        new_state["conditionMetSince"] = now_utc.isoformat()
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    if d_action == "clear_sustain":
        new_state["status"] = "idle"
        new_state["conditionMetSince"] = None
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    if d_action == "activate":
        cmds = [
            (a["command"], dict(a.get("payload") or {}))
            for a in actions
            if a.get("command")
        ]
        new_state["status"] = "active"
        new_state["lastActivatedAt"] = now_utc.isoformat()
        new_state["conditionLostSince"] = None
        return EvaluatorDecision(action="activate", commands=cmds, new_state=new_state)

    if d_action == "deactivate":
        cmds = [
            (a["command"], dict(a["restorePayload"]))
            for a in actions
            if a.get("command") and a.get("restorePayload")
        ]
        new_state["status"] = "idle"
        new_state["conditionLostSince"] = None
        return EvaluatorDecision(action="deactivate", commands=cmds, new_state=new_state)

    # none — active + conditions still met, or idle + not met
    # Track release-side hysteresis onset when active + conditions just dropped
    if exec_state.get("status") == "active" and not _evaluate_conditions(conditions, signals, held=True) and not exec_state.get("conditionLostSince"):
        new_state["conditionLostSince"] = now_utc.isoformat()
    return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
