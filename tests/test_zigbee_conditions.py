"""Zigbee sensor and socket conditions on an edge island (svitgrid D-2161).

The cloud reads a rule's Zigbee devices from `smartDevices/{id}`
(`zigbee-condition-signals.ts`) and judges them in `custom-event-decision.ts`.
On an edge island the box reports its devices to Home Assistant instead
(`zigbee_devices.py`), so the island builds the same document from its store
and runs the same two steps. The shared cases in
tests/fixtures/zigbee_condition_cases.json are the cloud's answers. Unknown
is "not met", even while the rule is active, so a silent sensor never keeps a
heater on.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from custom_components.svitgrid.harvest.event_evaluator import (
    ZIGBEE_SIGNALS_KEY,
    _evaluate_conditions,
    _one_holds,
    evaluate_event,
)
from custom_components.svitgrid.harvest.event_scheduler_loop import _tick
from custom_components.svitgrid.zigbee_devices import (
    ZigbeeDeviceStore,
    zigbee_signal_from_doc,
)
from tests.test_event_scheduler_loop import FakeEventStore, FakeStore

_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "zigbee_condition_cases.json").read_text()
)

TZ = "Europe/Kyiv"
_NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
NOW_MS = int(_NOW.timestamp() * 1000)


# ── the cloud's cases ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", _CASES["cases"], ids=[c["name"] for c in _CASES["cases"]])
def test_the_island_decides_each_case_as_the_cloud(case):
    signal = zigbee_signal_from_doc(case["doc"], _CASES["householdId"], _CASES["nowMs"])
    assert signal == case["signal"]
    signals = {ZIGBEE_SIGNALS_KEY: {case["condition"]["deviceId"]: signal}}
    assert _one_holds(case["condition"], signals, case["held"]) is case["holds"]


def test_one_household_skips_the_household_check():
    """Home Assistant is one household: it passes no id, and a document
    without one is still read."""
    doc = {k: v for k, v in _CASES["cases"][0]["doc"].items() if k != "householdId"}
    assert zigbee_signal_from_doc(doc, None, _CASES["nowMs"]) == _CASES["cases"][0]["signal"]


def test_a_device_the_signals_do_not_carry_is_not_met():
    condition = {"type": "zigbee_socket_on", "deviceId": "zb-1", "on": True}
    assert _evaluate_conditions([condition], {}, True) is False
    assert _evaluate_conditions([condition], {ZIGBEE_SIGNALS_KEY: {}}, True) is False


# ── the document the store builds ─────────────────────────────────────────────


async def _store(tmp_path) -> ZigbeeDeviceStore:
    store = ZigbeeDeviceStore(str(tmp_path / "zigbee_devices.db"))
    sensor = await store.async_add("EC-1", "A4:C1:38:00:00:00:00:01", "0x1001", 1)
    plug = await store.async_add("EC-1", "A4:C1:38:00:00:00:00:02", "0x1002", 1)
    await store.async_update(sensor["id"], {"name": "Спальня"})
    await store.async_update(plug["id"], {"name": "Бойлер", "on": True})
    return store


async def test_a_sensor_document_carries_the_latest_value_of_each_metric(tmp_path):
    store = await _store(tmp_path)
    await store.async_store_sensor_readings(
        "EC-1",
        [{"addr": "0x1001", "ageMs": 0, "temperature": 1900, "humidity": 5000}],
        received_at=_NOW - timedelta(minutes=20),
    )
    # The newest upload carries no humidity: the earlier one still stands,
    # as the cloud merges `lastReading` per metric.
    await store.async_store_sensor_readings(
        "EC-1",
        [{"addr": "0x1001", "ageMs": 60_000, "temperature": 1943, "battery": 180}],
        received_at=_NOW,
    )

    docs = await store.async_condition_docs(["zb-a4c1380000000001", "zb-missing"])

    doc = docs["zb-a4c1380000000001"]
    assert doc["type"] == "sensor"
    assert doc["name"] == "Спальня"
    assert doc["lastReading"] == {"temperature": 1943, "humidity": 5000, "battery": 180}
    assert doc["lastReadingAt"] == "2026-11-18T11:59:00.000Z"
    assert docs["zb-missing"] is None
    assert zigbee_signal_from_doc(doc, None, NOW_MS) == {
        "name": "Спальня",
        "temperatureC": 19.43,
        "humidityPct": 50.0,
        "powerW": None,
        "on": None,
    }


async def test_a_socket_document_carries_its_state_and_latest_power(tmp_path):
    store = await _store(tmp_path)
    at = (_NOW - timedelta(minutes=2)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    await store.async_store_socket_readings(
        "EC-1",
        at,
        [{"addr": "0x1002", "power": 1840, "voltage": 231, "current": 8, "energy": 10}],
        profiles={},
    )

    doc = (await store.async_condition_docs(["zb-a4c1380000000002"]))["zb-a4c1380000000002"]

    assert doc["type"] == "switch"
    assert doc["reachable"] is True
    assert doc["state"] == {"on": True, "power": 1840, "powerAt": at}
    assert zigbee_signal_from_doc(doc, None, NOW_MS) == {
        "name": "Бойлер",
        "temperatureC": None,
        "humidityPct": None,
        "powerW": 1840,
        "on": True,
    }


async def test_an_unreachable_socket_reads_as_unknown(tmp_path):
    store = await _store(tmp_path)
    await store.async_update("zb-a4c1380000000002", {"reachable": False})

    doc = (await store.async_condition_docs(["zb-a4c1380000000002"]))["zb-a4c1380000000002"]

    assert zigbee_signal_from_doc(doc, None, NOW_MS)["on"] is None


# ── the rule, through the evaluator and the scheduler ─────────────────────────

_COLD = {
    "type": "zigbee_temperature",
    "deviceId": "zb-a4c1380000000001",
    "op": "lte",
    "thresholdC": 20,
    "releaseThresholdC": 21,
}


def _rule(conditions) -> dict:
    return {
        "id": "rule-1",
        "inverterId": "inv-1",
        "mode": "custom",
        "enabled": True,
        "schedule": {"recurrence": "always"},
        "config": {
            "customConditions": conditions,
            "customActions": [{"type": "grid_charge_toggle", "enabled": True}],
            "sustainMinutes": 0,
        },
    }


def _reading(**fields) -> dict:
    at = (_NOW - timedelta(seconds=10)).isoformat()
    return {"timestamp": at, "batterySoc": 50, "pvPower": 0, "loadPower": 500, **fields}


def _signal(temperature_c) -> dict:
    return {
        "name": "Спальня",
        "temperatureC": temperature_c,
        "humidityPct": None,
        "powerW": None,
        "on": None,
    }


def test_a_cold_room_arms_the_rule():
    reading = _reading(**{ZIGBEE_SIGNALS_KEY: {_COLD["deviceId"]: _signal(19)}})
    decision = evaluate_event(_rule([_COLD]), reading, {"status": "idle"}, _NOW, TZ)
    assert decision.new_state["status"] == "pending_condition"


def test_a_zigbee_rule_is_not_skipped_as_cloud_only():
    reading = _reading(**{ZIGBEE_SIGNALS_KEY: {_COLD["deviceId"]: _signal(19)}})
    decision = evaluate_event(_rule([_COLD]), reading, {"status": "idle"}, _NOW, TZ)
    assert decision.action != "skip"


def test_a_silent_sensor_releases_an_active_rule():
    active = {"status": "active", "previousTimerEnabled": False}
    reading = _reading(**{ZIGBEE_SIGNALS_KEY: {_COLD["deviceId"]: _signal(None)}})
    decision = evaluate_event(_rule([_COLD]), reading, active, _NOW, TZ)
    assert decision.action == "deactivate"


def _snapshot() -> dict:
    return {"inverterId": "inv-1", "ts": _NOW.isoformat(), "payload": _reading(), "intervalS": 10}


async def test_the_scheduler_arms_a_zigbee_rule_from_the_box_devices():
    events = FakeEventStore([{**_rule([_COLD]), "executionState": {"status": "idle"}}])
    asked = []

    async def zigbee_docs(device_ids):
        asked.append(list(device_ids))
        return {
            "zb-a4c1380000000001": {
                "type": "sensor",
                "name": "Спальня",
                "lastReading": {"temperature": 1850},
                "lastReadingAt": (_NOW - timedelta(minutes=5)).isoformat(),
            }
        }

    await _tick(
        FakeStore([_snapshot()]),
        events,
        lambda _id: None,
        TZ,
        _NOW,
        zigbee_docs_for=zigbee_docs,
    )

    assert asked == [["zb-a4c1380000000001"]]
    assert events.state_calls[-1][1]["status"] == "pending_condition"


async def test_the_scheduler_reads_no_device_without_a_zigbee_rule():
    rule = _rule([{"type": "battery_soc", "op": "lte", "threshold": 60}])
    events = FakeEventStore([{**rule, "executionState": {"status": "idle"}}])
    asked = []

    async def zigbee_docs(device_ids):
        asked.append(device_ids)
        return {}

    await _tick(
        FakeStore([_snapshot()]), events, lambda _id: None, TZ, _NOW, zigbee_docs_for=zigbee_docs
    )
    assert asked == []


async def test_a_failed_device_read_leaves_the_devices_unknown():
    active = {"status": "active", "previousTimerEnabled": False}
    events = FakeEventStore([{**_rule([_COLD]), "executionState": active}])

    async def zigbee_docs(_device_ids):
        raise RuntimeError("db locked")

    class _Dispatcher:
        async def dispatch(self, *_args, **_kwargs):
            return None

    await _tick(
        FakeStore([_snapshot()]),
        events,
        lambda _id: _Dispatcher(),
        TZ,
        _NOW,
        zigbee_docs_for=zigbee_docs,
    )
    assert events.state_calls[-1][1]["status"] == "idle"
