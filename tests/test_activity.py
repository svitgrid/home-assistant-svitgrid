"""Tests for ActivityTracker — the shared object that feeds the
status/ingest/command sensors and the recent-events buffers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.svitgrid.activity import ActivityTracker


def _now() -> datetime:
    return datetime(2026, 5, 22, 14, 0, 0, tzinfo=UTC)


def test_initial_state_has_no_activity():
    t = ActivityTracker(now=_now)
    assert t.status == "idle"
    assert t.last_ingest_at is None
    assert t.last_ingest_status is None
    assert t.ingest_count_24h == 0
    assert t.last_command_at is None
    assert t.last_command_kind is None
    assert t.command_count_24h == 0
    assert list(t.recent_ingests()) == []
    assert list(t.recent_commands()) == []


def test_record_ingest_success_updates_status_count_and_buffer():
    t = ActivityTracker(now=_now)
    t.record_ingest_success(
        sample_count=5,
        period_sec=300,
        summary={
            "pvPower": 4200.0,
            "loadPower": 1500.0,
        },
    )
    assert t.status == "ok"
    assert t.last_ingest_at == _now()
    assert t.last_ingest_status == "ok"
    assert t.ingest_count_24h == 1
    recents = list(t.recent_ingests())
    assert len(recents) == 1
    assert recents[0]["sample_count"] == 5
    assert recents[0]["status"] == "ok"
    assert recents[0]["summary"]["pvPower"] == 4200.0


def test_record_ingest_failure_marks_error_status():
    t = ActivityTracker(now=_now)
    t.record_ingest_failure(reason="HTTP 400 validation error")
    assert t.status == "error"
    assert t.last_ingest_status == "error"
    # Counter increments even on failure (it's "ingest attempts")
    assert t.ingest_count_24h == 1
    recents = list(t.recent_ingests())
    assert recents[0]["status"] == "error"
    assert recents[0]["reason"] == "HTTP 400 validation error"


def test_record_command_updates_state_and_buffer():
    t = ActivityTracker(now=_now)
    t.record_command(
        kind="set_battery_charge",
        payload={"chargePowerLimitW": 2000},
        result={"appliedPowerW": 2000, "registerValue": 417},
        success=True,
    )
    assert t.last_command_at == _now()
    assert t.last_command_kind == "set_battery_charge"
    assert t.command_count_24h == 1
    recents = list(t.recent_commands())
    assert recents[0]["kind"] == "set_battery_charge"
    assert recents[0]["success"] is True
    assert recents[0]["payload"]["chargePowerLimitW"] == 2000
    assert recents[0]["result"]["registerValue"] == 417


def test_recent_buffers_cap_at_10():
    t = ActivityTracker(now=_now)
    for i in range(15):
        t.record_ingest_success(sample_count=1, period_sec=60, summary={"pvPower": float(i)})
    # Only the 10 most recent retained.
    recents = list(t.recent_ingests())
    assert len(recents) == 10
    # FIFO eviction: earliest 5 dropped.
    assert recents[0]["summary"]["pvPower"] == 5.0
    assert recents[-1]["summary"]["pvPower"] == 14.0


def test_24h_counters_evict_old_entries():
    """ingest_count_24h reflects only events within the last 24h."""
    clock = [_now()]

    def fake_now():
        return clock[0]

    t = ActivityTracker(now=fake_now)
    # 3 ingests at t0
    for _ in range(3):
        t.record_ingest_success(sample_count=1, period_sec=60, summary={})
    assert t.ingest_count_24h == 3

    # Advance 25 hours; the 3 prior ingests are now outside the window.
    clock[0] = _now() + timedelta(hours=25)
    # Record one new ingest at the new time.
    t.record_ingest_success(sample_count=1, period_sec=60, summary={})
    assert t.ingest_count_24h == 1


def test_status_reflects_most_recent_outcome():
    t = ActivityTracker(now=_now)
    t.record_ingest_success(sample_count=1, period_sec=60, summary={})
    assert t.status == "ok"
    t.record_ingest_failure(reason="HTTP 500")
    assert t.status == "error"
    # Subsequent success recovers status.
    t.record_ingest_success(sample_count=1, period_sec=60, summary={})
    assert t.status == "ok"


def test_record_ingest_skipped_sets_waiting_status_and_event():
    t = ActivityTracker(now=_now)
    t.record_ingest_skipped(
        missing_fields=["batterySoc", "gridPower"],
        entities={"batterySoc": "sensor.soc", "gridPower": None},
    )
    assert t.status == "waiting"
    assert t.last_ingest_status == "skipped"
    recent = list(t.recent_ingests())
    assert recent[-1]["status"] == "skipped"
    assert recent[-1]["missing_fields"] == ["batterySoc", "gridPower"]
    assert recent[-1]["entities"] == {"batterySoc": "sensor.soc", "gridPower": None}


def test_skip_does_not_count_toward_ingest_24h():
    t = ActivityTracker(now=_now)
    t.record_ingest_skipped(missing_fields=["batterySoc"], entities={})
    # A skip is not a network ingest — counters track real attempts only.
    assert t.ingest_count_24h == 0


def test_diagnostics_line_waiting_names_missing_fields():
    t = ActivityTracker(now=_now)
    t.record_ingest_skipped(missing_fields=["batterySoc", "gridPower"], entities={})
    line = t.diagnostics_line()
    assert "waiting" in line.lower()
    assert "batterySoc" in line and "gridPower" in line
    assert len(line) <= 255


def test_diagnostics_line_ok_after_success():
    t = ActivityTracker(now=_now)
    t.record_ingest_success(sample_count=1, period_sec=60, summary={"pvPower": 0.0})
    assert t.diagnostics_line() == "ok"


def test_diagnostics_line_idle_initially():
    assert ActivityTracker().diagnostics_line() == "idle"


def test_lifecycle_overrides_status():
    a = ActivityTracker()
    a.record_ingest_success(sample_count=1, period_sec=0, summary={})
    assert a.status == "ok"
    a.set_lifecycle("deprovisioned", "revoked")
    assert a.status == "deprovisioned"
    assert "re-pair" in a.diagnostics_line().lower()
    a.set_lifecycle("paused", "disabled")
    assert a.status == "paused"
    assert "paused" in a.diagnostics_line().lower()


# ── Mapped-but-unresolved sensors surface on the Diagnostics sensor ────────


def test_diagnostics_line_flags_unresolved_sensors_even_on_success():
    """A reading that sends fine but silently drops a mapped sensor must say
    so — otherwise a mapped-but-dead PV sensor just reads as 0 W forever."""
    t = ActivityTracker(now=_now)
    t.record_ingest_success(
        sample_count=1,
        period_sec=60,
        summary={"pvPower": 0.0},
        unresolved={"pv1Power": "sensor.victron_pv_power"},
    )
    line = t.diagnostics_line()
    assert line.startswith("ok")
    assert "pv1Power" in line
    assert "sensor.victron_pv_power" in line
    assert len(line) <= 255


def test_diagnostics_line_plain_ok_when_nothing_unresolved():
    t = ActivityTracker(now=_now)
    t.record_ingest_success(sample_count=1, period_sec=60, summary={"pvPower": 1.0}, unresolved={})
    assert t.diagnostics_line() == "ok"


def test_recent_ingest_carries_unresolved_entities():
    t = ActivityTracker(now=_now)
    t.record_ingest_success(
        sample_count=1,
        period_sec=60,
        summary={},
        unresolved={"pv1Power": "sensor.pv"},
    )
    assert list(t.recent_ingests())[-1]["unresolved"] == {"pv1Power": "sensor.pv"}


# ── Signing-key approval state ────────────────────────────────────────────
#
# An integration paired into a household that already has an approved phone
# gets a PENDING key by design. The ACK path on the server loads keys with
# status == 'approved' only, so every control-command ACK 401s while telemetry
# keeps flowing — the install looks healthy and control is silently dead.


def test_signing_key_is_assumed_approved_until_told_otherwise():
    """Fail-OPEN: an older API that never sends the status must not put 90
    healthy households into a false 'waiting for approval' state."""
    t = ActivityTracker(now=_now)
    t.record_ingest_success(sample_count=1, period_sec=60, summary={})
    assert t.diagnostics_line() == "ok"


def test_diagnostics_line_reports_a_pending_signing_key():
    t = ActivityTracker(now=_now)
    t.set_signing_key_approved(False)
    t.record_ingest_success(sample_count=1, period_sec=60, summary={})
    line = t.diagnostics_line()
    assert "approv" in line.lower()
    assert len(line) <= 255


def test_incomplete_reading_outranks_a_pending_signing_key():
    """No data at all is more urgent than control commands being refused."""
    t = ActivityTracker(now=_now)
    t.set_signing_key_approved(False)
    t.record_ingest_skipped(missing_fields=["gridPower"], entities={"gridPower": "sensor.g"})
    assert "gridPower" in t.diagnostics_line()


def test_approving_the_key_clears_the_line():
    t = ActivityTracker(now=_now)
    t.set_signing_key_approved(False)
    t.record_ingest_success(sample_count=1, period_sec=60, summary={})
    assert "approv" in t.diagnostics_line().lower()
    t.set_signing_key_approved(True)
    assert t.diagnostics_line() == "ok"
