"""Tests for ActivityTracker — the shared object that feeds the
status/ingest/command sensors and the recent-events buffers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.svitgrid.activity import ActivityTracker


def _now() -> datetime:
    return datetime(2026, 5, 22, 14, 0, 0, tzinfo=timezone.utc)


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
    t.record_ingest_success(sample_count=5, period_sec=300, summary={
        "pvPower": 4200.0,
        "loadPower": 1500.0,
    })
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
