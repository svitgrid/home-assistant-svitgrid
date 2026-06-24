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
