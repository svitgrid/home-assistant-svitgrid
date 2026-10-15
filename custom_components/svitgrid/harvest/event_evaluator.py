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
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)

# ─────────────────────────────── Window helpers ───────────────────────────────


def parse_time_to_minutes(hhmm: str) -> int:
    """Convert "HH:mm" to minutes since midnight.

    Mirrors TS: const [h, m] = time.split(':').map(Number); return h * 60 + m;
    """
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _is_one_time_event_date(schedule: dict, date_str: str) -> bool:
    """Port of isOneTimeEventDate: the start date, or the day after it when
    the window wraps midnight."""
    start_date = schedule.get("startDate")
    if not start_date or start_date == date_str:
        return True
    start = parse_time_to_minutes(schedule["startTime"])
    end = parse_time_to_minutes(schedule["endTime"])
    if end > start:
        return False
    next_day = (datetime.fromisoformat(start_date) + timedelta(days=1)).strftime("%Y-%m-%d")
    return next_day == date_str


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
      - 'always'        → a rule: in window, before any date check
      - 'none'          → on startDate; an overnight window also on the next
                           morning, until endTime
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
    # A rule (recurrence 'always', the app's «Постійно») has no window and no
    # dates: it is in window whenever it is enabled. The API returns before the
    # date checks too (evaluator.ts isEventInWindow).
    if schedule.get("recurrence") == "always":
        return True

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
    now_minutes = local_hours * 60 + local_minutes
    recurrence = schedule.get("recurrence", "daily")
    if recurrence == "none":
        # Only fires on its startDate, or on the next day for the morning half
        # of an overnight window (isOneTimeEventDate).
        if not _is_one_time_event_date(schedule, local_date_str):
            return False
        if start_date:
            # A one-time overnight window is [startTime@startDate,
            # endTime@startDate+1): on startDate only the evening half matches,
            # on the next day only the morning half (evaluator.ts case 'none').
            s_min = parse_time_to_minutes(schedule["startTime"])
            e_min = parse_time_to_minutes(schedule["endTime"])
            if e_min <= s_min:
                if start_date == local_date_str:
                    if now_minutes < s_min:
                        return False
                elif now_minutes >= e_min:
                    return False
    elif recurrence == "daily":
        pass  # Always active within date range
    elif recurrence in ("weekly", "custom"):
        weekdays = schedule.get("weekdays") or []
        if weekdays and not any(d % 7 == local_weekday for d in weekdays):
            return False

    # ── Time window ───────────────────────────────────────────────────────────
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

    action     : 'activate' | 'update' | 'deactivate' | 'hold' | 'skip'
                 'update' is a write while the event stays active (battery
                 maintenance's grid fallback, and its hand-back at SOC 99);
                 the scheduler dispatches it like a transition.
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
_CLOUD_CONDITION_TYPES: frozenset[str] = frozenset(
    {
        "forecast_today",
        "forecast_tomorrow",
        "dam_price",
        "scheduled_outage",
    }
)


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

    quiet = _in_quiet_hours(
        now_local_min, config.get("quietHoursStart"), config.get("quietHoursEnd")
    )
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
                "low_soc"
                if (soc_demand and soc_start is not None)
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


def _usable_grid_voltages(reading: dict) -> list[float]:
    """Port of usableGridVoltages: finite phase voltages above 0, else gridVoltage."""

    def usable(v) -> bool:
        return (
            isinstance(v, int | float)
            and not isinstance(v, bool)
            and math.isfinite(float(v))
            and float(v) > 0
        )

    raw = reading.get("phaseVoltages")
    phases = [float(v) for v in (raw if isinstance(raw, list) else []) if usable(v)]
    if phases:
        return phases
    gv = reading.get("gridVoltage")
    return [float(gv)] if usable(gv) else []


# Conditions that read the reading's numbers. A rule holding one of them does
# nothing on a reading older than _STALE_READING_MS (evaluator.ts
# hasReadingDependentCondition): acting on a stale SOC risks a false start.
_READING_CONDITION_TYPES = frozenset({"battery_soc", "pv_power", "load_power", "grid_voltage"})
_STALE_READING_MS = 15 * 60_000.0


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
            condition["op"],
            float(soc),
            float(condition["threshold"]),
            condition.get("releaseThreshold"),
            held,
        )
    if ctype == "pv_power":
        return _numeric_holds(
            condition["op"],
            float(signals.get("pvPowerW", 0)),
            float(condition["thresholdW"]),
            condition.get("releaseThresholdW"),
            held,
        )
    if ctype == "load_power":
        return _numeric_holds(
            condition["op"],
            float(signals.get("loadPowerW", 0)),
            float(condition["thresholdW"]),
            condition.get("releaseThresholdW"),
            held,
        )
    if ctype == "grid":
        gp = signals.get("gridPresent")
        if gp is None:
            return False
        return bool(gp) if condition.get("state") == "up" else not bool(gp)
    if ctype == "grid_voltage":
        # A sag reads the lowest usable phase, a surge the highest. Silence is
        # not a sag, and it never clears a latch: with no usable voltage the
        # condition keeps its previous state (custom-event-decision.ts).
        op = condition["op"]
        v = signals.get("gridVoltageMinV" if op == "lte" else "gridVoltageMaxV")
        if v is None:
            return held
        return _numeric_holds(
            op,
            float(v),
            float(condition["thresholdV"]),
            condition.get("releaseThresholdV"),
            held,
        )
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


# ─────────────────── What an event writes, and its restore ───────────────────


# The restore of a battery charge, as the cloud sends it: the six-slot table the
# activation's set_battery_charge pre-read (the ACK's ``prevSlots``), which
# WriteExecutor writes back slot by slot on the set_battery_charge recipe.
RESTORE_CHARGE = "restore_battery_charge"

# ``lastCommandError.reason`` for a restore with no captured prior value. The
# app words it as "could not read the inverter's settings".
_NO_PRIOR_REASON = "prev_read_failed"

# The execution-state key that keeps a register's prior value, by the command
# that writes the register. The scheduler fills them from the activation's
# ACKs (event_scheduler_loop.capture_priors): every one is read from the
# inverter by the write itself, never defaulted. The keys are the cloud's
# (CalendarEventDoc.executionState); the solar-sell bit keeps the key it had
# for battery maintenance in every mode.
COMMAND_PRIOR_KEYS = {
    "set_work_mode": "previousWorkMode",
    "set_grid_charge_toggle": "previousTimerEnabled",
    "set_solar_sell": "maintenancePriorSolarSell",
    "set_sell_power_cap": "previousSellPowerCapW",
    "set_gen_force": "previousGenForce",
    "set_gen_port_mode": "previousGenPortMode",
    "set_energy_pattern": "previousEnergyPattern",
    "set_battery_charge": "prevSlots",
}
PRIOR_KEYS = tuple(dict.fromkeys(COMMAND_PRIOR_KEYS.values()))

# The restore of each prior value. The sell cap uses the API's restore command,
# which WriteExecutor does not clamp to the set limit (#52); the slot table is
# written back whole.
_RESTORES = {
    "previousWorkMode": lambda v: ("set_work_mode", {"workMode": v}),
    "previousTimerEnabled": lambda v: ("set_grid_charge_toggle", {"gridChargeEnabled": bool(v)}),
    "maintenancePriorSolarSell": lambda v: ("set_solar_sell", {"solarSell": 1 if v else 0}),
    "previousSellPowerCapW": lambda v: ("restore_sell_power_cap", {"sellPowerCapW": v}),
    "previousGenForce": lambda v: ("set_gen_force", {"on": bool(v)}),
    "previousGenPortMode": lambda v: ("set_gen_port_mode", {"genPortMode": v}),
    "previousEnergyPattern": lambda v: ("set_energy_pattern", {"energyPattern": v}),
    "prevSlots": lambda v: (RESTORE_CHARGE, {"prevSlots": v}),
}

# Marks an event whose restore already ran inside its window: smart sell's PV
# release and the sell floor. Its priors stay for a re-activation in the same
# window, as the cloud keeps its snapshot, but nothing of it is on the inverter.
RELEASED_KEY = "restoredInWindow"

# Per-run markers a window end clears, so the next run starts fresh. The API's
# window-end reset clears the same maintenance markers.
_RUN_KEYS = ("completed", "gridFallbackFired", "belowMinSoc", RELEASED_KEY)

# Execution-state key of an event the app cancelled while it held a register
# (event_scheduler_loop.REMOVAL_KEY). Its restore is pending: it hands nothing
# back to another event.
_REMOVAL_KEY = "pendingRemoval"

# sunset-restore-guard.ts (#576): restoring Selling First (work mode 0) with
# less PV than this sells the battery at full power. The guard writes Zero
# Export To Load (1) instead, which works with or without a CT.
_SELLING_FIRST = 0
_ZERO_EXPORT_TO_LOAD = 1
_SELLING_FIRST_PV_FLOOR_W = 500


@dataclass(frozen=True)
class _Write:
    """One register an active event holds.

    ``key`` is the execution-state key of the register's prior value (None
    for a legacy custom command the island does not know). ``restore`` is a
    fixed restore that needs no prior: generator force ends with the port
    off, as in the API, and a legacy custom action may carry its own.
    """

    key: str | None
    command: str
    payload: dict
    restore: tuple[str, dict] | None = None
    sunset_guard: bool = False


def _charge_payload(config: dict, schedule: dict, target_soc, grid: bool) -> dict:
    payload: dict = {
        "gridChargeEnabled": grid,
        "gridChargeSoc": target_soc,
        "slotIndex": 0,
        "disableOtherSlots": True,
        "slotStart": _time_to_packed_hhmm(schedule.get("startTime", "00:00")),
        "slotEnd": _time_to_packed_hhmm(schedule.get("endTime", "23:59")),
    }
    # powerLimit only when the event sets one, and no chargeVoltage: the same
    # payload the API builds (buildBatteryChargePayload). The island knows no
    # battery chemistry or cell count, which is the API's chargeVoltage-0
    # case, so both registers keep the user's own values (write_compute skips
    # an absent field).
    power_limit = config.get("chargePowerLimitW")
    if power_limit is not None:
        payload["powerLimit"] = power_limit
    return payload


def _sell_writes(config: dict) -> list[_Write]:
    """sell_to_grid's writes, as activateSellToGrid sends them: Selling First,
    then the sell cap when the event sets one, then the TOU master off when
    ``disableTouDuringEvent`` is set."""
    writes = [_Write("previousWorkMode", "set_work_mode", {"workMode": 0}, sunset_guard=True)]
    cap = config.get("sellPowerCapW")
    if isinstance(cap, int | float) and not isinstance(cap, bool) and cap >= 0:
        writes.append(_Write("previousSellPowerCapW", "set_sell_power_cap", {"sellPowerCapW": cap}))
    if config.get("disableTouDuringEvent") is True:
        writes.append(
            _Write("previousTimerEnabled", "set_grid_charge_toggle", {"gridChargeEnabled": False})
        )
    return writes


# The app's custom action types (custom_action.dart, the API's
# CustomActionSchema) the island can write, and their payloads, as the API's
# buildActivatePayload builds them.
_CUSTOM_ACTIONS = {
    "work_mode": ("set_work_mode", lambda a: {"workMode": a["value"]}),
    "solar_sell": ("set_solar_sell", lambda a: {"solarSell": a["value"]}),
    "sell_power_cap": ("set_sell_power_cap", lambda a: {"sellPowerCapW": a["valueW"]}),
    "grid_charge_toggle": ("set_grid_charge_toggle", lambda a: {"gridChargeEnabled": a["enabled"]}),
    "gen_force": ("set_gen_force", lambda a: {"on": a["on"]}),
    "gen_port_mode": ("set_gen_port_mode", lambda a: {"genPortMode": a["value"]}),
    "energy_pattern": ("set_energy_pattern", lambda a: {"energyPattern": a["value"]}),
}


def _tou_slot_payload(action: dict) -> dict:
    """The API's single-slot ``tou_slots`` payload. Its whole-program form needs
    the cloud's settings mirror; the restore writes the six slots the write
    read, so the next slot's start this form moves is put back too."""
    payload: dict = {
        "slotIndex": action.get("slotIndex", 0),
        "gridChargeEnabled": action["gridChargeEnabled"],
        "gridChargeSoc": action["socTarget"],
        "powerLimit": action["powerW"],
    }
    if action.get("chargeVoltage") is not None:
        payload["chargeVoltage"] = action["chargeVoltage"]
    payload["slotStart"] = _time_to_packed_hhmm(action["startTime"])
    payload["slotEnd"] = _time_to_packed_hhmm(action["endTime"])
    payload["disableOtherSlots"] = False
    return payload


def _custom_write(action: dict) -> _Write | None:
    """One custom action as a write, in the app's shape or the legacy one.

    The legacy shape, ``{command, payload, restorePayload}``, keeps its own
    restore when it carries one. An action the island cannot write (a smart
    plug, an EV charger, a priority profile, or a register no recipe has) is
    skipped and logged.
    """
    command = action.get("command")
    if command:
        restore = action.get("restorePayload")
        return _Write(
            COMMAND_PRIOR_KEYS.get(command),
            command,
            dict(action.get("payload") or {}),
            (command, dict(restore)) if restore else None,
        )
    kind = action.get("type")
    try:
        if kind == "tou_slots":
            return _Write("prevSlots", "set_battery_charge", _tou_slot_payload(action))
        if kind in _CUSTOM_ACTIONS:
            name, build = _CUSTOM_ACTIONS[kind]
            return _Write(COMMAND_PRIOR_KEYS[name], name, build(action))
    except KeyError:
        _LOGGER.warning("Custom action %s is missing a field; skipped", action)
        return None
    _LOGGER.warning("Custom action type %r cannot be written by the island; skipped", kind)
    return None


def _event_writes(event: dict, exec_state: dict) -> list[_Write]:
    """The registers *event* writes while active, in activation order."""
    mode = event.get("mode", "")
    config = event.get("config") or {}
    schedule = event.get("schedule") or {}
    if mode == "battery_charge":
        source = config.get("chargeSource", "solar")
        grid = source in ("grid", "both", "solar_surplus_grid")
        payload = _charge_payload(config, schedule, config.get("targetSoc", 100), grid)
        return [_Write("prevSlots", "set_battery_charge", payload)]
    if mode == "sell_to_grid":
        return _sell_writes(config)
    if mode == "use_battery":
        return [
            _Write("previousWorkMode", "set_work_mode", {"workMode": 2}),
            _Write("previousTimerEnabled", "set_grid_charge_toggle", {"gridChargeEnabled": False}),
        ]
    if mode == "gen_force":
        # The API ends a generator force with the port off, not a prior value.
        return [
            _Write(
                "previousGenForce", "set_gen_force", {"on": True}, ("set_gen_force", {"on": False})
            )
        ]
    if mode == "battery_maintenance":
        writes = [_Write("maintenancePriorSolarSell", "set_solar_sell", {"solarSell": 0})]
        if exec_state.get("gridFallbackFired"):
            payload = _charge_payload(config, schedule, 100, True)
            writes.append(_Write("prevSlots", "set_battery_charge", payload))
        return writes
    if mode == "custom":
        writes = (_custom_write(a) for a in config.get("customActions") or [])
        return [w for w in writes if w is not None]
    return []


def has_landed_writes(exec_state: dict) -> bool:
    """Return True when a write of this event's run is still on the inverter.

    True for an active event, and for one whose activation failed partway:
    the scheduler keeps the prior value of every write that landed. False
    once the restore ran inside the window (``restoredInWindow``).
    """
    if exec_state.get(RELEASED_KEY):
        return False
    return exec_state.get("status") == "active" or any(
        exec_state.get(k) is not None for k in PRIOR_KEYS
    )


def _holder(key: str | None, others) -> _Write | None:
    """Another event's write of the register *key* names, while it holds it.

    Two events overlap on one register: the one that ends hands the register
    back to the one still running, rather than to the owner's value. The API
    skips the restore when another sell_to_grid event is active
    (deactivateSellToGrid); writing the other event's value also covers two
    modes that write different values to one register.
    """
    if key is None:
        return None
    for other in others or ():
        state = other.get("executionState") or {}
        if state.get(key) is None or state.get(RELEASED_KEY) or state.get(_REMOVAL_KEY):
            continue
        for write in _event_writes(other, state):
            if write.key == key:
                return write
    return None


def _guard_sunset(work_mode, reading: dict | None):
    """resolveRestoreWorkMode: never restore Selling First with no sun."""
    pv = (reading or {}).get("pvPower")
    if (
        work_mode == _SELLING_FIRST
        and isinstance(pv, int | float)
        and not isinstance(pv, bool)
        and pv < _SELLING_FIRST_PV_FLOOR_W
    ):
        _LOGGER.warning(
            "Not restoring Selling First with %s W of PV: it would sell the battery; "
            "restoring Zero Export To Load",
            pv,
        )
        return _ZERO_EXPORT_TO_LOAD
    return work_mode


def _restore_plan(
    event: dict,
    exec_state: dict,
    reading: dict | None = None,
    others=(),
    landed_only: bool = False,
) -> tuple[list, list]:
    """Return ``(restore commands, restores that cannot run)`` for an event.

    Each restore writes back the value the activation read from the inverter.
    A restore whose value was never read cannot run, and is named in the
    second list instead: there is nothing true to write. The old defaults
    (work mode 2, TOU master on, solar sell off, SOC 0 over the event's window
    in slot 1) overwrote whatever the owner had set.

    *landed_only* is the restore of an activation that failed partway: only
    the writes that landed (their prior is held) are put back, and a write
    that never landed is not reported. A register another event in *others*
    still holds is handed back to that event's value instead.
    """
    if landed_only and exec_state.get(RELEASED_KEY):
        return [], []
    commands: list = []
    missing: list = []
    writes = _event_writes(event, exec_state)
    # A prior held for a register the event's config no longer writes (the
    # owner edited the event while it ran) is still our write on the inverter.
    covered = {w.key for w in writes}
    writes += [
        _Write(key, _RESTORES[key](None)[0], {})
        for key in PRIOR_KEYS
        if key not in covered and exec_state.get(key) is not None
    ]
    for write in writes:
        holder = _holder(write.key, others)
        if holder is not None:
            commands.append((holder.command, dict(holder.payload)))
            continue
        prior = exec_state.get(write.key) if write.key else None
        if write.restore is not None:
            if not landed_only or prior is not None:
                commands.append((write.restore[0], dict(write.restore[1])))
            continue
        if write.key is None:
            continue
        if prior is None or (write.key == "prevSlots" and not prior):
            if not landed_only:
                missing.append(_RESTORES[write.key](None)[0])
            continue
        if write.sunset_guard:
            prior = _guard_sunset(prior, reading)
        commands.append(_RESTORES[write.key](prior))
    return commands, missing


def _restore_commands(event: dict, exec_state: dict) -> list:
    """Return the restore (de-activation) command list for the given mode."""
    return _restore_plan(event, exec_state)[0]


def unrestorable_command(event: dict, exec_state: dict) -> str | None:
    """Return the first restore that cannot run for want of a prior value, or None."""
    missing = _restore_plan(event, exec_state)[1]
    return missing[0] if missing else None


def _report_missing(event: dict, state: dict, missing: list, now_utc: datetime) -> None:
    """Record restores with no prior value in ``lastCommandError``, the field the
    app shows on the event. Retrying could not make a value up, so none is set."""
    if not missing:
        return
    _LOGGER.warning(
        "Event %s: %s not written back — the inverter's prior value was never read, "
        "so the inverter keeps the event's setting",
        event.get("id"),
        ", ".join(missing),
    )
    state["lastCommandError"] = {
        "command": missing[0],
        "reason": _NO_PRIOR_REASON,
        "at": now_utc.isoformat(),
    }


def _drop_run_state(state: dict) -> None:
    # lastActivatedAt stays: with lastDeactivatedAt it is the history the app
    # shows for a true island, which keeps no run records.
    for key in (
        "conditionMetSince",
        "conditionLostSince",
        *PRIOR_KEYS,
        *_RUN_KEYS,
    ):
        state.pop(key, None)


def restore_decision(
    event: dict,
    exec_state: dict,
    now_utc: datetime,
    reading: dict | None = None,
    others=(),
) -> tuple[list, dict]:
    """Return ``(restore commands, idle state)`` for ending an event.

    Used by the window end and by a cancel (delete or disable) from the app,
    so both put back the same values. An active event restores every write
    and reports, in ``lastCommandError``, a restore with no captured prior
    value. An event whose activation failed partway restores the writes that
    landed. *reading* feeds the sunset guard; *others* are the inverter's
    other events, whose held registers are handed back to them.
    """
    landed_only = exec_state.get("status") != "active"
    commands, missing = _restore_plan(event, exec_state, reading, others, landed_only)
    new_state = copy.deepcopy(exec_state)
    new_state["status"] = "idle"
    _drop_run_state(new_state)
    new_state[LAST_DEACTIVATED_KEY] = now_utc.isoformat()
    _report_missing(event, new_state, missing, now_utc)
    return commands, new_state


# ─────────────────── Main entry point ────────────────────────────────────────


# The island cannot run an event that needs cloud data: day planning, and any
# event that waits on a forecast, a day-ahead price or a scheduled outage. Such
# a skip is recorded on the event as `executionState.skippedReason`, which
# `GET /api/svitgrid/events` returns and the app shows as «Не працює без
# хмарної копії» (true-island contract, 2026-10-15).
SKIPPED_REASON_KEY = "skippedReason"
NEEDS_CLOUD = "needs_cloud"

# When the event last went active and last let go (the API's keys).
LAST_ACTIVATED_KEY = "lastActivatedAt"
LAST_DEACTIVATED_KEY = "lastDeactivatedAt"
_NEEDS_CLOUD_SKIPS: frozenset[str] = frozenset(
    {
        "cloud_only_mode",
        "requires_scheduled_outage",
        "cloud_only_condition",
        "cloud_only_threshold",
    }
)

# Config fields that gate an event on the forecast or the day-ahead price, in
# any mode: sell_to_grid's minForecastKwh and price floor, battery_charge's
# forecastThresholdKwh and price ceiling.
_CLOUD_THRESHOLD_FIELDS = (
    "minForecastKwh",
    "forecastThresholdKwh",
    "damPriceMinUahKwh",
    "damPriceMaxUahKwh",
)


def _cloud_skip_reason(event: dict) -> str | None:
    """Why the island cannot run *event* without cloud-only inputs, or None.

    Mirrors the app's cloudInputOf (cloud_dependency.dart), whose stricter
    list wins (owner, 2026-10-15): an event that waits for a forecast or a
    price is skipped, never run without its threshold. The shared vectors in
    tests/fixtures/needs_cloud_vectors.json keep the two in step.
    """
    config = event.get("config") or {}
    if event.get("mode") == "day_planning":
        return "cloud_only_mode"
    conditions = config.get("customConditions") or []
    if any(isinstance(c, dict) and c.get("type") in _CLOUD_CONDITION_TYPES for c in conditions):
        return "cloud_only_condition"
    if any(config.get(field) is not None for field in _CLOUD_THRESHOLD_FIELDS):
        return "cloud_only_threshold"
    if config.get("requireScheduledOutage") is True:
        return "requires_scheduled_outage"
    return None


def needs_cloud(event: dict) -> bool:
    """True when the island cannot run *event* without cloud-only inputs.

    The same test evaluate_event skips on, so the scheduler can say so for an
    event it has no reading to evaluate against yet.
    """
    return _cloud_skip_reason(event) is not None


def evaluate_event(
    event: dict,
    reading: dict,
    exec_state: dict,
    now_utc: datetime,
    tz: str,
    others=(),
) -> EvaluatorDecision:
    """Evaluate a calendar event; see `_evaluate_event`.

    Records `skippedReason: "needs_cloud"` on the new state of an event
    skipped for cloud-only inputs, and clears it from any other.
    """
    decision = _evaluate_event(event, reading, exec_state, now_utc, tz, others)
    state = {k: v for k, v in decision.new_state.items() if k != SKIPPED_REASON_KEY}
    if decision.action == "skip" and decision.skip_reason in _NEEDS_CLOUD_SKIPS:
        state[SKIPPED_REASON_KEY] = NEEDS_CLOUD
    # The run's times, which the app shows as the event's history on a true
    # island. The scheduler keeps this state only when the dispatch succeeds.
    if decision.action == "activate":
        state[LAST_ACTIVATED_KEY] = now_utc.isoformat()
    elif decision.action == "deactivate":
        state[LAST_DEACTIVATED_KEY] = now_utc.isoformat()
    decision.new_state = state
    return decision


def _evaluate_event(
    event: dict,
    reading: dict,
    exec_state: dict,
    now_utc: datetime,
    tz: str,
    others=(),
) -> EvaluatorDecision:
    """Evaluate a calendar event against a live reading.

    Returns EvaluatorDecision with action, commands, new_state, skip_reason.

    Cloud-only modes / conditions are NEVER evaluated — they return skip with
    an explicit reason so the scheduler can surface the reason to the user.

    *others* are the inverter's other events. A restore hands a register one
    of them still holds back to that event (see ``_holder``).

    An event that is not active but still has writes on the inverter (an
    activation that failed partway) puts them back as soon as it stops trying
    to activate: at window end, or in-window when its condition goes.
    """
    mode = event.get("mode", "")

    # ── Cloud-only inputs: day planning, a forecast, a price, an outage ───────
    cloud_reason = _cloud_skip_reason(event)
    if cloud_reason is not None:
        return EvaluatorDecision(
            action="skip",
            commands=[],
            new_state=dict(exec_state),
            skip_reason=cloud_reason,
        )

    # ── A rule an in-window event pauses (rule-pause.ts) ──────────────────────
    if _is_custom_rule(event):
        pauser = _pauser(event, exec_state, others, now_utc, tz)
        if pauser is not None:
            return _pause_rule(event, reading, exec_state, pauser, now_utc, others)
        if PAUSED_BY_KEY in exec_state:
            exec_state = {k: v for k, v in exec_state.items() if k != PAUSED_BY_KEY}

    # ── Window check ──────────────────────────────────────────────────────────
    schedule = event.get("schedule") or {}
    in_window = is_event_in_window(schedule, now_utc, tz)
    new_state = copy.deepcopy(exec_state)
    if _paused_by(new_state.get("skipReason")):
        new_state.pop("skipReason", None)

    if in_window and not _is_rule(event):
        blocked = _blocked_by_rule(event, exec_state, new_state, others)
        if blocked is not None:
            return blocked

    if not in_window:
        current_status = exec_state.get("status", "idle")
        if has_landed_writes(exec_state):
            restore_cmds, new_state = restore_decision(event, exec_state, now_utc, reading, others)
            return EvaluatorDecision(
                action="deactivate", commands=restore_cmds, new_state=new_state
            )
        if current_status == "pending_condition":
            new_state["status"] = "idle"
            new_state.pop("conditionMetSince", None)
        # Nothing of this run is at the inverter to restore. Priors kept for a
        # re-activation would otherwise outlive the window and stand in for
        # the next run's own reading.
        for key in (*PRIOR_KEYS, *_RUN_KEYS):
            new_state.pop(key, None)
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    # ── In-window: per-mode evaluation ────────────────────────────────────────
    decision = _eval_mode(mode, event, reading, exec_state, new_state, now_utc, tz, others)
    if decision.action == "activate":
        decision.new_state.pop(RELEASED_KEY, None)
        return decision
    if decision.action == "deactivate" and not decision.new_state.get(RELEASED_KEY):
        # The run's writes are restored: its priors must not pose as writes
        # still on the inverter, nor as the next run's own reading.
        for key in PRIOR_KEYS:
            decision.new_state.pop(key, None)
        return decision
    if (
        decision.action in ("hold", "skip")
        and exec_state.get("status") != "active"
        and decision.new_state.get("status") != "active"
        and has_landed_writes(exec_state)
    ):
        return _undo_partial(event, reading, exec_state, decision.new_state, others)
    return decision


# ─────────────────── Rule vs windowed event (rule-pause.ts, priority.ts) ─────

# The id of the windowed event a rule stands down for (the API's key).
PAUSED_BY_KEY = "pausedBy"
# skipReason of a windowed event that stands down for a rule the owner placed
# above it (the API's value).
_PAUSED_BY_EVENT = "paused_by_event:"


def _paused_by(skip_reason) -> bool:
    return isinstance(skip_reason, str) and skip_reason.startswith(_PAUSED_BY_EVENT)


def _is_rule(event: dict) -> bool:
    return (event.get("schedule") or {}).get("recurrence") == "always"


def _is_custom_rule(event: dict) -> bool:
    return event.get("mode") == "custom" and _is_rule(event)


def _rank(event: dict) -> tuple[float, int]:
    """priority.ts arbitrationRank: the owner's slot, then 0 for a windowed
    event and 1 for a rule. A slot that is not a non-negative integer is
    unplaced, below every placed automation."""
    raw = event.get("sortOrder")
    slot = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else math.inf
    return (slot, 1 if _is_rule(event) else 0)


def _outranks(a: dict, b: dict) -> bool:
    """priority.ts outranks: strictly higher priority."""
    return _rank(a) < _rank(b)


def _registers(event: dict, state: dict) -> set[str]:
    """The registers *event* writes, by the key of their prior value. Two
    events share a register family on the island when they share one."""
    return {w.key for w in _event_writes(event, state) if w.key}


def _live(other: dict) -> bool:
    state = other.get("executionState") or {}
    return other.get("enabled", True) is not False and not state.get(_REMOVAL_KEY)


def _pauser(rule: dict, state: dict, others, now_utc: datetime, tz: str) -> dict | None:
    """The windowed event that holds *rule* off, or None.

    One that shares a register with the rule, is enabled and runnable here,
    is active or in its window, and that the owner has not placed below the
    rule (pauserStillLive, rulesToPause).
    """
    mine = _registers(rule, state)
    if not mine:
        return None
    for other in others or ():
        if _is_rule(other) or not _live(other) or needs_cloud(other):
            continue
        if _outranks(rule, other):
            continue
        other_state = other.get("executionState") or {}
        if not mine & _registers(other, other_state):
            continue
        if other_state.get("status") == "active" or is_event_in_window(
            other.get("schedule") or {}, now_utc, tz
        ):
            return other
    return None


def _pause_rule(
    rule: dict, reading: dict, exec_state: dict, pauser: dict, now_utc: datetime, others
) -> EvaluatorDecision:
    """Stand *rule* down for *pauser*: restore what it holds, then stay idle."""
    new_state = copy.deepcopy(exec_state)
    new_state[PAUSED_BY_KEY] = pauser.get("id")
    new_state["status"] = "idle"
    new_state.pop("conditionMetSince", None)
    new_state.pop("conditionLostSince", None)
    if not has_landed_writes(exec_state):
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
    landed_only = exec_state.get("status") != "active"
    commands, missing = _restore_plan(rule, exec_state, reading, others, landed_only)
    for key in PRIOR_KEYS:
        new_state.pop(key, None)
    new_state.pop(RELEASED_KEY, None)
    _report_missing(rule, new_state, missing, now_utc)
    _LOGGER.info("Rule %s: paused by event %s", rule.get("id"), pauser.get("id"))
    return EvaluatorDecision(action="deactivate", commands=commands, new_state=new_state)


def _blocked_by_rule(
    event: dict, exec_state: dict, new_state: dict, others
) -> EvaluatorDecision | None:
    """Hold a windowed *event* that a rule still keeps from its register.

    A rule the owner placed above the event keeps the register: the event
    stands down with skipReason ``paused_by_event:<rule id>``. A rule the
    event outranks is paused by it (``_pauser``), and the event waits until
    the rule's restore has run, so it reads the owner's value, not the rule's.
    """
    if exec_state.get("status") == "active" or has_landed_writes(exec_state):
        return None
    mine = _registers(event, exec_state)
    if not mine:
        return None
    waiting = False
    for other in others or ():
        if not _is_custom_rule(other) or not _live(other):
            continue
        other_state = other.get("executionState") or {}
        holds = other_state.get("status") in ("active", "pending_condition")
        if not mine & _registers(other, other_state):
            continue
        if holds and not _outranks(event, other):
            new_state["status"] = "idle"
            new_state.pop("conditionMetSince", None)
            new_state["skipReason"] = f"{_PAUSED_BY_EVENT}{other.get('id')}"
            return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
        if has_landed_writes(other_state):
            waiting = True
    if waiting:
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
    return None


def _undo_partial(
    event: dict, reading: dict, exec_state: dict, new_state: dict, others
) -> EvaluatorDecision:
    """Put back the writes of an activation that failed partway.

    The event no longer wants to activate (its condition went, or the sell
    floor holds it off), so the writes that landed would otherwise stay on the
    inverter with nothing left to restore them.
    """
    commands, _missing = _restore_plan(event, exec_state, reading, others, landed_only=True)
    for key in PRIOR_KEYS:
        new_state.pop(key, None)
    _LOGGER.info(
        "Event %s: activation failed partway; putting back what landed (%s)",
        event.get("id"),
        ", ".join(c[0] for c in commands) or "nothing",
    )
    return EvaluatorDecision(action="deactivate", commands=commands, new_state=new_state)


def _eval_mode(
    mode: str,
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
    tz: str,
    others=(),
) -> EvaluatorDecision:
    if mode == "battery_charge":
        return _eval_battery_charge(event, reading, exec_state, new_state, now_utc)
    if mode == "sell_to_grid":
        return _eval_sell_to_grid(event, reading, exec_state, new_state, now_utc, others)
    if mode == "lower_consumption":
        return _eval_lower_consumption(event, reading, exec_state, new_state, now_utc)
    if mode == "consume_from_sun":
        return _eval_consume_from_sun(event, reading, exec_state, new_state, now_utc)
    if mode == "battery_maintenance":
        return _eval_battery_maintenance(event, reading, exec_state, new_state, now_utc, tz, others)
    if mode == "use_battery":
        return _eval_use_battery(event, exec_state, new_state, now_utc)
    if mode == "gen_force":
        return _eval_gen_force(event, reading, exec_state, new_state, now_utc, tz)
    if mode == "custom":
        return _eval_custom(event, reading, exec_state, new_state, now_utc, others)
    # Unknown mode → hold
    return EvaluatorDecision(action="hold", commands=[], new_state=new_state)


# ─────────────────── battery_charge ──────────────────────────────────────────


def _eval_battery_charge(
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
) -> EvaluatorDecision:
    """In-window → activate set_battery_charge once; hold when already active.

    An event with a forecast or price gate is skipped as needing the cloud.
    """
    if exec_state.get("status") == "active":
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    new_state["status"] = "active"
    new_state["lastActivatedAt"] = now_utc.isoformat()
    return EvaluatorDecision(
        action="activate",
        commands=_activation_commands(event, exec_state),
        new_state=new_state,
    )


def _activation_commands(event: dict, exec_state: dict) -> list:
    return [(w.command, dict(w.payload)) for w in _event_writes(event, exec_state)]


# ─────────────────── sell_to_grid ────────────────────────────────────────────


def _eval_sell_to_grid(
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
    others=(),
) -> EvaluatorDecision:
    """Simple mode: activate once. An event gated on the forecast or a price
    never gets here: it is skipped as needing the cloud.
    Smart mode: PV-power-gated with 2-min hysteresis on the OFF edge.

    Both stop at the battery floor ``minSocPercent`` and restore, and resume
    only when SOC is 5 points above it (evaluateSellToGrid, issue #717). The
    API's preferred floor behaviour, holding the battery with solar sell in a
    zero-export mode, is not ported: see ``_floor_check``.

    TS lines 2043-2348.
    """
    config = event.get("config") or {}
    sell_mode = config.get("sellMode", "simple")
    status = exec_state.get("status", "idle")
    now_ms = _now_ms(now_utc)

    floor = _floor_check(event, reading, exec_state, new_state, now_utc, others)
    if floor is not None:
        return floor

    def activate() -> EvaluatorDecision:
        # The prior values come from these writes' ACKs, which the scheduler
        # keeps; none is defaulted here.
        new_state["status"] = "active"
        new_state["lastActivatedAt"] = now_utc.isoformat()
        return EvaluatorDecision(
            action="activate",
            commands=_activation_commands(event, exec_state),
            new_state=new_state,
        )

    # ── Smart mode ────────────────────────────────────────────────────────────
    if sell_mode == "smart":
        pv_threshold = float(config.get("pvThresholdW", 500))
        pv_power = float(reading.get("pvPower") or 0)
        pv_above = pv_power >= pv_threshold

        if status == "idle":
            if not pv_above:
                return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
            new_state.pop("conditionMetSince", None)
            new_state.pop("conditionLostSince", None)
            return activate()

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
            # Hysteresis elapsed → deactivate + restore the captured values.
            new_state.pop("conditionLostSince", None)
            new_state["conditionMetSince"] = None
            return _release_in_window(event, reading, exec_state, new_state, now_utc, others)

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
            new_state.pop("conditionMetSince", None)
            return activate()

        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    # ── Simple mode (a forecast or price gate is skipped as needs_cloud) ──────
    if status == "active":
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
    return activate()


def _release_in_window(
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
    others,
) -> EvaluatorDecision:
    """Restore an active sale inside its window and wait in pending_condition.

    The captured values stay for a re-activation in this window, as the cloud
    keeps its snapshot; ``restoredInWindow`` records that none of the event's
    writes is on the inverter any more.
    """
    commands, missing = _restore_plan(event, exec_state, reading, others)
    new_state["status"] = "pending_condition"
    new_state[RELEASED_KEY] = True
    _report_missing(event, new_state, missing, now_utc)
    return EvaluatorDecision(action="deactivate", commands=commands, new_state=new_state)


# evaluator.ts SELL_MIN_SOC_RESUME_HYSTERESIS_PERCENT: once selling stopped at
# the floor, SOC must climb this far above it before selling resumes.
_SELL_RESUME_HYSTERESIS = 5


def _floor_check(
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
    others,
) -> EvaluatorDecision | None:
    """The sell floor ``minSocPercent`` (issue #717), or None to sell on.

    At or below the floor an active sale is restored; below floor + 5 after
    that, no sale starts. This is the API's fallback, which it takes for a
    model it cannot tell to sell only the surplus. Its preferred hold (solar
    sell on, work mode zero-export, battery held at the floor) is not ported:
    the island cannot tell whether the executor writes the work mode with the
    solar-sell bit, and a hold that half-lands keeps selling the battery.
    """
    floor = (event.get("config") or {}).get("minSocPercent")
    soc = reading.get("batterySoc")
    if not isinstance(floor, int | float) or not isinstance(soc, int | float):
        return None
    was_held = exec_state.get("belowMinSoc") is True
    below = soc < floor + _SELL_RESUME_HYSTERESIS if was_held else soc <= floor
    if not below:
        if was_held:
            _LOGGER.info(
                "Event %s: battery back above the floor; selling may resume", event.get("id")
            )
            new_state["belowMinSoc"] = False
        return None
    new_state["belowMinSoc"] = True
    if exec_state.get("status") != "active":
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
    _LOGGER.info(
        "Event %s: battery at %s %% reached the floor of %s %%; stopping the sale",
        event.get("id"),
        soc,
        floor,
    )
    new_state["conditionMetSince"] = None
    new_state.pop("conditionLostSince", None)
    return _release_in_window(event, reading, exec_state, new_state, now_utc, others)


# ─────────────────── lower_consumption ───────────────────────────────────────


def _eval_lower_consumption(
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
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
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
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
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
    tz: str,
    others=(),
) -> EvaluatorDecision:
    """Periodic full-charge cycle: the legacy body of the API's
    evaluateBatteryMaintenanceCharge (no fill/drain windows, no due-gate).

    - Activation writes set_solar_sell {solarSell: 0}. Its ACK reports the
      prior bit, which the scheduler keeps as ``maintenancePriorSolarSell``.
    - SOC >= 99 marks the run completed. A grid fallback already written is
      handed back at once (restore_battery_charge from the table its
      set_battery_charge read), as the API's endMaintenanceSlotHold does, and
      a completed run never re-arms it.
    - From gridFallbackHour (local) with SOC < 100, set_battery_charge to 100 %
      once per run. When that hour comes after activation, the write is an
      ``update``: the scheduler dispatches it while the event stays active.
      Before, it rode on a ``hold``, which the scheduler never dispatches.
    """
    config = event.get("config") or {}
    schedule = event.get("schedule") or {}
    fallback_hour = int(config.get("gridFallbackHour", 17))
    now_hour = _local_hour(now_utc, tz)
    soc = float(reading.get("batterySoc", 0) or 0)
    activating = exec_state.get("status", "idle") != "active"

    cmds: list = []
    if activating:
        cmds.append(("set_solar_sell", {"solarSell": 0}))
        new_state["status"] = "active"
        new_state["lastActivatedAt"] = now_utc.isoformat()

    if soc >= 99:
        new_state["completed"] = True
        if exec_state.get("gridFallbackFired"):
            holder = _holder("prevSlots", others)
            if holder is not None:
                cmds.append((holder.command, dict(holder.payload)))
            elif exec_state.get("prevSlots"):
                cmds.append((RESTORE_CHARGE, {"prevSlots": exec_state["prevSlots"]}))
            else:
                _report_missing(event, new_state, [RESTORE_CHARGE], now_utc)
            new_state.pop("gridFallbackFired", None)
            new_state.pop("prevSlots", None)
    elif (
        now_hour >= fallback_hour
        and not exec_state.get("completed")
        and not exec_state.get("gridFallbackFired")
    ):
        cmds.append(("set_battery_charge", _charge_payload(config, schedule, 100, True)))
        new_state["gridFallbackFired"] = True

    if activating:
        return EvaluatorDecision(action="activate", commands=cmds, new_state=new_state)
    if cmds:
        return EvaluatorDecision(action="update", commands=cmds, new_state=new_state)
    return EvaluatorDecision(action="hold", commands=[], new_state=new_state)


# ─────────────────── use_battery ─────────────────────────────────────────────


def _eval_use_battery(
    event: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
) -> EvaluatorDecision:
    """In-window → activate workMode=2 + disable grid charge. Idempotent.

    TS lines 2802-2855.
    """
    if exec_state.get("status") == "active":
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    # previousWorkMode and previousTimerEnabled come from the two ACKs
    # (prevWorkMode, prevGridChargeEnabled), kept by the scheduler.
    new_state["status"] = "active"
    new_state["lastActivatedAt"] = now_utc.isoformat()
    return EvaluatorDecision(
        action="activate",
        commands=_activation_commands(event, exec_state),
        new_state=new_state,
    )


# ─────────────────── gen_force ───────────────────────────────────────────────


def _eval_gen_force(
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
    tz: str,
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
    event: dict,
    reading: dict,
    exec_state: dict,
    new_state: dict,
    now_utc: datetime,
    others=(),
) -> EvaluatorDecision:
    """Evaluate custom event local conditions (battery_soc, pv_power, load_power, grid).

    forecast_today / dam_price / scheduled_outage conditions are blocked
    before this function is called — they return skip at the top level.
    TS evaluator.ts lines 3043+; custom-event-decision.ts.

    The actions are the app's ``{type, ...}`` objects (see ``_custom_write``);
    each restore writes back the value its write read from the inverter, as
    the API's buildRestorePayload does from its snapshot.
    """
    config = event.get("config") or {}
    conditions = config.get("customConditions") or []
    sustain_minutes = float(config.get("sustainMinutes") or 0)

    now_ms = _now_ms(now_utc)

    # An event with nothing to do is dropped, as the API drops it. A rule
    # whose only setting is the notification has something to do.
    if not config.get("customActions") and config.get("notifyOnStartStop") is not True:
        return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    # Stale-reading guard: no state change on a reading 15 minutes old.
    if any(isinstance(c, dict) and c.get("type") in _READING_CONDITION_TYPES for c in conditions):
        reading_ms = _iso_to_ms(reading.get("timestamp"))
        if reading_ms is not None and now_ms - reading_ms > _STALE_READING_MS:
            _LOGGER.debug("Event %s: stale reading, deferring", event.get("id"))
            return EvaluatorDecision(action="hold", commands=[], new_state=new_state)

    grid_present = _derive_grid_present(reading)
    voltages = _usable_grid_voltages(reading)

    signals = {
        "batterySoc": reading.get("batterySoc"),
        "pvPowerW": float(reading.get("pvPower") or 0),
        "loadPowerW": float(reading.get("loadPower") or 0),
        "gridPresent": grid_present,
        "gridVoltageMinV": min(voltages) if voltages else None,
        "gridVoltageMaxV": max(voltages) if voltages else None,
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
        new_state["status"] = "active"
        new_state["lastActivatedAt"] = now_utc.isoformat()
        new_state["conditionLostSince"] = None
        return EvaluatorDecision(
            action="activate",
            commands=_activation_commands(event, exec_state),
            new_state=new_state,
        )

    if d_action == "deactivate":
        commands, missing = _restore_plan(event, exec_state, reading, others)
        new_state["status"] = "idle"
        new_state["conditionLostSince"] = None
        _report_missing(event, new_state, missing, now_utc)
        return EvaluatorDecision(action="deactivate", commands=commands, new_state=new_state)

    # none — active + conditions still met, or idle + not met
    # Track release-side hysteresis onset when active + conditions just dropped
    if (
        exec_state.get("status") == "active"
        and not _evaluate_conditions(conditions, signals, held=True)
        and not exec_state.get("conditionLostSince")
    ):
        new_state["conditionLostSince"] = now_utc.isoformat()
    return EvaluatorDecision(action="hold", commands=[], new_state=new_state)
