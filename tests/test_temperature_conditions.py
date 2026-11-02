"""Inverter and battery temperature conditions on a true island (svitgrid#1328).

Ports of `custom-event-decision.ts` (`inverter_temperature`,
`bms_temperature`) and `bms-condition-signals.ts` (the household's battery
temperature). Unknown is "not met" for both, so an active rule releases,
unlike `grid_voltage`. The BMS expectations are the API's
(`bms-condition-signals.test.ts`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.svitgrid.bms_temperature import (
    BMS_TEMP_FRESH_MS,
    bms_temperature_from_states,
    has_bms_temperature,
)
from custom_components.svitgrid.harvest.event_evaluator import (
    BMS_TEMPERATURE_KEY,
    evaluate_event,
)
from custom_components.svitgrid.harvest.event_scheduler_loop import _tick
from tests.test_event_scheduler_loop import FakeEventStore, FakeStore

TZ = "Europe/Kyiv"
_NOW = datetime(2026, 11, 2, 10, 0, 0, tzinfo=UTC)
NOW_MS = int(_NOW.timestamp() * 1000)


def _ago(minutes: float) -> str:
    return (_NOW - timedelta(minutes=minutes)).isoformat()


def _rule(conditions, sustain=0) -> dict:
    return {
        "id": "rule-1",
        "inverterId": "inv-1",
        "mode": "custom",
        "enabled": True,
        "schedule": {"recurrence": "always"},
        "config": {
            "customConditions": conditions,
            "customActions": [{"type": "grid_charge_toggle", "enabled": True}],
            "sustainMinutes": sustain,
        },
    }


def _reading(**fields) -> dict:
    return {"timestamp": _ago(0.2), "batterySoc": 50, "pvPower": 0, "loadPower": 500, **fields}


_HOT = [{"type": "inverter_temperature", "op": "gte", "thresholdC": 60, "releaseThresholdC": 55}]
_COLD_PACK = [{"type": "bms_temperature", "op": "lte", "thresholdC": 5, "releaseThresholdC": 8}]
_IDLE = {"status": "idle"}
_ACTIVE = {"status": "active", "previousTimerEnabled": False}


# ── inverter_temperature ──────────────────────────────────────────────────────


def test_a_hot_inverter_arms_the_rule():
    decision = evaluate_event(_rule(_HOT), _reading(inverterTemperature=62), _IDLE, _NOW, TZ)
    assert decision.new_state["status"] == "pending_condition"


def test_a_cool_inverter_does_not_arm_it():
    decision = evaluate_event(_rule(_HOT), _reading(inverterTemperature=50), _IDLE, _NOW, TZ)
    assert decision.new_state["status"] == "idle"


def test_inverter_temperature_holds_between_the_thresholds_and_releases_below():
    held = evaluate_event(_rule(_HOT), _reading(inverterTemperature=57), _ACTIVE, _NOW, TZ)
    assert held.action == "hold"
    released = evaluate_event(_rule(_HOT), _reading(inverterTemperature=54), _ACTIVE, _NOW, TZ)
    assert released.action == "deactivate"


def test_an_unknown_inverter_temperature_releases_an_active_rule():
    released = evaluate_event(_rule(_HOT), _reading(), _ACTIVE, _NOW, TZ)
    assert released.action == "deactivate"


def test_an_implausible_inverter_temperature_is_unknown():
    # -100 is raw 0 on a biased register; 121 is outside -40..120.
    for value in (-100, 121, None):
        d = evaluate_event(_rule(_HOT), _reading(inverterTemperature=value), _ACTIVE, _NOW, TZ)
        assert d.action == "deactivate", value


def test_90_degrees_is_a_measurement():
    decision = evaluate_event(_rule(_HOT), _reading(inverterTemperature=90), _IDLE, _NOW, TZ)
    assert decision.new_state["status"] == "pending_condition"


def test_a_stale_reading_changes_nothing_for_inverter_temperature():
    stale = _reading(timestamp=_ago(20), inverterTemperature=40)
    decision = evaluate_event(_rule(_HOT), stale, _ACTIVE, _NOW, TZ)
    assert decision.action == "hold"


def test_an_inverter_temperature_rule_is_not_skipped_as_cloud_only():
    decision = evaluate_event(_rule(_HOT), _reading(inverterTemperature=62), _IDLE, _NOW, TZ)
    assert decision.action != "skip"
    assert "skippedReason" not in decision.new_state


# ── bms_temperature in the evaluator ──────────────────────────────────────────


def test_a_cold_pack_arms_the_rule():
    reading = _reading(**{BMS_TEMPERATURE_KEY: 3.0})
    decision = evaluate_event(_rule(_COLD_PACK), reading, _IDLE, _NOW, TZ)
    assert decision.new_state["status"] == "pending_condition"


def test_bms_temperature_holds_until_the_release_threshold():
    held = evaluate_event(
        _rule(_COLD_PACK), _reading(**{BMS_TEMPERATURE_KEY: 7}), _ACTIVE, _NOW, TZ
    )
    assert held.action == "hold"
    released = evaluate_event(
        _rule(_COLD_PACK), _reading(**{BMS_TEMPERATURE_KEY: 9}), _ACTIVE, _NOW, TZ
    )
    assert released.action == "deactivate"


def test_an_unknown_battery_temperature_releases_an_active_rule():
    released = evaluate_event(_rule(_COLD_PACK), _reading(), _ACTIVE, _NOW, TZ)
    assert released.action == "deactivate"


# ── the household's battery temperature (bms-condition-signals.test.ts) ──────

_JK32 = {
    "id": "aaaaaaaaaa01",
    "driver": "jk02_32s",
    "result": "ok",
    "everRead": True,
    "ageMs": 20_000,
}


def _box(packs, received_at_ms=NOW_MS) -> dict:
    return {"packs": packs, "receivedAtMs": received_at_ms, "updatedAt": _NOW.isoformat()}


def _mean(*boxes) -> float | None:
    return bms_temperature_from_states({f"box-{i}": b for i, b in enumerate(boxes)}, NOW_MS)


def test_phantom_zeros_of_a_jk02_32s_are_dropped():
    assert _mean(_box([{**_JK32, "tempsC": [14, 13, 0, 0, 18]}])) == 15


def test_zero_is_kept_when_the_whole_pack_agrees():
    assert _mean(_box([{**_JK32, "driver": "jk02_24s", "tempsC": [0, 0]}])) == 0


def test_a_zero_within_3_degrees_is_kept():
    assert _mean(_box([{**_JK32, "driver": "jbd", "tempsC": [0, 2]}])) == 1


def test_the_mosfet_and_balancer_slots_of_an_ant_pack_are_excluded():
    ant = {
        "id": "aaaaaaaaaa02",
        "driver": "ant_new",
        "result": "ok",
        "everRead": True,
        "ageMs": 0,
        "tempsC": [10, 12, 55, 60],
    }
    assert _mean(_box([ant])) == 11


def test_the_sentinel_and_out_of_window_values_are_dropped():
    assert _mean(_box([{**_JK32, "driver": "jbd", "tempsC": [-40, 20, 101, -31, 22]}])) == 21


def test_every_accepted_slot_is_averaged_across_packs_and_boxes():
    a = {**_JK32, "id": "aaaaaaaaaa03", "driver": "jbd", "tempsC": [10, 12]}
    b = {**_JK32, "id": "aaaaaaaaaa04", "driver": "jbd", "tempsC": [20]}
    assert _mean(_box([a]), _box([b])) == 14


def test_only_result_ok_counts():
    assert _mean(_box([{**_JK32, "result": "selfcheck_failed", "tempsC": [14, 13]}])) is None


def test_a_pack_with_no_slot_labels_contributes_nothing():
    assert _mean(_box([{**_JK32, "driver": "mystery", "tempsC": [14, 13]}])) is None


def test_a_pack_ages_from_received_at_minus_age():
    fresh = {**_JK32, "ageMs": BMS_TEMP_FRESH_MS - 1000, "tempsC": [14, 13]}
    assert _mean(_box([fresh])) == 13.5
    stale = {**_JK32, "ageMs": BMS_TEMP_FRESH_MS + 1000, "tempsC": [14, 13]}
    assert _mean(_box([stale])) is None
    old = _box([{**_JK32, "ageMs": 0, "tempsC": [14, 13]}], NOW_MS - 20 * 60_000)
    assert _mean(old) is None


def test_a_pack_reported_by_two_boxes_counts_once():
    pack = {**_JK32, "driver": "jbd", "tempsC": [20]}
    assert _mean(_box([pack]), _box([{**pack, "tempsC": [4]}])) == 20


def test_no_packs_is_unknown():
    assert bms_temperature_from_states({}, NOW_MS) is None
    assert bms_temperature_from_states(None, NOW_MS) is None


def test_has_bms_temperature_needs_a_labelled_cell_slot_fresh_or_not():
    old = {**_JK32, "ageMs": 10 * 60 * 60_000, "tempsC": [14, 13]}
    assert has_bms_temperature({"b": _box([old])}) is True
    assert has_bms_temperature({"b": _box([{**_JK32}])}) is False
    assert (
        has_bms_temperature({"b": _box([{**_JK32, "driver": "mystery", "tempsC": [14]}])}) is False
    )
    assert has_bms_temperature({}) is False


# ── the scheduler passes the battery temperature to the evaluator ─────────────


def _snapshot() -> dict:
    return {"inverterId": "inv-1", "ts": _ago(0.2), "payload": _reading(), "intervalS": 10}


async def test_the_scheduler_arms_a_bms_rule_from_the_box_reports():
    events = FakeEventStore([{**_rule(_COLD_PACK), "executionState": {"status": "idle"}}])
    pack = {**_JK32, "driver": "jbd", "tempsC": [3, 4]}

    async def bms_states():
        return {"box-1": _box([pack])}

    await _tick(
        FakeStore([_snapshot()]),
        events,
        lambda _id: None,
        TZ,
        _NOW,
        bms_states_for=bms_states,
    )
    assert events.state_calls[-1][1]["status"] == "pending_condition"


async def test_the_scheduler_reads_no_bms_state_without_a_bms_rule():
    events = FakeEventStore([{**_rule(_HOT), "executionState": {"status": "idle"}}])
    calls = []

    async def bms_states():
        calls.append(1)
        return {}

    await _tick(
        FakeStore([_snapshot()]), events, lambda _id: None, TZ, _NOW, bms_states_for=bms_states
    )
    assert calls == []


async def test_a_failed_bms_read_leaves_the_temperature_unknown():
    events = FakeEventStore([{**_rule(_COLD_PACK), "executionState": dict(_ACTIVE)}])

    async def bms_states():
        raise RuntimeError("db locked")

    await _tick(
        FakeStore([_snapshot()]),
        events,
        lambda _id: AsyncDispatcher(),
        TZ,
        _NOW,
        bms_states_for=bms_states,
    )
    assert events.state_calls[-1][1]["status"] == "idle"


class AsyncDispatcher:
    async def dispatch(self, *_args, **_kwargs):
        return None
