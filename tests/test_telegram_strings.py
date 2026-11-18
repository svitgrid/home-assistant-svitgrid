"""The cloud's string-outage rule, run by the island (svitgrid D-2155).

The cloud's weekly sweep skips a local-only island. The add-on runs the same
rule over its own readings: `reduceStringPeaks` and `detectDeadStrings` from
`functions/src/string-health-rule.ts`, with the sweep's gates on top. The
shared cases are exported from the cloud by running that code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.svitgrid.telegram import strings as rule

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "island_string_health_cases.json").read_text()
)
T0 = 1_793_181_600_000
DAY_MS = 24 * 60 * 60 * 1000


def _readings(samples):
    return [dict(s["reading"]) for s in samples for _ in range(s["times"])]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
@pytest.mark.parametrize("round_trip", [False, True], ids=["live", "json"])
def test_shared_cases(case, round_trip):
    state: dict | None = None
    for run in case["runs"]:
        state, result = rule.sweep_inverter(
            state, case["strings"], _readings(run["samples"]), T0 + run["atDay"] * DAY_MS
        )
        if round_trip:
            state = json.loads(json.dumps(state))
        expect = run["expect"]
        assert result.checked is expect["checked"], run
        assert list(state.get("dead", [])) == expect["deadStringIds"], run
        assert result.health == expect["health"], run
        got = None if result.alert_string_id is None else {"stringId": result.alert_string_id}
        assert got == expect["alert"], run


def test_an_absent_channel_is_not_counted_as_reported():
    peaks = rule.reduce_string_peaks([{"pvPower": 100, "pvPower1": 100}], 2)
    assert peaks.reported_channels == 1
    assert peaks.peak_w == [100, 0]
    assert rule.reduce_string_peaks([], 2).reported_channels is None


def test_malformed_strings_and_readings_check_nothing():
    """A layout from an older or broken app never raises."""
    for strings in (None, "PV1", [None, 3], [{"id": 5, "peakPowerWp": "3000"}]):
        state, result = rule.sweep_inverter(None, strings, [{"pvPower": "x"}] * 60, T0)
        assert result.alert_string_id is None
    state, result = rule.sweep_inverter(
        {"dead": "PV2", "lastAlertMs": "soon"},
        [{"id": "PV1", "peakPowerWp": 3000}, {"id": "PV2", "peakPowerWp": 3000}],
        [{"pvPower": 2500, "pvPower1": 2500, "pvPower2": 0}] * 60,
        T0,
    )
    assert result.alert_string_id == "PV2"
