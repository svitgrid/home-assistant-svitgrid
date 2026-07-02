"""Tests for ReadingStore.hourly_range_live — computes hourly buckets live from
readings_raw (INCLUDING the current in-progress hour), so a fresh household whose
raw data has not yet been sealed into readings_hourly still gets a Day chart.
"""
import json

from custom_components.svitgrid.reading_store import ReadingStore
from custom_components.svitgrid import rollup as _rollup


def _store(tmp_path):
    return ReadingStore(None, str(tmp_path / "readings.db"))


def test_hourly_range_live_includes_completed_and_current_hour(tmp_path):
    """Raw across two hours (a completed hour + the CURRENT in-progress hour)
    both appear, and each bucket matches what rollup.aggregate produces for that
    hour's rows — matching _hourly_range_sync's shape exactly."""
    store = _store(tmp_path)
    day = "2026-06-24"

    # Completed hour 09:00 — two samples.
    h9_readings = [
        {"inverterId": "inv-1", "timestamp": "2026-06-24T09:00:00Z",
         "pvPower": 500.0, "batterySoc": 40.0, "dailyPvEnergy": 0.5},
        {"inverterId": "inv-1", "timestamp": "2026-06-24T09:30:00Z",
         "pvPower": 900.0, "batterySoc": 45.0, "dailyPvEnergy": 0.9},
    ]
    # "Current" in-progress hour 10:00 — the rollup would SKIP this hour, so it
    # is invisible via the sealed table but MUST be visible here.
    h10_readings = [
        {"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z",
         "pvPower": 1500.0, "batterySoc": 50.0, "dailyPvEnergy": 1.5},
        {"inverterId": "inv-1", "timestamp": "2026-06-24T10:15:00Z",
         "pvPower": 2000.0, "batterySoc": 55.0, "dailyPvEnergy": 2.0},
        {"inverterId": "inv-1", "timestamp": "2026-06-24T10:30:00Z",
         "pvPower": 1800.0, "batterySoc": 60.0, "dailyPvEnergy": 2.2},
    ]
    for r in h9_readings + h10_readings:
        store._append_sync(r)

    result = store._hourly_range_live_sync("inv-1", day)

    hours = [b["hour"] for b in result]
    assert hours == ["2026-06-24T09:00:00Z", "2026-06-24T10:00:00Z"]

    by_hour = {b["hour"]: b for b in result}

    # Each bucket must equal exactly what rollup.aggregate produces for its rows.
    exp9 = _rollup.aggregate([{"payload": r} for r in h9_readings])
    exp10 = _rollup.aggregate([{"payload": r} for r in h10_readings])

    b9 = by_hour["2026-06-24T09:00:00Z"]
    assert b9["sample_count"] == exp9["sample_count"] == 2
    assert b9["avgs"] == exp9["avgs"]
    assert b9["peaks"] == exp9["peaks"]
    assert b9["energy"] == exp9["energy"]

    b10 = by_hour["2026-06-24T10:00:00Z"]
    assert b10["sample_count"] == exp10["sample_count"] == 3
    assert b10["avgs"] == exp10["avgs"]
    assert b10["peaks"] == exp10["peaks"]
    assert b10["energy"] == exp10["energy"]

    # Shape must match _hourly_range_sync exactly (same keys).
    assert set(b10.keys()) == {"hour", "sample_count", "avgs", "peaks", "energy"}


def test_hourly_range_live_current_hour_absent_from_sealed_table(tmp_path):
    """Sanity: the sealed rollup would NOT expose the current hour, proving the
    live path adds value. Same raw data, contrasting the two readers."""
    store = _store(tmp_path)
    now = "2026-06-24T10:45:00Z"  # current hour = 10:00
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T09:10:00Z",
                        "pvPower": 500.0})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:20:00Z",
                        "pvPower": 1500.0})

    # Seal completed hours only (mirrors production rollup behavior).
    store._rollup_sync(now)
    sealed = store._hourly_range_sync("inv-1", "2026-06-24")
    sealed_hours = [b["hour"] for b in sealed]
    assert "2026-06-24T10:00:00Z" not in sealed_hours  # current hour skipped

    live = store._hourly_range_live_sync("inv-1", "2026-06-24")
    live_hours = [b["hour"] for b in live]
    assert live_hours == ["2026-06-24T09:00:00Z", "2026-06-24T10:00:00Z"]


def test_hourly_range_live_excludes_other_inverters_and_days(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z", "pvPower": 1.0})
    store._append_sync({"inverterId": "inv-2", "timestamp": "2026-06-24T10:00:00Z", "pvPower": 9.0})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-23T10:00:00Z", "pvPower": 5.0})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-25T10:00:00Z", "pvPower": 5.0})

    result = store._hourly_range_live_sync("inv-1", "2026-06-24")
    assert [b["hour"] for b in result] == ["2026-06-24T10:00:00Z"]
    assert result[0]["sample_count"] == 1


def test_hourly_range_live_includes_end_of_day_boundary(tmp_path):
    """A 23:59:59 reading falls within the day and must be bucketed."""
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T23:59:59Z", "pvPower": 3.0})
    result = store._hourly_range_live_sync("inv-1", "2026-06-24")
    assert [b["hour"] for b in result] == ["2026-06-24T23:00:00Z"]


def test_hourly_range_live_empty_day_returns_empty(tmp_path):
    store = _store(tmp_path)
    assert store._hourly_range_live_sync("inv-1", "2026-06-24") == []


def test_hourly_range_live_includes_subsecond_last_second_of_day(tmp_path):
    """A reading at 23:59:59.743Z (sub-second, string > T23:59:59Z) must still be
    bucketed into the 23:00 hour — regression for the exclusive next-day bound."""
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T23:59:59.743115Z",
                        "pvPower": 3.0})
    result = store._hourly_range_live_sync("inv-1", "2026-06-24")
    assert [b["hour"] for b in result] == ["2026-06-24T23:00:00Z"]
    assert result[0]["sample_count"] == 1


def test_month_bounds_valid_month(tmp_path):
    store = _store(tmp_path)
    assert store._month_bounds("2026-02") == ("2026-02-01", "2026-02-28")
    assert store._month_bounds("2024-02") == ("2024-02-01", "2024-02-29")  # leap year


def test_month_bounds_malformed_month_raises_valueerror(tmp_path):
    """Malformed month must raise ValueError (endpoint maps it to HTTP 400),
    never crash with an unhandled error."""
    import pytest
    store = _store(tmp_path)
    for bad in ("foo", "2026-13", "2026-00", "", "2026"):
        with pytest.raises(ValueError):
            store._month_bounds(bad)
