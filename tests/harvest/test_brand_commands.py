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

# The fixture's commands the island emits: the priority-profile commands
# (svitgrid#1254) and the PI30 and MUST threshold commands (#1255).
_EMITTED = frozenset(
    {
        "apply_priority_profile",
        "restore_priority_profile",
        "set_pi30_thresholds",
        "restore_pi30_thresholds",
        "set_must_thresholds",
        "restore_must_thresholds",
    }
)

# The box's own settings as it posts them to sync-settings (edge_api), with the
# owner's switch-over voltages: QPIRI field 9 (48.0) and 23 (51.0).
_PI30_SETTINGS = {
    "inverter_id": "inv-1",
    "model_id": "edecoa_em_624a",
    "kind": "pi30",
    "body": {
        "protocol": "pi30",
        "qpiri": "230.0 30.4 230.0 50.0 30.4 7000 6200 48.0 48.0 46.0 53.5 52.5 2 30 050 0 2 2 "
        "6 01 0 0 51.0 0 1",
    },
}
_PI30_PRIOR = {"backToGridVoltage": 48.0, "backToBatteryVoltage": 51.0}


def _must_settings(stop_discharge_word: int, stop_charge_word: int) -> dict:
    """A MUST settings body whose 20118 and 20119 hold the given 0.1 V words."""
    high = [0] * 43
    high[20118 - 20101] = stop_discharge_word
    high[20119 - 20101] = stop_charge_word
    return {
        "inverter_id": "inv-1",
        "model_id": "must_pv18_3224",
        "kind": "must",
        "body": {
            "protocol": "must",
            "blocks": [
                {"startRegister": 10101, "registers": [0] * 12},
                {"startRegister": 20101, "registers": high},
            ],
        },
    }


_MUST_SETTINGS = _must_settings(470, 555)
_MUST_PRIOR = {"stopDischargeV": 47.0, "stopChargeV": 55.5}
_MUST = {"modelId": "must_pv18_3224", "transport": "modbus_rtu", "protocolId": "must"}

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
    settings = {"pi30": _PI30_SETTINGS, "must": _MUST_SETTINGS}.get(entry["inverter"]["protocolId"])
    decision = evaluate_event(
        event,
        {k: v for k, v in entry["reading"].items() if k != "inverterId"},
        event["executionState"],
        _parse_now(entry["now"]),
        entry["timezone"],
        inverter=entry["inverter"],
        settings=settings,
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


@pytest.mark.parametrize("mode", ["sell_to_grid", "gen_force"])
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
    ("action", "inverter"),
    [
        ({"type": "must_thresholds", "stopDischargeV": 48, "stopChargeV": 54}, _PI30),
        ({"type": "pi30_thresholds", "backToGridVoltage": 46, "backToBatteryVoltage": 54}, _MUST),
    ],
    ids=["must-on-pi30", "pi30-on-must"],
)
def test_threshold_action_of_another_protocol_is_unsupported(action, inverter):
    decision = evaluate_event(
        _custom_event(action),
        _READING,
        {"status": "pending_condition"},
        _NOW,
        TZ,
        inverter=inverter,
        settings=_PI30_SETTINGS if inverter is _PI30 else _MUST_SETTINGS,
    )
    assert decision.commands == []
    assert decision.skip_reason == "unsupported_field"


_PI30_ACTION = {"type": "pi30_thresholds", "backToGridVoltage": 46, "backToBatteryVoltage": 54}
_MUST_ACTION = {"type": "must_thresholds", "stopDischargeV": 48, "stopChargeV": 54}

_THRESHOLD_CASES = [
    (
        _PI30_ACTION,
        _PI30,
        _PI30_SETTINGS,
        "previousPi30Thresholds",
        _PI30_PRIOR,
        "no_prior_pi30_thresholds",
    ),
    (
        _MUST_ACTION,
        _MUST,
        _MUST_SETTINGS,
        "previousMustThresholds",
        _MUST_PRIOR,
        "no_prior_must_thresholds",
    ),
]


@pytest.mark.parametrize(
    ("action", "inverter", "settings", "key", "prior", "reason"),
    _THRESHOLD_CASES,
    ids=["pi30", "must"],
)
def test_threshold_activation_keeps_the_owners_prior_voltages(
    action, inverter, settings, key, prior, reason
):
    """The owner's voltages come from the box's own settings and are kept in
    customSnapshot, which the restore sends back."""
    event = _custom_event(action)
    on = evaluate_event(
        event,
        _READING,
        {"status": "pending_condition"},
        _NOW,
        TZ,
        inverter=inverter,
        settings=settings,
    )
    assert on.action == "activate"
    assert on.new_state["status"] == "active"
    assert on.new_state["customSnapshot"] == {key: prior}

    # The box has since synced the rule's own voltages: the restore keeps the
    # owner's, held since the activation.
    later = datetime(2026, 6, 29, 12, 5, 0, tzinfo=UTC)
    released = {**_READING, "batterySoc": 95}
    off = evaluate_event(event, released, on.new_state, later, TZ, inverter=inverter, settings=None)
    assert off.action == "deactivate"
    restore = {"pi30_thresholds": ("restore_pi30_thresholds", prior)}.get(
        action["type"], ("restore_must_thresholds", {"prev": prior})
    )
    assert off.commands == [restore]
    assert "customSnapshot" not in off.new_state
    assert off.new_state["status"] == "idle"


@pytest.mark.parametrize(
    ("action", "inverter", "settings", "key", "prior", "reason"),
    _THRESHOLD_CASES,
    ids=["pi30", "must"],
)
@pytest.mark.parametrize("missing", ["no_snapshot", "no_voltages"])
def test_no_prior_thresholds_skips_activation_once(
    action, inverter, settings, key, prior, reason, missing
):
    """Without the owner's voltages nothing is set that could not be put back:
    the activation is skipped with the cloud's reason, recorded once."""
    if missing == "no_snapshot":
        settings = None
    elif action["type"] == "pi30_thresholds":
        settings = {**settings, "body": {"protocol": "pi30", "qpiri": "230.0 30.4"}}
    else:
        settings = _must_settings(0, 555)
    event = _custom_event(action)
    state = {"status": "pending_condition"}
    first = evaluate_event(event, _READING, state, _NOW, TZ, inverter=inverter, settings=settings)
    assert first.action == "skip"
    assert first.commands == []
    assert first.skip_reason == reason
    assert first.new_state["skipReason"] == reason
    assert first.new_state["status"] == "pending_condition"
    assert "customSnapshot" not in first.new_state

    later = datetime(2026, 6, 29, 12, 5, 0, tzinfo=UTC)
    second = evaluate_event(
        event, _READING, first.new_state, later, TZ, inverter=inverter, settings=settings
    )
    assert second.commands == []
    assert second.skip_reason == reason
    # Recorded once: the next tick changes nothing.
    assert second.new_state == first.new_state


def test_threshold_activation_clears_an_earlier_no_prior_skip():
    event = _custom_event(_PI30_ACTION)
    state = {"status": "pending_condition", "skipReason": "no_prior_pi30_thresholds"}
    decision = evaluate_event(
        event, _READING, state, _NOW, TZ, inverter=_PI30, settings=_PI30_SETTINGS
    )
    assert decision.action == "activate"
    assert "skipReason" not in decision.new_state


@pytest.mark.parametrize(
    ("action", "inverter", "settings", "key", "prior", "reason"),
    _THRESHOLD_CASES,
    ids=["pi30", "must"],
)
def test_threshold_window_end_restores_the_owners_voltages(
    action, inverter, settings, key, prior, reason
):
    event = {**_custom_event(action), "schedule": {**_ALL_DAY, "endTime": "15:30"}}
    on = evaluate_event(
        event,
        _READING,
        {"status": "pending_condition"},
        _NOW,
        TZ,
        inverter=inverter,
        settings=settings,
    )
    assert on.action == "activate"
    after = datetime(2026, 6, 29, 12, 45, 0, tzinfo=UTC)  # 15:45 Kyiv
    off = evaluate_event(event, _READING, on.new_state, after, TZ, inverter=inverter)
    assert off.action == "deactivate"
    assert len(off.commands) == 1
    assert off.commands[0][0].startswith("restore_")
    assert "customSnapshot" not in off.new_state


def test_threshold_restore_without_held_voltages_sends_nothing():
    """A damaged snapshot: no voltages are made up, and the app is told."""
    event = _custom_event(_MUST_ACTION)
    state = {"status": "active", "customSnapshot": {}}
    released = {**_READING, "batterySoc": 95}
    off = evaluate_event(event, released, state, _NOW, TZ, inverter=_MUST)
    assert off.action == "deactivate"
    assert off.commands == []
    assert off.new_state["lastCommandError"]["command"] == "restore_must_thresholds"


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


def test_a_cancelled_threshold_run_restores_the_owners_voltages():
    """The app's delete or disable puts back the same values as the window end."""
    from custom_components.svitgrid.harvest.event_evaluator import restore_decision

    event = _custom_event(_PI30_ACTION)
    state = {"status": "active", "customSnapshot": {"previousPi30Thresholds": _PI30_PRIOR}}
    commands, done = restore_decision(event, state, _NOW)
    assert commands == [("restore_pi30_thresholds", _PI30_PRIOR)]
    assert "customSnapshot" not in done
    assert done["status"] == "idle"
