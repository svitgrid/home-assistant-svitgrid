"""Tests for ReadingStore._history_range_live_sync — sealed prior days + today live
from readings_raw (INCLUDING the current in-progress day), so the daily /history
path serves today's data even before it is rolled up into readings_daily.
"""
import json

from custom_components.svitgrid.reading_store import ReadingStore
from custom_components.svitgrid import rollup as _rollup


def _store(tmp_path):
    return ReadingStore(None, str(tmp_path / "readings.db"))


def _insert_daily(store, inv, day, agg):
    """Directly insert a sealed readings_daily row."""
    conn = store._connect_for_test()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO readings_daily "
            "(inverter_id, day, sample_count, avgs, peaks, energy) VALUES (?,?,?,?,?,?)",
            (inv, day, agg["sample_count"], json.dumps(agg["avgs"]),
             json.dumps(agg["peaks"]), json.dumps(agg["energy"])),
        )
        conn.commit()
    finally:
        conn.close()


# ── main integration test ──────────────────────────────────────────────────────

def test_history_range_live_sealed_prior_plus_today_live(tmp_path):
    """Sealed prior days returned unchanged; today aggregated live from raw.

    The today bucket must match merge_hourly(aggregate-per-hour) field-by-field,
    mirroring exactly what _rollup_sync would produce once the day is sealed.
    """
    store = _store(tmp_path)
    inv = "inv-1"
    # Pin "today" via now_iso
    now_iso = "2026-06-26T10:45:00Z"

    # Sealed rows for two prior days
    agg_24 = {
        "sample_count": 48,
        "avgs": {"pvPower": 500.0},
        "peaks": {"pvPower": 1000.0},
        "energy": {"dailyPvEnergy": 5.0},
    }
    agg_25 = {
        "sample_count": 48,
        "avgs": {"pvPower": 600.0},
        "peaks": {"pvPower": 1200.0},
        "energy": {"dailyPvEnergy": 6.0},
    }
    _insert_daily(store, inv, "2026-06-24", agg_24)
    _insert_daily(store, inv, "2026-06-25", agg_25)

    # Today's raw rows across two hours (h09 + h10)
    today_h9 = [
        {"inverterId": inv, "timestamp": "2026-06-26T09:00:00Z",
         "pvPower": 400.0, "dailyPvEnergy": 0.4},
        {"inverterId": inv, "timestamp": "2026-06-26T09:30:00Z",
         "pvPower": 800.0, "dailyPvEnergy": 0.8},
    ]
    today_h10 = [
        {"inverterId": inv, "timestamp": "2026-06-26T10:00:00Z",
         "pvPower": 1200.0, "dailyPvEnergy": 1.2},
        {"inverterId": inv, "timestamp": "2026-06-26T10:30:00Z",
         "pvPower": 1000.0, "dailyPvEnergy": 1.5},
    ]
    for r in today_h9 + today_h10:
        store._append_sync(r)

    result = store._history_range_live_sync(
        inv, "2026-06-24", "2026-06-26", now_iso=now_iso)

    # --- sorted by day ---
    days = [r["day"] for r in result]
    assert days == ["2026-06-24", "2026-06-25", "2026-06-26"]

    # --- prior sealed days returned unchanged ---
    r24 = next(r for r in result if r["day"] == "2026-06-24")
    assert r24["sample_count"] == 48
    assert r24["avgs"] == {"pvPower": 500.0}
    assert r24["peaks"] == {"pvPower": 1000.0}
    assert r24["energy"] == {"dailyPvEnergy": 5.0}

    r25 = next(r for r in result if r["day"] == "2026-06-25")
    assert r25["sample_count"] == 48
    assert r25["avgs"] == {"pvPower": 600.0}

    # --- today: must equal merge_hourly(aggregate-per-hour) field-by-field ---
    exp_h9  = _rollup.aggregate([{"payload": r} for r in today_h9])
    exp_h10 = _rollup.aggregate([{"payload": r} for r in today_h10])
    expected_today = _rollup.merge_hourly([exp_h9, exp_h10])

    r26 = next(r for r in result if r["day"] == "2026-06-26")
    assert r26["sample_count"] == expected_today["sample_count"]
    assert r26["avgs"]   == expected_today["avgs"]
    assert r26["peaks"]  == expected_today["peaks"]
    assert r26["energy"] == expected_today["energy"]


# ── range excludes today ───────────────────────────────────────────────────────

def test_history_range_live_excludes_today_when_range_ends_before_today(tmp_path):
    """A range ending strictly before today returns only sealed rows — no today bucket."""
    store = _store(tmp_path)
    inv = "inv-1"
    now_iso = "2026-06-26T10:00:00Z"

    agg = {
        "sample_count": 10,
        "avgs": {"pvPower": 300.0},
        "peaks": {"pvPower": 600.0},
        "energy": {"dailyPvEnergy": 2.0},
    }
    _insert_daily(store, inv, "2026-06-24", agg)
    # Today's raw rows exist but must NOT appear since end_day is 2026-06-25
    store._append_sync({"inverterId": inv, "timestamp": "2026-06-26T09:00:00Z",
                        "pvPower": 900.0})

    result = store._history_range_live_sync(inv, "2026-06-24", "2026-06-25", now_iso=now_iso)
    days = [r["day"] for r in result]
    assert "2026-06-26" not in days
    assert days == ["2026-06-24"]


# ── today only in range ────────────────────────────────────────────────────────

def test_history_range_live_today_only_in_range(tmp_path):
    """Range is exactly today — no sealed rows, only the live today bucket."""
    store = _store(tmp_path)
    inv = "inv-1"
    today = "2026-06-26"
    now_iso = "2026-06-26T11:00:00Z"

    today_readings = [
        {"inverterId": inv, "timestamp": "2026-06-26T09:00:00Z",
         "pvPower": 700.0, "dailyPvEnergy": 1.0},
        {"inverterId": inv, "timestamp": "2026-06-26T10:00:00Z",
         "pvPower": 1100.0, "dailyPvEnergy": 2.0},
    ]
    for r in today_readings:
        store._append_sync(r)

    result = store._history_range_live_sync(inv, today, today, now_iso=now_iso)
    assert len(result) == 1
    assert result[0]["day"] == today

    # Verify sample_count and aggregated energy field-by-field
    exp_h9  = _rollup.aggregate([{"payload": today_readings[0]}])
    exp_h10 = _rollup.aggregate([{"payload": today_readings[1]}])
    expected = _rollup.merge_hourly([exp_h9, exp_h10])
    assert result[0]["sample_count"] == expected["sample_count"] == 2
    assert result[0]["avgs"]   == expected["avgs"]
    assert result[0]["peaks"]  == expected["peaks"]
    assert result[0]["energy"] == expected["energy"]


# ── no raw for today ───────────────────────────────────────────────────────────

def test_history_range_live_today_no_raw_emits_no_today_bucket(tmp_path):
    """Today is in range but has no raw rows — no today bucket emitted (no crash)."""
    store = _store(tmp_path)
    inv = "inv-1"
    now_iso = "2026-06-26T08:00:00Z"

    result = store._history_range_live_sync(inv, "2026-06-26", "2026-06-26", now_iso=now_iso)
    assert result == []


# ── shape parity ───────────────────────────────────────────────────────────────

def test_history_range_live_shape_matches_history_range(tmp_path):
    """Each day dict must have exactly {day, sample_count, avgs, peaks, energy}."""
    store = _store(tmp_path)
    inv = "inv-1"
    now_iso = "2026-06-26T10:00:00Z"
    store._append_sync({"inverterId": inv, "timestamp": "2026-06-26T09:00:00Z",
                        "pvPower": 500.0})

    result = store._history_range_live_sync(inv, "2026-06-26", "2026-06-26", now_iso=now_iso)
    assert len(result) == 1
    assert set(result[0].keys()) == {"day", "sample_count", "avgs", "peaks", "energy"}


# ── other inverters not mixed in ───────────────────────────────────────────────

def test_history_range_live_ignores_other_inverters(tmp_path):
    """Raw rows for a different inverter must not appear in today's bucket."""
    store = _store(tmp_path)
    now_iso = "2026-06-26T12:00:00Z"
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-26T09:00:00Z",
                        "pvPower": 100.0})
    store._append_sync({"inverterId": "inv-2", "timestamp": "2026-06-26T09:00:00Z",
                        "pvPower": 999.0})

    result = store._history_range_live_sync("inv-1", "2026-06-26", "2026-06-26",
                                            now_iso=now_iso)
    assert len(result) == 1
    assert result[0]["sample_count"] == 1
    assert result[0]["avgs"]["pvPower"] == 100.0


# ── no double-count when a sealed row exists for TODAY ─────────────────────────

def test_history_range_live_today_not_double_counted(tmp_path):
    """A sealed readings_daily row for TODAY plus today's raw → today appears
    EXACTLY ONCE (from the live raw path), never doubled from the sealed row."""
    store = _store(tmp_path)
    inv = "inv-1"
    today = "2026-06-26"
    now_iso = "2026-06-26T11:00:00Z"

    # A stale sealed row for today (as if a mid-day rollup ran earlier).
    _insert_daily(store, inv, today, {
        "sample_count": 1,
        "avgs": {"pvPower": 100.0},
        "peaks": {"pvPower": 100.0},
        "energy": {"dailyPvEnergy": 0.1},
    })
    # Fresher raw for today — the live path must supersede the stale sealed row.
    today_readings = [
        {"inverterId": inv, "timestamp": "2026-06-26T09:00:00Z",
         "pvPower": 700.0, "dailyPvEnergy": 1.0},
        {"inverterId": inv, "timestamp": "2026-06-26T10:00:00Z",
         "pvPower": 1100.0, "dailyPvEnergy": 2.0},
    ]
    for r in today_readings:
        store._append_sync(r)

    result = store._history_range_live_sync(inv, today, today, now_iso=now_iso)

    today_rows = [r for r in result if r["day"] == today]
    assert len(today_rows) == 1  # exactly once, not doubled

    # And it is the LIVE value (sample_count 2), not the stale sealed value (1).
    exp = _rollup.merge_hourly([
        _rollup.aggregate([{"payload": today_readings[0]}]),
        _rollup.aggregate([{"payload": today_readings[1]}]),
    ])
    assert today_rows[0]["sample_count"] == exp["sample_count"] == 2
    assert today_rows[0]["energy"] == exp["energy"]


# ── sub-second last-second-of-day reading must be included ─────────────────────

def test_history_range_live_includes_subsecond_last_second_of_day(tmp_path):
    """A reading at 23:59:59.743Z (sub-second, string > T23:59:59Z) must still
    be counted in today's live aggregate — regression for the exclusive bound."""
    store = _store(tmp_path)
    inv = "inv-1"
    today = "2026-06-26"
    now_iso = "2026-06-26T23:59:59.900000Z"

    readings = [
        {"inverterId": inv, "timestamp": "2026-06-26T09:00:00Z", "pvPower": 500.0},
        {"inverterId": inv, "timestamp": "2026-06-26T23:59:59.743115Z", "pvPower": 12.0},
    ]
    for r in readings:
        store._append_sync(r)

    result = store._history_range_live_sync(inv, today, today, now_iso=now_iso)
    assert len(result) == 1
    # Both readings counted (the 23:59:59.743 one would be dropped by a <= T23:59:59Z bound).
    assert result[0]["sample_count"] == 2


# ── today not yet in daily table (rollup not yet run) ─────────────────────────

def test_history_range_live_today_absent_from_sealed_table(tmp_path):
    """Contrast: sealed table has no row for today (rollup hasn't run yet),
    but history_range_live provides it live from raw."""
    store = _store(tmp_path)
    inv = "inv-1"
    now_iso = "2026-06-26T11:00:00Z"

    store._append_sync({"inverterId": inv, "timestamp": "2026-06-26T09:00:00Z",
                        "pvPower": 800.0, "dailyPvEnergy": 1.0})

    # Sealed table has no row for 2026-06-26 — confirm the gap
    sealed = store._history_range_sync(inv, "2026-06-26", "2026-06-26")
    assert sealed == []

    # Live path fills the gap
    live = store._history_range_live_sync(inv, "2026-06-26", "2026-06-26",
                                          now_iso=now_iso)
    assert len(live) == 1
    assert live[0]["day"] == "2026-06-26"
    assert live[0]["sample_count"] == 1
