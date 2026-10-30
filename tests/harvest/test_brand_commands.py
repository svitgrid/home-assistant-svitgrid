"""Per-brand command emission on an edge island (svitgrid#1233, Task 13a).

The fixtures are vendored from the monorepo by
scripts/sync-edge-command-fixtures.sh.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.svitgrid.harvest.brand_commands import (
    brand_kind,
    edge_inverter,
    priority_profile_protocol,
    profile_for_mode,
)
from custom_components.svitgrid.harvest.event_evaluator import evaluate_event
from custom_components.svitgrid.keystore import EdgeBoxEntry

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_RESOLUTION = json.loads((_FIXTURES / "priority_profile_resolution.json").read_text())
_PAYLOADS = json.loads((_FIXTURES / "edge_command_payloads.json").read_text())

# The fixture's commands this task emits. The priority-profile commands are
# svitgrid#1254 and the threshold commands #1255; each adds its own here.
_EMITTED = frozenset()

TZ = "Europe/Kyiv"
_NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)  # 15:00 Kyiv
_ALL_DAY = {"recurrence": "daily", "startTime": "00:00", "endTime": "23:59"}
_READING = {"batterySoc": 50, "pvPower": 0, "loadPower": 200, "gridPower": 200}

_DEYE = {"modelId": "deye_sg04lp3", "transport": None, "protocolId": None}
_PI30 = {"modelId": "edecoa_em_624a", "transport": "pi30_serial", "protocolId": "pi30"}
_SOLAX = {"modelId": "solax_x1_hybrid_g4", "transport": "modbus_tcp", "protocolId": "modbus_tcp"}


def _parse_now(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize(
    "case", _RESOLUTION, ids=lambda c: f"{c['modelId']}-{c['transport']}-{c['protocolId']}"
)
def test_priority_resolution_fixture(case):
    got = priority_profile_protocol(case["modelId"], case["transport"], case["protocolId"])
    assert got == case["expected"]


def test_profile_for_mode():
    assert profile_for_mode("battery_charge") == "grid_charge"
    assert profile_for_mode("battery_maintenance_charge") == "grid_charge"
    assert profile_for_mode("use_battery") == "battery_first"
    assert profile_for_mode("sell_to_grid") is None


def test_brand_kind():
    assert brand_kind(_DEYE) == "deye"
    assert brand_kind({"modelId": "solark_12k"}) == "deye"
    assert brand_kind(_PI30) == "priority"
    assert brand_kind({"modelId": "srne_hsi_1500"}) == "priority"
    assert brand_kind(_SOLAX) == "solax"
    assert brand_kind({"modelId": "victron_multiplus"}) == "none"
    assert brand_kind({}) == "none"


def test_solis_models_are_none_or_absent():
    assert brand_kind({"modelId": "solis_30k_5g", "transport": "modbus_rtu"}) == "none"
    for hybrid in ("solis_rhi_48es_5g", "solis_s6_eh1p"):
        assert brand_kind({"modelId": hybrid}) not in ("deye", "priority")


def test_edge_inverter_reads_the_adopt_body_keys():
    box = EdgeBoxEntry(
        device_id="box-1",
        api_key=None,
        inverter_ids=["inv-1", "inv-2"],
        inverters={"inv-1": {"inverterId": "inv-1", **_PI30, "slaveId": 1}},
        added_at="",
        pair_code_hash=None,
        pair_code_expires_at=None,
    )
    assert edge_inverter([box], "inv-1") == _PI30
    assert edge_inverter([box], "inv-2") == {
        "modelId": None,
        "transport": None,
        "protocolId": None,
    }
    assert edge_inverter([box], "inv-3") is None


@pytest.mark.parametrize(
    "entry", _PAYLOADS, ids=lambda e: f"{e['command']}-{e['inverter']['modelId']}"
)
def test_every_emitted_payload_matches_cloud_fixture(entry):
    """Each payload the island emits for a fixture event is the cloud's.

    A command this task does not emit yet sends nothing at all and records
    ``unsupported_field``: never a partial payload the box could act on.
    """
    event = entry["event"]
    decision = evaluate_event(
        event,
        {k: v for k, v in entry["reading"].items() if k != "inverterId"},
        event["executionState"],
        _parse_now(entry["now"]),
        entry["timezone"],
        inverter=entry["inverter"],
    )
    if entry["command"] in _EMITTED:
        assert (entry["command"], entry["payload"]) in [
            (name, {"inverterId": event["inverterId"], **payload})
            for name, payload in decision.commands
        ]
    else:
        assert decision.commands == []
        assert decision.action == "skip"
        assert decision.skip_reason == "unsupported_field"


def _custom_event(action: dict, state: dict | None = None) -> dict:
    return {
        "id": "evt-1",
        "inverterId": "inv-1",
        "enabled": True,
        "mode": "custom",
        "schedule": _ALL_DAY,
        "config": {
            "customActions": [action],
            "customConditions": [{"type": "battery_soc", "op": "lte", "threshold": 60}],
            "sustainMinutes": 0,
        },
        "executionState": state or {"status": "idle"},
    }


def test_solax_custom_action_is_unsupported_once():
    event = _custom_event({"type": "work_mode", "value": 2})
    first = evaluate_event(event, _READING, {"status": "idle"}, _NOW, TZ, inverter=_SOLAX)
    assert first.action == "skip"
    assert first.commands == []
    assert first.skip_reason == "unsupported_field"
    error = first.new_state["lastCommandError"]
    assert error["reason"] == "unsupported_field"
    assert error["at"] == _NOW.isoformat()

    later = datetime(2026, 6, 29, 12, 5, 0, tzinfo=UTC)
    second = evaluate_event(event, _READING, first.new_state, later, TZ, inverter=_SOLAX)
    assert second.commands == []
    assert second.skip_reason == "unsupported_field"
    # Recorded once: the next tick does not write a new error.
    assert second.new_state == first.new_state


@pytest.mark.parametrize("mode", ["battery_charge", "use_battery", "sell_to_grid", "gen_force"])
def test_priority_brand_mode_event_is_unsupported(mode):
    event = {
        "id": "evt-1",
        "inverterId": "inv-1",
        "enabled": True,
        "mode": mode,
        "schedule": _ALL_DAY,
        "config": {"targetSoc": 100, "chargeSource": "grid"},
        "executionState": {"status": "idle"},
    }
    decision = evaluate_event(event, _READING, {"status": "idle"}, _NOW, TZ, inverter=_PI30)
    assert decision.commands == []
    assert decision.skip_reason == "unsupported_field"
    assert decision.new_state["lastCommandError"]["reason"] == "unsupported_field"


@pytest.mark.parametrize(
    "action",
    [
        {"type": "pi30_thresholds", "backToGridVoltage": 46, "backToBatteryVoltage": 54},
        {"type": "priority_profile", "profile": "grid_charge"},
    ],
)
def test_priority_brand_custom_action_is_unsupported(action):
    decision = evaluate_event(
        _custom_event(action), _READING, {"status": "idle"}, _NOW, TZ, inverter=_PI30
    )
    assert decision.commands == []
    assert decision.skip_reason == "unsupported_field"


@pytest.mark.parametrize(
    "event",
    [
        _custom_event({"type": "work_mode", "value": 2}),
        {
            "id": "evt-2",
            "inverterId": "inv-1",
            "enabled": True,
            "mode": "battery_charge",
            "schedule": _ALL_DAY,
            "config": {"targetSoc": 90, "chargeSource": "grid"},
            "executionState": {"status": "idle"},
        },
    ],
    ids=["custom", "battery_charge"],
)
def test_deye_inverter_argument_changes_nothing(event):
    # A custom event activates once its condition has held: pending first.
    state = {"status": "pending_condition"} if event["mode"] == "custom" else {"status": "idle"}
    plain = evaluate_event(event, _READING, state, _NOW, TZ)
    edge = evaluate_event(event, _READING, state, _NOW, TZ, inverter=_DEYE)
    assert plain.action == "activate"
    assert plain.commands
    assert edge == plain
