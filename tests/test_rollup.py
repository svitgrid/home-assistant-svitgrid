from custom_components.svitgrid import rollup
from custom_components.svitgrid.reading_store import ReadingStore


def test_aggregate_means_peaks_and_energy():
    rows = [
        {"payload": {"batterySoc": 80.0, "pvPower": 1000.0, "dailyPvEnergy": 5.0}},
        {"payload": {"batterySoc": 90.0, "pvPower": 3000.0, "dailyPvEnergy": 8.0}},
    ]
    agg = rollup.aggregate(rows)
    assert agg["sample_count"] == 2
    assert agg["avgs"]["batterySoc"] == 85.0
    assert agg["avgs"]["pvPower"] == 2000.0
    assert agg["peaks"]["pvPower"] == 3000.0
    assert agg["energy"]["dailyPvEnergy"] == 8.0  # max = end-of-period total


def test_merge_hourly_sample_weighted_mean():
    h1 = {"sample_count": 2, "avgs": {"pvPower": 1000.0}, "peaks": {"pvPower": 1500.0},
          "energy": {"dailyPvEnergy": 4.0}}
    h2 = {"sample_count": 6, "avgs": {"pvPower": 2000.0}, "peaks": {"pvPower": 3000.0},
          "energy": {"dailyPvEnergy": 9.0}}
    daily = rollup.merge_hourly([h1, h2])
    assert daily["sample_count"] == 8
    # weighted: (1000*2 + 2000*6)/8 = 1750
    assert daily["avgs"]["pvPower"] == 1750.0
    assert daily["peaks"]["pvPower"] == 3000.0
    assert daily["energy"]["dailyPvEnergy"] == 9.0


def test_store_rollup_aggregates_completed_hour(tmp_path):
    store = ReadingStore(None, str(tmp_path / "readings.db"))
    # two raw rows in the 10:00 hour; "now" is 12:00 so 10:00 is complete
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z",
                        "pvPower": 1000.0, "dailyPvEnergy": 5.0, "batterySoc": 80.0})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:30:00Z",
                        "pvPower": 3000.0, "dailyPvEnergy": 8.0, "batterySoc": 90.0})
    counts = store._rollup_sync("2026-06-24T12:00:00Z")
    assert counts["hours"] == 1
    conn = store._connect_for_test()
    try:
        rows = conn.execute(
            "SELECT inverter_id, hour_start, sample_count, avgs, peaks, energy "
            "FROM readings_hourly").fetchall()
        assert len(rows) == 1
        import json
        assert json.loads(rows[0]["avgs"])["pvPower"] == 2000.0
        assert json.loads(rows[0]["energy"])["dailyPvEnergy"] == 8.0
    finally:
        conn.close()


def test_merge_hourly_field_present_in_one_hour():
    h1 = {"sample_count": 2, "avgs": {"pvPower": 1000.0}, "peaks": {}, "energy": {}}
    h2 = {"sample_count": 6, "avgs": {"batterySoc": 50.0}, "peaks": {}, "energy": {}}
    daily = rollup.merge_hourly([h1, h2])
    # pvPower only in h1 → weighted only by h1's 2 samples → 1000.0 (NOT 1000*2/8=250)
    assert daily["avgs"]["pvPower"] == 1000.0
    # batterySoc only in h2 → 50.0
    assert daily["avgs"]["batterySoc"] == 50.0
    # overall sample_count is the raw sum
    assert daily["sample_count"] == 8


def test_aggregate_includes_tier2_daily_counters():
    from custom_components.svitgrid import rollup
    rows = [{"payload": {"dailyBatteryChargeEnergy": 5.0, "dailyGeneratorEnergy": 2.0}},
            {"payload": {"dailyBatteryChargeEnergy": 8.0, "dailyGeneratorEnergy": 2.0}}]
    agg = rollup.aggregate(rows)
    assert agg["energy"]["dailyBatteryChargeEnergy"] == 8.0   # max over the day
    assert agg["energy"]["dailyGeneratorEnergy"] == 2.0


def test_aggregate_includes_tier3_losses_and_loadfreq():
    from custom_components.svitgrid import rollup
    rows = [{"payload": {"dailyLossesEnergy": 1.0, "loadFrequency": 50.0}},
            {"payload": {"dailyLossesEnergy": 3.0, "loadFrequency": 50.04}}]
    agg = rollup.aggregate(rows)
    assert agg["energy"]["dailyLossesEnergy"] == 3.0          # max over day (DAILY_COUNTER_FIELDS)
    assert 49.0 <= agg["avgs"]["loadFrequency"] <= 51.0  # averaged (INSTANTANEOUS_FIELDS)


def test_prune_drops_old_raw_keeps_daily(tmp_path):
    store = ReadingStore(None, str(tmp_path / "readings.db"))
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-05-01T10:00:00Z",
                        "pvPower": 1.0})  # >14d before "now"
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z",
                        "pvPower": 1.0})
    pruned = store._prune_sync("2026-06-24T12:00:00Z",
                               raw_retention_s=14 * 86400, hourly_retention_s=2 * 365 * 86400)
    assert pruned["raw"] == 1
    assert store._count_by_state_sync() == {"pending": 1}
