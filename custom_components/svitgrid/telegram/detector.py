"""Outage and grid-restored detection for island Telegram alerts.

A pure port of the cloud's `evaluateOutageAlerts` (outage-evaluator.ts) with the
household rule from the island Telegram spec: one message per household, out
while any inverter has alerted, restored when the last of them clears, and an
outage within 60 minutes of the last sent one is suppressed together with its
restore. The shared cases in `tests/fixtures/island_alert_cases.json` pin the
behaviour; the Dart twin runs the same file.

`state` holds only dict, list, str, int, float, bool and None, so the runtime
can persist it as JSON.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ..edge_reading_pipeline import _debounce_grid_present, derive_grid_present

OUTAGE_DEBOUNCE_MS = 5 * 60 * 1000
COOLDOWN_MS = 60 * 60 * 1000


@dataclass(frozen=True)
class Alert:
    """One household-level alert to send."""

    type: Literal["outage", "grid_restored"]
    household_id: str
    event_at_ms: int
    battery_soc: int | None


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="milliseconds")


def _parse_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _round(value: Any) -> int | None:
    """Round half up, as JavaScript's `Math.round` does."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    return math.floor(value + 0.5)


def _grid_present(inv: dict, reading: dict) -> bool | None:
    if "gridPresent" in reading:
        present = reading["gridPresent"]
        raw = present if isinstance(present, bool) else None
        stored = raw
    else:
        raw = derive_grid_present(reading)
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

    if grid is False:
        if reading.get("gridConfirmedSource") == "edge" and not inv["outageAlerted"]:
            inv["gridDownSince"] = (
                reading.get("gridConfirmedAt") or inv["gridDownSince"] or _iso(now_ms)
            )
            inv["outageAlerted"] = True
            fired = "outage"
        elif not inv["gridDownSince"]:
            inv["gridDownSince"] = _iso(now_ms)
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
        if inv["outageAlerted"]:
            fired = "grid_restored"
        inv["gridDownSince"] = None
        inv["outageAlerted"] = False

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
            at = _parse_ms(reading.get("timestamp"))
            alerts.append(
                Alert("grid_restored", household_id, now_ms if at is None else at, _round(soc))
            )
        hh["suppressed"] = False

    return state, alerts
