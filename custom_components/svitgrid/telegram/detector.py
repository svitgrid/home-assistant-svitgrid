"""Outage, grid-restored and battery detection for island Telegram alerts.

A pure port of the cloud's `evaluateOutageAlerts` (outage-evaluator.ts) with the
household rule from the island Telegram spec: one message per household, out
while any inverter has alerted, restored when the last of them clears. The
60-minute cooldown applies to outage alerts only: an outage within 60 minutes
of the last sent one is suppressed together with its restore. A sent outage
always gets its restore, so there is no restore cooldown. The restore waits
5 minutes of steady grid, as the cloud's does, and a drop inside that wait
continues the outage already announced. The shared cases in
`tests/fixtures/island_alert_cases.json` pin the behaviour; the Dart twin runs
the same file.

The battery alerts (svitgrid D-2147) are per inverter, as the cloud's are: low
battery on a crossing below 20 % (alert-evaluator.ts), critical below 8 %
latched until 12 % (critical-battery-evaluator.ts), and full at 100 % once the
charge has tapered, latched until under 70 % (battery-full-evaluator.ts), each
behind the cloud's per-type cooldown. `tests/fixtures/island_battery_alert_cases.json`
is computed by running those evaluators.

`state` holds only dict, list, str, int, float, bool and None, so the runtime
can persist it as JSON.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from ..edge_reading_pipeline import (
    _debounce_grid_present,
    _iso_ms,
    _parse_ms,
    derive_grid_present,
    js_round,
    normalize_inbound_reading,
)

OUTAGE_DEBOUNCE_MS = 5 * 60 * 1000
RESTORE_DEBOUNCE_MS = 5 * 60 * 1000
COOLDOWN_MS = 60 * 60 * 1000

LOW_BATTERY_THRESHOLD = 20
CRITICAL_BATTERY_THRESHOLD = 8
CRITICAL_BATTERY_REARM_THRESHOLD = 12
BATTERY_FULL_THRESHOLD = 100
BATTERY_FULL_REARM_THRESHOLD = 70
BATTERY_FULL_TAPER_POWER_W = 500
BATTERY_FULL_TAPER_READINGS = 2
MAX_SOC_DROP_PER_READING = 30

# The cloud's COOLDOWN_MINUTES (functions/src/notifications.ts), per inverter and type.
BATTERY_COOLDOWN_MS = {
    "low_battery": 360 * 60 * 1000,
    "critical_battery": 360 * 60 * 1000,
    "battery_full": 60 * 60 * 1000,
}

AlertType = Literal["outage", "grid_restored", "low_battery", "critical_battery", "battery_full"]


@dataclass(frozen=True)
class Alert:
    """One alert to send. An outage or a restore is household-level; a battery
    alert names the inverter it is about."""

    type: AlertType
    household_id: str
    event_at_ms: int
    battery_soc: int | None
    inverter_id: str | None = None


def _round(value: Any) -> int | None:
    """`js_round` for a finite number; None for anything else."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return js_round(value) if math.isfinite(value) else None


def _grid_present(inv: dict, reading: dict) -> bool | None:
    if "gridPresent" in reading:
        present = reading["gridPresent"]
        raw = present if isinstance(present, bool) else None
        stored = raw
    else:
        raw = derive_grid_present(normalize_inbound_reading(reading))
        stored = _debounce_grid_present(raw, inv.get("rawGrid"), inv.get("storedGrid"))
    inv["rawGrid"] = raw
    inv["storedGrid"] = stored
    return stored


def evaluate(
    state: dict, household_id: str, inverter_id: str, reading: dict, now_ms: int
) -> tuple[dict, list[Alert]]:
    """Feed one accepted reading; return the new state and any alerts to send."""
    state = deepcopy(state) if state else {}
    hh = state.setdefault("households", {}).setdefault(household_id, {})
    invs = hh.setdefault("inverters", {})
    inv = invs.setdefault(inverter_id, {"gridDownSince": None, "outageAlerted": False})

    grid = _grid_present(inv, reading)
    soc = reading.get("batterySoc")
    fired: str | None = None
    restored_at_ms: int | None = None

    if grid is False:
        # Already announced: the outage continues, or the grid dropped again
        # inside the restore wait. Either way nothing new is sent.
        if inv["outageAlerted"]:
            inv["gridRestoredSince"] = None
            inv["gridRestoredAtMs"] = None
        if reading.get("gridConfirmedSource") == "edge" and not inv["outageAlerted"]:
            inv["gridDownSince"] = (
                reading.get("gridConfirmedAt") or inv["gridDownSince"] or _iso_ms(now_ms)
            )
            inv["outageAlerted"] = True
            fired = "outage"
        elif not inv["gridDownSince"]:
            inv["gridDownSince"] = _iso_ms(now_ms)
            inv["outageAlerted"] = False
        else:
            since = _parse_ms(inv["gridDownSince"])
            if (
                since is not None
                and now_ms - since >= OUTAGE_DEBOUNCE_MS
                and not inv["outageAlerted"]
            ):
                inv["outageAlerted"] = True
                fired = "outage"
    elif grid is True:
        restored_since = _parse_ms(inv.get("gridRestoredSince"))
        if not inv["outageAlerted"]:
            inv["gridDownSince"] = None
            inv["gridRestoredSince"] = None
            inv["gridRestoredAtMs"] = None
        elif restored_since is None:
            # The restore waits for the grid to hold, as the cloud's does.
            inv["gridRestoredSince"] = _iso_ms(now_ms)
            at = _parse_ms(reading.get("timestamp"))
            inv["gridRestoredAtMs"] = now_ms if at is None else at
        elif now_ms - restored_since >= RESTORE_DEBOUNCE_MS:
            fired = "grid_restored"
            at = inv.get("gridRestoredAtMs")
            restored_at_ms = at if type(at) is int else restored_since
            inv["gridDownSince"] = None
            inv["outageAlerted"] = False
            inv["gridRestoredSince"] = None
            inv["gridRestoredAtMs"] = None

    alerts: list[Alert] = []
    any_out = any(i["outageAlerted"] for i in invs.values())

    if fired == "outage" and not hh.get("out"):
        hh["out"] = True
        last = hh.get("lastOutageSentMs")
        if last is not None and now_ms - last < COOLDOWN_MS:
            hh["suppressed"] = True
        else:
            hh["suppressed"] = False
            hh["lastOutageSentMs"] = now_ms
            starts = [
                ms
                for i in invs.values()
                if i["outageAlerted"] and (ms := _parse_ms(i["gridDownSince"])) is not None
            ]
            alerts.append(
                Alert(
                    "outage",
                    household_id,
                    min(starts) if starts else now_ms,
                    _round(soc if soc is not None else 0),
                )
            )

    if hh.get("out") and not any_out:
        hh["out"] = False
        if not hh.get("suppressed") and fired == "grid_restored":
            alerts.append(
                Alert(
                    "grid_restored",
                    household_id,
                    now_ms if restored_at_ms is None else restored_at_ms,
                    _round(soc),
                )
            )
        hh["suppressed"] = False

    alerts += _battery_alerts(inv, household_id, inverter_id, reading, now_ms)
    return state, alerts


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None


def _soc_glitch(soc: float, source: str | None, prev: float | None, prev_source: Any) -> bool:
    """`isSocGlitch` (soc-glitch-guard.ts): a drop the battery could not have
    made in one reading, or a drop off a BMS figure onto the inverter's own."""
    if prev is None or prev <= 0:
        return False
    if prev - soc > MAX_SOC_DROP_PER_READING:
        return True
    return soc < prev and prev_source == "bms" and source != "bms"


def _battery_alerts(
    inv: dict, household_id: str, inverter_id: str, reading: dict, now_ms: int
) -> list[Alert]:
    """The cloud's three battery evaluators for one inverter's reading.

    Updates `inv` in place and returns what is due past the cooldown.
    """
    soc = _finite(reading.get("batterySoc"))
    raw_source = reading.get("batterySocSource")
    source = raw_source if isinstance(raw_source, str) else None
    prev = _finite(inv.get("prevSoc"))
    prev_source = inv.get("prevSocSource")
    inv["prevSoc"] = soc
    inv["prevSocSource"] = source
    if soc is None:
        return []

    fired: list[str] = []
    glitch = _soc_glitch(soc, source, prev, prev_source)

    # Low: a crossing below 20 %, never off a missing or zero previous figure.
    if (
        soc > 0
        and prev is not None
        and prev > 0
        and not glitch
        and soc < LOW_BATTERY_THRESHOLD
        and prev >= LOW_BATTERY_THRESHOLD
    ):
        fired.append("low_battery")

    # Critical: under 8 %, latched until SoC is back at 12 %.
    if soc > 0:
        latched = inv.get("criticalBatteryAlerted") is True
        if latched and soc >= CRITICAL_BATTERY_REARM_THRESHOLD:
            inv["criticalBatteryAlerted"] = False
        elif not latched and soc < CRITICAL_BATTERY_THRESHOLD and not glitch:
            inv["criticalBatteryAlerted"] = True
            fired.append("critical_battery")

    # Full: 100 % once the charge has tapered for two readings in a row,
    # latched until SoC drops under 70 %. The add-on knows no bank capacity,
    # so the taper is the cloud's power rule, or SoC alone with no power figure.
    taper = inv.get("batteryFullTaperCount")
    taper = taper if type(taper) is int else 0
    if inv.get("batteryFullAlerted") is True:
        if soc < BATTERY_FULL_REARM_THRESHOLD:
            inv["batteryFullAlerted"] = False
            inv["batteryFullTaperCount"] = 0
    elif soc < BATTERY_FULL_THRESHOLD:
        inv["batteryFullTaperCount"] = 0
    else:
        power = _finite(reading.get("batteryPower"))
        tapered = None if power is None else power < BATTERY_FULL_TAPER_POWER_W
        if tapered is False:
            inv["batteryFullTaperCount"] = 0
        elif tapered is True and taper + 1 < BATTERY_FULL_TAPER_READINGS:
            inv["batteryFullTaperCount"] = taper + 1
        else:
            inv["batteryFullAlerted"] = True
            inv["batteryFullTaperCount"] = 0
            fired.append("battery_full")

    sent = inv.get("batteryAlertSentMs")
    if not isinstance(sent, dict):
        sent = inv["batteryAlertSentMs"] = {}
    alerts: list[Alert] = []
    for kind in fired:
        last = sent.get(kind)
        if isinstance(last, int | float) and now_ms - last < BATTERY_COOLDOWN_MS[kind]:
            continue
        sent[kind] = now_ms
        alerts.append(Alert(kind, household_id, now_ms, _round(soc), inverter_id))
    return alerts
