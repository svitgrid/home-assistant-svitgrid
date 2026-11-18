"""The cloud's string-outage rule for an island (svitgrid D-2155).

A port of `services/functions/src/string-health-rule.ts` and of the per-inverter
gates of the cloud's weekly sweep (`runStringHealthSweep`), which skips a
local-only island. A string is dead when, on a clearly alive array, it gives
almost nothing while a sibling gives plenty, and it is also far below its own
`peakPowerWp`. The string layout comes from the app (`solar_strings.py`); with
no layout, or fewer than two strings, nothing is checked.

`island_string_health_cases.json`, exported from the cloud's code, pins it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

STRING_WINDOW_DAYS = 7
ARRAY_ALIVE_MIN_W = 1500
SIBLING_DEAD_FRACTION = 0.20
CONFIG_DEAD_FRACTION = 0.40
SIBLING_ALIVE_MIN_W = 1000
MIN_CONFIGURED_WP = 500
MAX_PV_CHANNELS = 4
MIN_SWEEP_SAMPLES = 50
# The cloud's `string_offline` cooldown (COOLDOWN_MINUTES, functions/src/notifications.ts).
COOLDOWN_MS = 10080 * 60 * 1000

PV_KEYS = ("pvPower", *(f"pvPower{i}" for i in range(1, MAX_PV_CHANNELS + 1)))


def _num(value: Any) -> float:
    """`num` in the cloud: a finite number, else 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return value if math.isfinite(value) else 0


@dataclass(frozen=True)
class StringPeaks:
    peak_w: list[float]
    array_peak_w: float
    sample_count: int
    # How many `pvPower{n}` keys the readings carry; None for an empty window.
    reported_channels: int | None


@dataclass(frozen=True)
class SweepResult:
    """What one sweep decided for one inverter."""

    checked: bool
    health: list[dict[str, Any]] = field(default_factory=list)
    # The newly dead strings, joined as the cloud joins them, when an alert goes out.
    alert_string_id: str | None = None


def reduce_string_peaks(readings: list[dict], string_count: int) -> StringPeaks:
    """`reduceStringPeaks`: per-string and array peaks over the window.

    Key presence, not value, counts a channel: a channel reading 0 all week is
    a candidate dead string, an absent one is a register the inverter lacks.
    """
    peak_w: list[float] = [0] * string_count
    array_peak = 0
    channels: set[int] = set()
    for reading in readings:
        if not isinstance(reading, dict):
            continue
        for i in range(string_count):
            peak_w[i] = max(peak_w[i], _num(reading.get(f"pvPower{i + 1}")))
        for i in range(1, MAX_PV_CHANNELS + 1):
            if f"pvPower{i}" in reading:
                channels.add(i)
        array_peak = max(array_peak, _num(reading.get("pvPower")))
    return StringPeaks(
        peak_w=peak_w,
        array_peak_w=array_peak,
        sample_count=len(readings),
        reported_channels=None if not readings else len(channels),
    )


def _string(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def detect_dead_strings(strings: list, peaks: StringPeaks) -> list[dict[str, Any]]:
    """`detectDeadStrings`: one health row per configured string."""
    if len(strings) < 2:
        return []
    obs = [peaks.peak_w[i] if i < len(peaks.peak_w) else 0 for i in range(len(strings))]
    mapping_unknown = peaks.reported_channels is not None and peaks.reported_channels < len(strings)
    out = []
    for i, raw in enumerate(strings):
        config = _string(raw)
        configured_wp = _num(config.get("peakPowerWp"))
        observed = obs[i]
        best_sibling = max(v for j, v in enumerate(obs) if j != i)
        string_id = config.get("id") if isinstance(config.get("id"), str) else f"#{i}"
        config_flaggable = configured_wp >= MIN_CONFIGURED_WP
        dead = (
            not mapping_unknown
            and peaks.array_peak_w >= ARRAY_ALIVE_MIN_W
            and best_sibling >= SIBLING_ALIVE_MIN_W
            and config_flaggable
            and observed < SIBLING_DEAD_FRACTION * best_sibling
            and observed < CONFIG_DEAD_FRACTION * configured_wp
        )
        out.append(
            {
                "stringId": string_id,
                "index": i,
                "status": "dead" if dead else "ok",
                "observedPeakW": observed,
                "bestSiblingPeakW": best_sibling,
                "configuredWp": configured_wp,
            }
        )
    return out


def sweep_inverter(
    prev: Any, strings: Any, readings: list[dict], now_ms: int
) -> tuple[dict[str, Any], SweepResult]:
    """One sweep for one inverter: the next state and what to tell.

    `prev` is `{"dead": [...], "lastAlertMs": ...}` from the last sweep. Too few
    strings or readings leave it as it was. An alert goes out only for a
    string that was not dead last time, and not within 7 days of the last one.
    """
    state = dict(prev) if isinstance(prev, dict) else {}
    dead_before = state.get("dead") if isinstance(state.get("dead"), list) else []
    last_ms = state.get("lastAlertMs")
    last_ms = last_ms if type(last_ms) is int else None
    if not isinstance(strings, list) or len(strings) < 2:
        return state, SweepResult(checked=False)
    peaks = reduce_string_peaks(readings, len(strings))
    if peaks.sample_count < MIN_SWEEP_SAMPLES:
        return state, SweepResult(checked=False)
    health = detect_dead_strings(strings, peaks)
    dead = [h["stringId"] for h in health if h["status"] == "dead"]
    newly = [s for s in dead if s not in dead_before]
    state["dead"] = dead
    alert = None
    if newly and (last_ms is None or now_ms - last_ms >= COOLDOWN_MS):
        alert = ", ".join(newly)
        state["lastAlertMs"] = now_ms
    return state, SweepResult(checked=True, health=health, alert_string_id=alert)
