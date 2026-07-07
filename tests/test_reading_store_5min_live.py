"""Tests for ReadingStore.five_min_range_live — computes 5-minute buckets live
from readings_raw so the island Day charts get fine-grained (~288 pts/day)
resolution matching the cloud, instead of coarse hourly (24 pts/day).
"""

from custom_components.svitgrid import rollup as _rollup
from custom_components.svitgrid.reading_store import ReadingStore


def _store(tmp_path):
    return ReadingStore(None, str(tmp_path / "readings.db"))


def test_five_min_of_floors_to_five_minute_bucket(tmp_path):
    store = _store(tmp_path)
    assert store._five_min_of("2026-06-24T10:17:30.5Z") == "2026-06-24T10:15:00Z"
    assert store._five_min_of("2026-06-24T10:00:00Z") == "2026-06-24T10:00:00Z"
    assert store._five_min_of("2026-06-24T10:04:59Z") == "2026-06-24T10:00:00Z"
    assert store._five_min_of("2026-06-24T10:05:00Z") == "2026-06-24T10:05:00Z"
    assert store._five_min_of("2026-06-24T10:59:59Z") == "2026-06-24T10:55:00Z"


def test_five_min_range_live_buckets_by_five_minutes(tmp_path):
    """Raw rows are grouped into 5-minute buckets, each aggregated with
    rollup.aggregate — same shape/keys as the hourly buckets (so the mobile
    mapping is reused unchanged), and the current in-progress bucket appears."""
    store = _store(tmp_path)
    day = "2026-06-24"

    b0 = [  # 10:00-10:04 → bucket 10:00
        {
            "inverterId": "inv-1",
            "timestamp": "2026-06-24T10:01:00Z",
            "pvPower": 1000.0,
            "batterySoc": 50.0,
            "dailyPvEnergy": 1.0,
        },
        {
            "inverterId": "inv-1",
            "timestamp": "2026-06-24T10:03:00Z",
            "pvPower": 1200.0,
            "batterySoc": 51.0,
            "dailyPvEnergy": 1.1,
        },
    ]
    b5 = [  # 10:05-10:09 → bucket 10:05
        {
            "inverterId": "inv-1",
            "timestamp": "2026-06-24T10:07:30Z",
            "pvPower": 1500.0,
            "batterySoc": 52.0,
            "dailyPvEnergy": 1.2,
        },
    ]
    b15 = [  # 10:17 → bucket 10:15
        {
            "inverterId": "inv-1",
            "timestamp": "2026-06-24T10:17:00Z",
            "pvPower": 1800.0,
            "batterySoc": 55.0,
            "dailyPvEnergy": 1.5,
        },
    ]
    for r in b0 + b5 + b15:
        store._append_sync(r)

    result = store._five_min_range_live_sync("inv-1", day)

    buckets = [b["hour"] for b in result]
    assert buckets == [
        "2026-06-24T10:00:00Z",
        "2026-06-24T10:05:00Z",
        "2026-06-24T10:15:00Z",
    ]

    by = {b["hour"]: b for b in result}
    exp0 = _rollup.aggregate([{"payload": r} for r in b0])
    assert by["2026-06-24T10:00:00Z"]["sample_count"] == exp0["sample_count"] == 2
    assert by["2026-06-24T10:00:00Z"]["avgs"] == exp0["avgs"]
    assert by["2026-06-24T10:00:00Z"]["peaks"] == exp0["peaks"]
    assert by["2026-06-24T10:00:00Z"]["energy"] == exp0["energy"]

    # Shape/keys match the hourly bucket exactly (mobile reuses the same mapper).
    assert set(by["2026-06-24T10:05:00Z"].keys()) == {
        "hour",
        "sample_count",
        "avgs",
        "peaks",
        "energy",
    }


def test_five_min_range_live_excludes_other_days(tmp_path):
    store = _store(tmp_path)
    store._append_sync(
        {"inverterId": "inv-1", "timestamp": "2026-06-24T23:57:00Z", "pvPower": 100.0}
    )
    store._append_sync(
        {"inverterId": "inv-1", "timestamp": "2026-06-25T00:02:00Z", "pvPower": 200.0}
    )
    result = store._five_min_range_live_sync("inv-1", "2026-06-24")
    assert [b["hour"] for b in result] == ["2026-06-24T23:55:00Z"]


def test_bucket_includes_per_phase_grid_voltage(tmp_path):
    """Per-phase grid voltage (gridVoltageL1..L3) must survive aggregation into
    the buckets — otherwise the island Grid Voltage Day chart has no data.
    Raw readings carry them but rollup.aggregate only averages
    INSTANTANEOUS_FIELDS, which historically omitted grid voltage.
    """
    store = _store(tmp_path)
    store._append_sync(
        {
            "inverterId": "inv-1",
            "timestamp": "2026-06-24T10:01:00Z",
            "pvPower": 100.0,
            "gridVoltageL1": 230.0,
            "gridVoltageL2": 231.0,
            "gridVoltageL3": 229.0,
        }
    )
    store._append_sync(
        {
            "inverterId": "inv-1",
            "timestamp": "2026-06-24T10:03:00Z",
            "pvPower": 120.0,
            "gridVoltageL1": 232.0,
            "gridVoltageL2": 233.0,
            "gridVoltageL3": 231.0,
        }
    )
    avgs = store._five_min_range_live_sync("inv-1", "2026-06-24")[0]["avgs"]
    assert "gridVoltageL1" in avgs
    assert "gridVoltageL2" in avgs
    assert "gridVoltageL3" in avgs
    assert avgs["gridVoltageL1"] == 231.0  # (230 + 232) / 2
