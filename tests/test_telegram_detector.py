"""Island Telegram outage detector: the shared cases and the derive path."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.svitgrid.telegram.detector import evaluate

CASES = json.loads((Path(__file__).parent / "fixtures" / "island_alert_cases.json").read_text())
MIN_MS = 60_000


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _run(steps):
    state: dict = {}
    got = []
    for index, step in enumerate(steps):
        for inv_id, reading in step["inverters"].items():
            state, alerts = evaluate(state, "hh1", inv_id, reading, step["atMs"])
            got += [(index, a) for a in alerts]
    return state, got


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_cases(case):
    _, got = _run(case["steps"])
    assert [(i, a.type, a.event_at_ms, a.battery_soc, a.household_id) for i, a in got] == [
        (e["atStep"], e["type"], _ms(e["eventAt"]), e["batterySoc"], "hh1") for e in case["expect"]
    ]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_state_round_trips_through_json(case):
    state = {}
    got = []
    for index, step in enumerate(case["steps"]):
        for inv_id, reading in step["inverters"].items():
            state, alerts = evaluate(state, "hh1", inv_id, reading, step["atMs"])
            state = json.loads(json.dumps(state))
            got += [(index, a.type) for a in alerts]
    assert got == [(e["atStep"], e["type"]) for e in case["expect"]]


def test_missing_grid_present_is_derived_and_first_down_is_held():
    t0 = 1_793_181_600_000
    dead = {"gridVoltage": 0, "gridFrequency": 0, "gridPower": 0, "batterySoc": 70}
    live = {"gridVoltage": 230, "gridFrequency": 50, "gridPower": 500, "batterySoc": 70}
    state, alerts = evaluate({}, "hh1", "inv1", live, t0)
    assert alerts == []
    # The first down reading is held by the debounce: no clock starts.
    state, alerts = evaluate(state, "hh1", "inv1", dead, t0 + MIN_MS)
    assert alerts == []
    # The second confirms the grid down and starts the 5 minute clock.
    state, alerts = evaluate(state, "hh1", "inv1", dead, t0 + 2 * MIN_MS)
    assert alerts == []
    state, alerts = evaluate(state, "hh1", "inv1", dead, t0 + 6 * MIN_MS)
    assert alerts == []
    state, alerts = evaluate(state, "hh1", "inv1", dead, t0 + 7 * MIN_MS)
    assert [(a.type, a.event_at_ms, a.battery_soc) for a in alerts] == [
        ("outage", t0 + 2 * MIN_MS, 70)
    ]
    state, alerts = evaluate(state, "hh1", "inv1", live, t0 + 9 * MIN_MS)
    assert [(a.type, a.event_at_ms) for a in alerts] == [("grid_restored", t0 + 9 * MIN_MS)]


def test_restore_uses_reading_timestamp_when_present():
    t0 = 1_793_181_600_000
    edge = {
        "gridPresent": False,
        "gridConfirmedSource": "edge",
        "gridConfirmedAt": datetime.fromtimestamp(t0 / 1000, UTC).isoformat(),
        "batterySoc": 50,
    }
    state, alerts = evaluate({}, "hh1", "inv1", edge, t0)
    assert [a.type for a in alerts] == ["outage"]
    up = {"gridPresent": True, "batterySoc": 51, "timestamp": "2026-10-28T10:20:00.000Z"}
    _, alerts = evaluate(state, "hh1", "inv1", up, t0 + 25 * MIN_MS)
    assert alerts[0].event_at_ms == _ms("2026-10-28T10:20:00.000Z")
