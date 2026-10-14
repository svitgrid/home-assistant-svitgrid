"""The island scheduler's payloads must compute against the bundled write recipes.

The evaluator and the register specs are tested apart, so nothing caught the
seam between them: an island battery-charge event built a payload without
`powerLimit`, the SG04LP3 recipe looked the key up, and `write_compute` raised
`KeyError: 'powerLimit'` on every activation. These tests run each payload the
island evaluator can emit through the write recipes in the golden-vector
fixture (the SG03LP1 and SG04LP3 specs' recipes, exported from the monorepo).

The register writes expected below are the ones the cloud path produces for the
same event: the API's `buildBatteryChargePayload` single-slot shape, executed
by the firmware's `execute_battery_charge` or the mobile harvester's
`BatteryChargeExecutor`. Both skip the power-limit write when the payload has
no `powerLimit`, and skip the voltage write when `chargeVoltage` is absent or 0.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from custom_components.svitgrid.harvest.event_evaluator import (
    _restore_commands,
    evaluate_event,
)
from custom_components.svitgrid.harvest.register_spec import WriteCommand
from custom_components.svitgrid.harvest.write_compute import compute_register_writes
from custom_components.svitgrid.harvest.write_executor import _RECIPE_ALIASES

_VECTORS = Path(__file__).parent.parent / "fixtures" / "write-golden-vectors.json"

_NOW = datetime(2026, 10, 14, 10, 30, tzinfo=UTC)
_SCHEDULE = {"startTime": "10:00", "endTime": "11:00", "recurrence": "daily"}
_READING = {"batterySoc": 40, "pvPower": 0, "gridPower": 300, "loadPower": 300}
# A captured slot table (the set_battery_charge ACK's prevSlots).
_TABLE = [
    {
        "start": h * 400,
        "end": (h + 1) * 400 % 2400,
        "power": 5000,
        "voltage": 5300,
        "soc": 20,
        "enable": 0,
    }
    for h in range(6)
]


def _recipes() -> dict[tuple[str, str], WriteCommand]:
    """(modelId, command) -> the spec's write recipe, from the golden vectors."""
    vectors = json.loads(_VECTORS.read_text(encoding="utf-8"))["vectors"]
    return {
        (v["modelId"], v["command"]): WriteCommand.from_dict(v["writeCommand"]) for v in vectors
    }


def _sg04lp3(command: str) -> WriteCommand:
    return _recipes()[("deye_sg04lp3", command)]


def _quick_override_event() -> dict:
    # What quick_battery_override_provider.dart creates for "charge": grid
    # source, 100 %, and no chargePowerLimitW.
    return {
        "id": "33f55ef3",
        "inverterId": "inv-1",
        "enabled": True,
        "mode": "battery_charge",
        "source": "quick_override",
        "schedule": dict(_SCHEDULE),
        "config": {"chargeSource": "grid", "targetSoc": 100},
    }


def _island_commands() -> list[tuple[str, str, dict]]:
    """Every (label, command, payload) the island evaluator emits, activate + restore."""
    out: list[tuple[str, str, dict]] = []
    events = [
        ("battery_charge/quick_override", _quick_override_event()),
        (
            "battery_charge/solar_surplus_grid",
            {
                "mode": "battery_charge",
                "schedule": dict(_SCHEDULE),
                "config": {"chargeSource": "solar_surplus_grid", "targetSoc": 90},
            },
        ),
        (
            "battery_charge/power_limit",
            {
                "mode": "battery_charge",
                "schedule": dict(_SCHEDULE),
                "config": {"chargeSource": "grid", "chargePowerLimitW": 3000},
            },
        ),
        ("use_battery", {"mode": "use_battery", "schedule": dict(_SCHEDULE), "config": {}}),
        (
            "sell_to_grid",
            {"mode": "sell_to_grid", "schedule": dict(_SCHEDULE), "config": {}},
        ),
        (
            "battery_maintenance",
            {
                "mode": "battery_maintenance",
                "schedule": {"startTime": "00:00", "endTime": "23:59", "recurrence": "daily"},
                "config": {"gridFallbackHour": 0},
            },
        ),
        ("gen_force", {"mode": "gen_force", "schedule": dict(_SCHEDULE), "config": {}}),
    ]
    for label, event in events:
        decision = evaluate_event(event, _READING, {}, _NOW, "UTC")
        for name, payload in decision.commands:
            out.append((f"{label}/activate", name, payload))
        active_state = dict(
            decision.new_state, status="active", gridFallbackFired=True, prevSlots=_TABLE
        )
        for name, payload in _restore_commands(event, active_state):
            out.append((f"{label}/restore", name, payload))
    return out


_COMMANDS = _island_commands()


def test_the_island_emits_commands_to_check():
    names = {name for _, name, _ in _COMMANDS}
    assert {"set_battery_charge", "set_work_mode", "set_grid_charge_toggle"} <= names


@pytest.mark.parametrize(
    ("label", "name", "payload"),
    _COMMANDS,
    ids=[f"{label}:{name}" for label, name, _ in _COMMANDS],
)
def test_every_island_payload_computes_on_every_recipe(label, name, payload):
    checked = 0
    recipe = _RECIPE_ALIASES.get(name, name)
    for (model_id, command), cmd in _recipes().items():
        if command != recipe:
            continue
        writes = compute_register_writes(cmd, payload, {})
        assert writes, f"{model_id}: {name} produced no writes"
        checked += 1
    assert checked, f"no recipe defines {name}"


def test_quick_override_charge_writes_what_the_cloud_path_writes():
    decision = evaluate_event(_quick_override_event(), _READING, {}, _NOW, "UTC")
    assert decision.action == "activate"
    [(name, payload)] = decision.commands
    assert name == "set_battery_charge"

    writes = compute_register_writes(_sg04lp3(name), payload, {})

    # slot 1 start 10:00, slot 2 start (the end) 11:00, SOC 100, grid bit on.
    # No 154 (power limit) and no 160 (charge voltage): the event sets
    # neither, so the cloud path leaves both registers as the user set them.
    assert writes == [(1, 148, 1000), (1, 149, 1100), (1, 166, 100), (1, 172, 1)]


def test_an_event_power_limit_is_written_to_the_slot():
    event = _quick_override_event()
    event["config"]["chargePowerLimitW"] = 3000
    [(name, payload)] = evaluate_event(event, _READING, {}, _NOW, "UTC").commands

    writes = compute_register_writes(_sg04lp3(name), payload, {})

    assert (1, 154, 3000) in writes
    assert all(addr != 160 for _, addr, _ in writes)


def test_use_battery_clears_the_grid_charge_bit_and_keeps_the_rest():
    event = {"mode": "use_battery", "schedule": dict(_SCHEDULE), "config": {}}
    cmds = dict(evaluate_event(event, _READING, {}, _NOW, "UTC").commands)

    writes = compute_register_writes(
        _sg04lp3("set_grid_charge_toggle"),
        cmds["set_grid_charge_toggle"],
        {146: 0b1111_1111},
    )

    assert writes == [(1, 146, 0b1111_1110)]
