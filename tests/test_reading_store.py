from custom_components.svitgrid.reading_store import ReadingStore


def _store(tmp_path):
    # hass is unused by the sync core; pass None.
    return ReadingStore(None, str(tmp_path / "readings.db"))


def test_append_then_recent_roundtrips_payload(tmp_path):
    store = _store(tmp_path)
    reading = {
        "inverterId": "inv-1",
        "timestamp": "2026-06-24T10:00:00Z",
        "source": "edge",
        "batterySoc": 85.0,
        "gridPower": 1200.0,
    }
    store._append_sync(reading)

    rows = store._recent_sync("inv-1", limit=10)
    assert len(rows) == 1
    assert rows[0]["payload"] == reading
    assert rows[0]["sync_state"] == "pending"
    assert rows[0]["ts"] == "2026-06-24T10:00:00Z"


def test_append_is_idempotent_on_same_ts(tmp_path):
    store = _store(tmp_path)
    r = {"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z", "batterySoc": 1.0}
    store._append_sync(r)
    store._append_sync({**r, "batterySoc": 2.0})  # same (inverter, ts) → replace
    rows = store._recent_sync("inv-1", limit=10)
    assert len(rows) == 1
    assert rows[0]["payload"]["batterySoc"] == 2.0


def test_count_by_state_groups_pending(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:05Z"})
    assert store._count_by_state_sync() == {"pending": 2}


def _seed(store, inverter_id, *timestamps, state="pending"):
    for ts in timestamps:
        store._append_sync({"inverterId": inverter_id, "timestamp": ts, "batterySoc": 1.0})
    if state != "pending":
        store._mark_failed_sync([(inverter_id, ts) for ts in timestamps], "2026-06-24T12:00:00Z")
        if state == "failed":
            return


def test_get_sendable_returns_oldest_first_within_cap(tmp_path):
    store = _store(tmp_path)
    # cap = 48h relative to now below
    now = "2026-06-24T12:00:00Z"
    _seed(store, "inv-1",
          "2026-06-20T12:00:00Z",   # > 48h old → excluded
          "2026-06-24T09:00:00Z",
          "2026-06-24T10:00:00Z")
    rows = store._get_sendable_sync(now, cap_s=48 * 3600, limit=50)
    assert [r["ts"] for r in rows] == ["2026-06-24T09:00:00Z", "2026-06-24T10:00:00Z"]


def test_mark_sent_excludes_from_sendable(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    _seed(store, "inv-1", "2026-06-24T10:00:00Z")
    store._mark_sent_sync([("inv-1", "2026-06-24T10:00:00Z")])
    assert store._get_sendable_sync(now, 48 * 3600, 50) == []
    assert store._count_by_state_sync() == {"sent": 1}


def test_mark_failed_increments_attempts_and_stays_sendable(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    _seed(store, "inv-1", "2026-06-24T10:00:00Z")
    store._mark_failed_sync([("inv-1", "2026-06-24T10:00:00Z")], now)
    rows = store._get_sendable_sync(now, 48 * 3600, 50)
    assert len(rows) == 1 and rows[0]["attempts"] == 1


def test_skip_aged_moves_old_unsent_to_skipped(tmp_path):
    store = _store(tmp_path)
    now = "2026-06-24T12:00:00Z"
    _seed(store, "inv-1", "2026-06-20T12:00:00Z", "2026-06-24T10:00:00Z")
    skipped = store._skip_aged_sync(now, cap_s=48 * 3600)
    assert skipped == 1
    assert store._count_by_state_sync() == {"pending": 1, "skipped": 1}


def test_live_snapshot_returns_latest_per_inverter(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z", "pvPower": 1.0})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:05Z", "pvPower": 2.0})
    store._append_sync({"inverterId": "inv-2", "timestamp": "2026-06-24T10:00:01Z", "pvPower": 9.0})
    snap = {s["inverterId"]: s for s in store._live_snapshot_sync()}
    assert snap["inv-1"]["payload"]["pvPower"] == 2.0
    assert snap["inv-2"]["payload"]["pvPower"] == 9.0


def test_live_snapshot_reports_interval(tmp_path):
    """3 rows at 300s apart → median gap 300.0, latest ts is the newest."""
    store = _store(tmp_path)
    for ts in ("2026-06-25T10:00:00Z", "2026-06-25T10:05:00Z", "2026-06-25T10:10:00Z"):
        store._append_sync({"inverterId": "inv-1", "timestamp": ts, "pvPower": 1.0})
    snap = {s["inverterId"]: s for s in store._live_snapshot_sync()}
    entry = snap["inv-1"]
    assert entry["ts"] == "2026-06-25T10:10:00Z"
    assert entry["intervalS"] == 300.0


def test_live_snapshot_interval_none_single_reading(tmp_path):
    """Single row → intervalS is None (not enough data for a gap)."""
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-2", "timestamp": "2026-06-25T10:00:00Z", "pvPower": 5.0})
    snap = {s["inverterId"]: s for s in store._live_snapshot_sync()}
    assert snap["inv-2"]["intervalS"] is None


def test_median_gap_seconds_multiple(tmp_path):
    """Pure helper: 3 ts 300s apart → 300.0; single ts → None."""
    from custom_components.svitgrid.reading_store import _median_gap_seconds
    assert _median_gap_seconds([
        "2026-06-25T10:10:00Z",
        "2026-06-25T10:05:00Z",
        "2026-06-25T10:00:00Z",
    ]) == 300.0
    assert _median_gap_seconds(["2026-06-25T10:00:00Z"]) is None


def test_sync_status_counts_and_last_sent(tmp_path):
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:00Z"})
    store._append_sync({"inverterId": "inv-1", "timestamp": "2026-06-24T10:00:05Z"})
    store._mark_sent_sync([("inv-1", "2026-06-24T10:00:00Z")])
    status = store._sync_status_sync()
    assert status["counts"] == {"sent": 1, "pending": 1}
    assert status["last_sent_ts"] == "2026-06-24T10:00:00Z"


def test_today_summary_returns_daily_row_when_present(tmp_path):
    store = _store(tmp_path)
    import json
    conn = store._connect_for_test()
    try:
        conn.execute(
            "INSERT INTO readings_daily (inverter_id, day, sample_count, avgs, peaks, energy) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "inv-1",
                "2026-06-24",
                42,
                json.dumps({}),
                json.dumps({"pvPower": 3000.0}),
                json.dumps({"dailyPvEnergy": 8.0}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = store._today_summary_sync("2026-06-24")
    assert len(result) == 1
    row = result[0]
    assert row["inverterId"] == "inv-1"
    assert row["sample_count"] == 42
    assert row["peaks"]["pvPower"] == 3000.0
    assert row["energy"]["dailyPvEnergy"] == 8.0


def test_today_summary_falls_back_to_raw_aggregate(tmp_path):
    store = _store(tmp_path)
    # No daily row — only raw readings (including one at 23:59:59 to verify Fix 1).
    store._append_sync({
        "inverterId": "inv-1",
        "timestamp": "2026-06-24T10:00:00Z",
        "pvPower": 1000.0,
        "dailyPvEnergy": 5.0,
    })
    store._append_sync({
        "inverterId": "inv-1",
        "timestamp": "2026-06-24T23:59:59Z",
        "pvPower": 3000.0,
        "dailyPvEnergy": 8.0,
    })

    result = store._today_summary_sync("2026-06-24")
    assert len(result) == 1
    row = result[0]
    assert row["inverterId"] == "inv-1"
    assert row["sample_count"] == 2
    assert row["peaks"]["pvPower"] == 3000.0
    assert row["energy"]["dailyPvEnergy"] == 8.0


def test_lifecycle_meta_roundtrip(tmp_path):
    store = _store(tmp_path)
    store._set_lifecycle_sync("deprovisioned", "revoked", "2026-06-25T10:00:00Z")
    assert store._get_lifecycle_sync() == {
        "state": "deprovisioned", "reason": "revoked", "since": "2026-06-25T10:00:00Z"}


def test_lifecycle_defaults_active_when_unset(tmp_path):
    store = _store(tmp_path)
    assert store._get_lifecycle_sync() == {"state": "active", "reason": None, "since": None}


def test_prune_inverters_not_in_keeps_listed_and_deletes_rest(tmp_path):
    """Rows for inverters NOT in keep_ids are deleted; rows for listed ids remain."""
    store = _store(tmp_path)
    # seed 2 rows for inv-A and 3 rows for inv-B
    for i in range(2):
        store._append_sync({"inverterId": "inv-A", "timestamp": f"2026-06-24T10:0{i}:00Z"})
    for i in range(3):
        store._append_sync({"inverterId": "inv-B", "timestamp": f"2026-06-24T11:0{i}:00Z"})

    deleted = store._prune_inverters_not_in_sync({"inv-A"})

    assert deleted == 3  # all inv-B rows removed
    # only inv-A rows remain
    conn = store._connect_for_test()
    try:
        rows = conn.execute("SELECT inverter_id FROM readings_raw ORDER BY inverter_id, ts").fetchall()
    finally:
        conn.close()
    assert all(r["inverter_id"] == "inv-A" for r in rows)
    assert len(rows) == 2


def test_prune_inverters_not_in_empty_set_deletes_all(tmp_path):
    """Empty keep set (nothing active) deletes ALL rows from readings_raw.
    readings_hourly/readings_daily are intentionally untouched (local archive)."""
    store = _store(tmp_path)
    store._append_sync({"inverterId": "inv-A", "timestamp": "2026-06-24T10:00:00Z"})
    store._append_sync({"inverterId": "inv-B", "timestamp": "2026-06-24T10:00:01Z"})

    deleted = store._prune_inverters_not_in_sync(set())

    assert deleted == 2
    conn = store._connect_for_test()
    try:
        count = conn.execute("SELECT COUNT(*) c FROM readings_raw").fetchone()["c"]
    finally:
        conn.close()
    assert count == 0


def test_history_range_orders_and_bounds(tmp_path):
    store = _store(tmp_path)
    import json
    conn = store._connect_for_test()
    try:
        rows_to_insert = [
            ("inv-1", "2026-06-22"),
            ("inv-1", "2026-06-23"),
            ("inv-1", "2026-06-24"),
            ("inv-1", "2026-06-20"),   # out of range
            ("inv-2", "2026-06-23"),   # different inverter
        ]
        for inv_id, day in rows_to_insert:
            conn.execute(
                "INSERT INTO readings_daily (inverter_id, day, sample_count, avgs, peaks, energy) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (inv_id, day, 10, json.dumps({}), json.dumps({}), json.dumps({})),
            )
        conn.commit()
    finally:
        conn.close()

    result = store._history_range_sync("inv-1", "2026-06-22", "2026-06-24")
    assert len(result) == 3
    days_returned = [r["day"] for r in result]
    assert days_returned == ["2026-06-22", "2026-06-23", "2026-06-24"]
    # Out-of-range day and other inverter must not appear.
    assert "2026-06-20" not in days_returned


def test_median_gap_seconds_malformed_timestamps_returns_none():
    """Malformed timestamps must not raise; result is None (treat as unknown)."""
    from custom_components.svitgrid.reading_store import _median_gap_seconds

    # All malformed → fewer than 2 parseable entries → None, no exception.
    assert _median_gap_seconds(["not-a-timestamp", "also-bad"]) is None

    # One valid + one malformed → still fewer than 2 parseable → None.
    assert _median_gap_seconds(["2026-06-25T10:00:00Z", "bad"]) is None

    # Two valid despite a malformed entry mixed in → result is a number.
    result = _median_gap_seconds([
        "2026-06-25T10:10:00Z",
        "BAD-ENTRY",
        "2026-06-25T10:00:00Z",
    ])
    assert result == 600.0
