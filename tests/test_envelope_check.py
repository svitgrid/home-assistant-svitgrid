"""The add-on's envelope check runs the cloud's cases (svitgrid D-1939).

tests/fixtures/envelope_enforcement_cases.json is vendored from the monorepo
(scripts/sync-edge-command-fixtures.sh), where envelope-replay.test.ts holds
the cloud's envelopeFailures and envelopeVerdict to the same answers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.svitgrid.envelope_check import (
    envelope_check_report,
    envelope_failures,
    envelope_verdict,
    parse_iso8601_utc,
)

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "envelope_enforcement_cases.json").read_text()
)
_NOW = parse_iso8601_utc(_FIXTURE["now"])
_CASES = _FIXTURE["cases"]


def _failures(case):
    payload = case.get("payload") or {}
    target = payload.get("inverterId") if isinstance(payload.get("inverterId"), str) else ""
    return envelope_failures(
        case["signedEventData"], case["command"], target, _NOW, clock_set=case["clockSet"]
    )


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_the_check_runs_the_clouds_cases(case):
    failed = _failures(case)
    assert failed == case["failed"]
    for level in ("off", "switch", "always"):
        action, reason = envelope_verdict(failed, case["command"], case["signedEventData"], level)
        assert {"action": action, "reason": reason} == case["verdict"][level], level


def test_the_report_names_the_checks_and_the_expired_event():
    case = next(c for c in _CASES if c["name"] == "an expired event envelope")
    report = envelope_check_report(
        _failures(case), case["signedEventData"], case["command"], "admin-key", _NOW
    )
    assert report == {
        "failed": ["envelope_expired"],
        "command": "set_battery_charge",
        "shape": "scenario",
        "signingKeyId": "admin-key",
        "eventId": "sc-1",
        "expiredAgoS": 86400,
    }


def test_no_report_when_every_check_holds():
    case = _CASES[0]
    assert envelope_check_report([], case["signedEventData"], case["command"], "k", _NOW) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-10-21T23:00:00+02:00", parse_iso8601_utc("2026-10-21T21:00:00Z")),
        ("2026-10-21T21:00:00.123456789Z", parse_iso8601_utc("2026-10-21T21:00:00Z")),
        ("next year", None),
        ("2026-10-21 21:00:00Z", None),
        ("2026-13-01T00:00:00Z", None),
        (1793491200, None),
    ],
)
def test_valid_until_parses_what_the_board_parses(value, expected):
    assert parse_iso8601_utc(value) == expected


# ── svitgrid D-2288: an edge box authorizes an island event by its mode ──────
#
# On an edge island this scheduler relays the event's own signature to the
# box with every command it sends for the event. The event signs its mode,
# not a command list, so an always-enforcing box (svitgrid D-1899) authorizes
# what the mode sends: the deye lists of authorized-commands-by-mode.json (or
# the list of a mode in envelope-check-vectors.json islandEventModeAliases),
# and for a custom event its actions' lists. A command this scheduler sends
# that the list lacks would be refused by such a box. These tests hold the
# scheduler's writes, restores and brand commands to those lists.

from custom_components.svitgrid.harvest import event_evaluator as ev  # noqa: E402
from custom_components.svitgrid.harvest import priority_profile as pp  # noqa: E402
from custom_components.svitgrid.harvest.brand_commands import profile_for_mode  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures"
_BY_MODE = json.loads((_FIXTURES / "authorized-commands-by-mode.json").read_text())
_VECTORS = json.loads((_FIXTURES / "envelope-check-vectors.json").read_text())


def _box_list(mode: str) -> list[str]:
    mode = _VECTORS["islandEventModeAliases"].get(mode, mode)
    return _BY_MODE["deye"][mode]


# A prior value for every register an event can hold, so every restore runs.
_PRIORS = {
    "previousWorkMode": 3,
    "previousTimerEnabled": True,
    "maintenancePriorSolarSell": True,
    "previousSellPowerCapW": 4000,
    "previousGenForce": False,
    "previousGenPortMode": 1,
    "previousEnergyPattern": 0,
    "previousBatteryChargeCurrentA": 80,
    "prevSlots": [{"slot": 1, "gridChargeEnabled": False, "gridChargeSoc": 20}],
}


def _sent(event: dict) -> set[str]:
    """Every command the scheduler can send for *event*: its writes, the
    restore of every register it holds, and a priority brand's profile."""
    state = {"gridFallbackFired": True}
    writes = ev._event_writes(event, state)
    # The priors the activation read for the registers it wrote. A prior left
    # by a write the owner has since edited out of the event is restored with
    # that register's command, which the new mode may not authorize: the
    # cloud has the same edge (svitgrid D-2288).
    state.update({w.key: _PRIORS[w.key] for w in writes if w.key in _PRIORS})
    sent = {w.command for w in writes}
    sent |= {name for name, _ in ev._restore_plan(event, state)[0]}
    if profile_for_mode(event.get("mode", "")):
        sent |= {pp.APPLY_COMMAND, pp.RESTORE_COMMAND}
    return sent


_EVENTS = {
    "battery_charge": {"chargeSource": "grid", "targetSoc": 80},
    "sell_to_grid": {"sellPowerCapW": 3000, "disableTouDuringEvent": True},
    "use_battery": {},
    "gen_force": {},
    "battery_maintenance_charge": {},
    "battery_maintenance": {},
}


@pytest.mark.parametrize("mode", sorted(_EVENTS))
def test_an_island_event_sends_only_what_its_mode_authorizes_on_the_box(mode):
    event = {
        "id": "ev-1",
        "inverterId": "inv-A",
        "mode": mode,
        "schedule": {"startTime": "23:00", "endTime": "06:00", "recurrence": "daily"},
        "config": _EVENTS[mode],
    }
    sent = _sent(event)
    assert sent, mode
    assert sent <= set(_box_list(mode)), sorted(sent - set(_box_list(mode)))


def test_the_island_socket_modes_switch_with_set_state():
    for mode in ("lower_consumption", "consume_from_sun"):
        assert "set_state" in _box_list(mode)
    for kind in ("smart_device", "smart_device_except"):
        assert "set_state" in _BY_MODE["custom"][kind]


_CUSTOM_ACTIONS = {
    "work_mode": {"value": 0},
    "solar_sell": {"value": 1},
    "sell_power_cap": {"valueW": 3000},
    "grid_charge_toggle": {"enabled": False},
    "gen_force": {"on": True},
    "gen_port_mode": {"value": 1},
    "energy_pattern": {"value": 1},
    "battery_charge_current": {"currentA": 40},
    "tou_slots": {
        "slotIndex": 1,
        "gridChargeEnabled": True,
        "socTarget": 90,
        "powerW": 3000,
        "startTime": "01:00",
        "endTime": "05:00",
    },
}


@pytest.mark.parametrize("kind", sorted(_CUSTOM_ACTIONS))
def test_an_island_custom_action_sends_only_what_its_type_authorizes_on_the_box(kind):
    event = {
        "id": "ev-2",
        "inverterId": "inv-A",
        "mode": "custom",
        "schedule": {"startTime": "18:00", "endTime": "22:00", "recurrence": "always"},
        "config": {"customActions": [{"type": kind, **_CUSTOM_ACTIONS[kind]}]},
    }
    sent = _sent(event)
    assert sent, kind
    assert sent <= set(_BY_MODE["custom"][kind]), sorted(sent - set(_BY_MODE["custom"][kind]))


def test_every_custom_action_the_scheduler_writes_has_a_box_list():
    assert set(ev._CUSTOM_ACTIONS) | {"tou_slots"} <= set(_CUSTOM_ACTIONS)


def test_a_brand_custom_action_sends_only_what_its_type_authorizes_on_the_box():
    for kind, command in ev._BRAND_ACTION_COMMANDS.items():
        assert command in _BY_MODE["custom"][kind], kind
    for kind, threshold in ev._THRESHOLDS.items():
        assert {threshold.activate, threshold.restore} <= set(_BY_MODE["custom"][kind]), kind
    assert {pp.APPLY_COMMAND, pp.RESTORE_COMMAND} <= set(_BY_MODE["custom"]["priority_profile"])
