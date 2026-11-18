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
