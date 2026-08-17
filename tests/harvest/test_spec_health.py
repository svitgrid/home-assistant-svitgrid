"""A register spec the decoder cannot execute must be LOUD.

Before 2026-08-17 `RegisterSpec.validate()` was dead code outside the test
suite, so a spec carrying a builtin this add-on does not implement produced,
from the user's side, exactly the same thing as a spec that was never fetched
at all: an inverter that pairs successfully, shows as configured in the app,
and then reports nothing, indefinitely, behind a `logger.debug` line.

These tests pin the three surfaces that now make that impossible:
  1. build_spec() refuses an unexecutable spec instead of installing it.
  2. It logs at ERROR, naming the model and the problem.
  3. It records the problem on the ActivityTracker, which is what
     `sensor.svitgrid_diagnostics` renders.
"""

from __future__ import annotations

import logging

from custom_components.svitgrid.activity import ActivityTracker
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.spec_health import (
    SPEC_UNAVAILABLE_TICKS,
    build_spec,
    report_spec_unavailable,
)

_GOOD = {
    "modelId": "deye_sg04lp3",
    "version": 1,
    "protocol": "solarman_v5",
    "port": 8899,
    "defaultSlaveId": 1,
    "flags": {},
    "reads": [{"field": "batterySoc", "address": 588}],
    "derivations": [],
    "writes": [],
}

_UNKNOWN_BUILTIN = {
    **_GOOD,
    "modelId": "brandnew_9000",
    "derivations": [
        {
            "field": "gridPower",
            "op": "builtin",
            "builtin": "some_future_builtin",
            "inputs": ["batterySoc"],
        }
    ],
}


# ---------------------------------------------------------------------------
# build_spec
# ---------------------------------------------------------------------------


def test_build_spec_returns_a_spec_for_an_executable_document():
    activity = ActivityTracker()
    spec = build_spec(_GOOD, model_id="deye_sg04lp3", activity=activity)
    assert isinstance(spec, RegisterSpec)
    assert spec.model_id == "deye_sg04lp3"
    assert activity.spec_problem is None


def test_build_spec_refuses_a_spec_with_an_unknown_builtin():
    """Installing it would make _apply_builtin raise on every tick instead."""
    assert build_spec(_UNKNOWN_BUILTIN, model_id="brandnew_9000") is None


def test_build_spec_logs_the_unknown_builtin_at_error_level(caplog):
    with caplog.at_level(logging.DEBUG):
        build_spec(_UNKNOWN_BUILTIN, model_id="brandnew_9000")
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "an unexecutable spec must not sit below ERROR"
    joined = " ".join(r.getMessage() for r in errors)
    assert "brandnew_9000" in joined
    assert "some_future_builtin" in joined


def test_build_spec_records_the_problem_on_the_activity_tracker():
    activity = ActivityTracker()
    build_spec(_UNKNOWN_BUILTIN, model_id="brandnew_9000", activity=activity)
    assert activity.spec_problem is not None
    assert "some_future_builtin" in activity.spec_problem


def test_build_spec_surfaces_an_unparseable_document_loudly(caplog):
    """A malformed spec (missing required key) must not be swallowed either."""
    activity = ActivityTracker()
    with caplog.at_level(logging.DEBUG):
        spec = build_spec({"version": 1}, model_id="broken_model", activity=activity)
    assert spec is None
    assert activity.spec_problem is not None
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_build_spec_handles_a_missing_document_as_unavailable():
    """None is the 404/500 case — different message, same visibility."""
    activity = ActivityTracker()
    assert build_spec(None, model_id="srne_asf_10k", activity=activity) is None
    assert activity.spec_problem is not None
    assert "srne_asf_10k" in activity.spec_problem


def test_build_spec_clears_a_stale_problem_on_success():
    activity = ActivityTracker()
    build_spec(_UNKNOWN_BUILTIN, model_id="brandnew_9000", activity=activity)
    assert activity.spec_problem is not None
    build_spec(_GOOD, model_id="deye_sg04lp3", activity=activity)
    assert activity.spec_problem is None


# ---------------------------------------------------------------------------
# report_spec_unavailable — the 404 / never-fetched path in the harvest loop
# ---------------------------------------------------------------------------


def test_report_spec_unavailable_stays_quiet_for_the_first_few_ticks(caplog):
    """Startup races are normal — don't cry wolf on tick 1."""
    activity = ActivityTracker()
    with caplog.at_level(logging.DEBUG):
        for n in range(1, SPEC_UNAVAILABLE_TICKS):
            report_spec_unavailable(
                model_id="srne_asf_10k",
                inverter_id="inv-1",
                consecutive=n,
                activity=activity,
            )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert activity.spec_problem is None


def test_report_spec_unavailable_escalates_after_n_ticks(caplog):
    activity = ActivityTracker()
    with caplog.at_level(logging.DEBUG):
        report_spec_unavailable(
            model_id="srne_asf_10k",
            inverter_id="inv-1",
            consecutive=SPEC_UNAVAILABLE_TICKS,
            activity=activity,
        )
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert loud, "a spec that never arrives must escalate above debug"
    assert "srne_asf_10k" in " ".join(r.getMessage() for r in loud)
    assert activity.spec_problem is not None
    assert "srne_asf_10k" in activity.spec_problem


def test_report_spec_unavailable_does_not_re_log_every_tick(caplog):
    """Escalate once at the threshold, not once a minute for ever."""
    activity = ActivityTracker()
    with caplog.at_level(logging.DEBUG):
        for n in (SPEC_UNAVAILABLE_TICKS, SPEC_UNAVAILABLE_TICKS + 1, SPEC_UNAVAILABLE_TICKS + 2):
            report_spec_unavailable(
                model_id="srne_asf_10k",
                inverter_id="inv-1",
                consecutive=n,
                activity=activity,
            )
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1
