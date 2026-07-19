"""Tests for the one-time readings_daily rebuild into local-day buckets.

Existing installs have a ``readings_daily`` table keyed by UTC date. Once the
rollup starts keying by household-local date the two schemes coexist in one
table, so the Month/Year bars would mix ~3h-misattributed old rows with
correct new ones. ``rebuild_daily_local`` re-derives every day it still can
from ``readings_hourly`` (retained ~2 years) under the new local keying.

Days older than the hourly retention cannot be re-derived and are deliberately
LEFT ALONE -- a slightly misattributed old bar beats a deleted one.
"""

from custom_components.svitgrid.reading_store import ReadingStore

KYIV = "Europe/Kyiv"  # UTC+3 in July


def _store(tmp_path):
    return ReadingStore(None, str(tmp_path / "readings.db"))


def _reading(ts: str, pv: float, inverter_id: str = "inv-1") -> dict:
    return {
        "inverterId": inverter_id,
        "timestamp": ts,
        "pvPower": pv,
        "batterySoc": 50.0,
        "dailyPvEnergy": 1.0,
    }


def _days(store, inverter_id="inv-1"):
    return [
        d["day"] for d in store._history_range_sync(inverter_id, "2000-01-01", "2099-12-31")
    ]


def _seed_utc_bucketed(store, readings, now_iso="2026-07-20T00:00:00Z"):
    """Seed raw + seal it the OLD (UTC) way, as an existing install would have."""
    for r in readings:
        store._append_sync(r)
    store._rollup_sync(now_iso)  # no tz == UTC bucketing


def _hourly_span(start_iso: str, hours: int, inverter_id: str = "inv-1"):
    """One reading per UTC hour, so local days inside the span are FULLY
    covered by readings_hourly and are therefore rebuildable."""
    from datetime import datetime, timedelta

    t0 = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    return [
        _reading(
            (t0 + timedelta(hours=i)).strftime("%Y-%m-%dT%H:00:00Z"),
            100.0 + i,
            inverter_id,
        )
        for i in range(hours)
    ]


def test_rebuild_rekeys_fully_covered_days_to_local_boundaries(tmp_path):
    """A fully covered local day must be re-sealed on LOCAL midnights.

    Kyiv local Jul 16 runs 2026-07-15T21:00Z .. 2026-07-16T21:00Z. Sealed the
    old UTC way those 24 hours were split across two UTC days; after the
    rebuild they form one local Jul 16 bar of exactly 24 samples.
    """
    store = _store(tmp_path)
    # 21:00Z Jul 15 through 20:00Z Jul 17: local Jul 16 is fully covered.
    _seed_utc_bucketed(store, _hourly_span("2026-07-15T21:00:00Z", 48))

    store._rebuild_daily_local_sync(KYIV, "2026-07-20T00:00:00Z")

    assert "2026-07-16" in _days(store)
    conn = store._connect_for_test()
    row = conn.execute(
        "SELECT sample_count FROM readings_daily WHERE day = '2026-07-16'"
    ).fetchone()
    conn.close()
    assert row["sample_count"] == 24


def test_rebuild_is_idempotent(tmp_path):
    """Running twice must not change the result or duplicate rows -- the meta
    marker makes the second call a no-op."""
    store = _store(tmp_path)
    _seed_utc_bucketed(
        store,
        [
            _reading("2026-07-15T10:00:00Z", 100.0),
            _reading("2026-07-15T21:00:00Z", 200.0),
        ],
    )

    first = store._rebuild_daily_local_sync(KYIV, "2026-07-20T00:00:00Z")
    after_first = _days(store)
    second = store._rebuild_daily_local_sync(KYIV, "2026-07-20T00:00:00Z")

    assert first["rebuilt"] > 0
    assert second["rebuilt"] == 0  # skipped: already at this tz
    assert _days(store) == after_first


def test_rebuild_reruns_when_the_household_timezone_changes(tmp_path):
    """The marker records WHICH tz the table is keyed to, so a user moving
    their HA timezone re-keys rather than silently keeping stale buckets."""
    store = _store(tmp_path)
    _seed_utc_bucketed(store, _hourly_span("2026-07-15T21:00:00Z", 48))

    def _samples(day):
        conn = store._connect_for_test()
        row = conn.execute(
            "SELECT sample_count FROM readings_daily WHERE day = ?", (day,)
        ).fetchone()
        conn.close()
        return row["sample_count"] if row else None

    store._rebuild_daily_local_sync(KYIV, "2026-07-20T00:00:00Z")
    assert _samples("2026-07-16") == 24  # local-day boundaries

    store._rebuild_daily_local_sync("UTC", "2026-07-20T00:00:00Z")
    assert _samples("2026-07-16") == 24  # UTC-day boundaries, same count here
    assert _samples("2026-07-17") == 21  # ...but the tail day differs


def test_rebuild_leaves_days_older_than_hourly_coverage_untouched(tmp_path):
    """A daily row with no surviving hourly rows to re-derive from is kept
    as-is rather than deleted."""
    store = _store(tmp_path)
    _seed_utc_bucketed(store, [_reading("2026-07-15T21:00:00Z", 200.0)])
    # An ancient daily row whose hourly rows have long since been pruned.
    conn = store._connect_for_test()
    conn.execute(
        "INSERT INTO readings_daily (inverter_id, day, sample_count, avgs, peaks, energy) "
        "VALUES ('inv-1', '2024-01-01', 5, '{}', '{}', '{}')"
    )
    conn.commit()
    conn.close()

    store._rebuild_daily_local_sync(KYIV, "2026-07-20T00:00:00Z")

    assert "2024-01-01" in _days(store)


def test_rebuild_does_not_seal_the_current_local_day(tmp_path):
    """The in-progress local day must stay out of readings_daily, or the
    rebuild would freeze a partial day the rollup then never revisits."""
    store = _store(tmp_path)
    for r in [
        _reading("2026-07-15T10:00:00Z", 100.0),
        _reading("2026-07-16T10:00:00Z", 150.0),  # local Jul 16 13:00 == "today"
    ]:
        store._append_sync(r)
    store._rollup_sync("2026-07-16T12:00:00Z")

    store._rebuild_daily_local_sync(KYIV, "2026-07-16T12:00:00Z")  # local Jul 16 15:00

    assert "2026-07-16" not in _days(store)


def test_rebuild_keeps_inverters_separate(tmp_path):
    store = _store(tmp_path)
    _seed_utc_bucketed(
        store,
        _hourly_span("2026-07-15T21:00:00Z", 48, "inv-1")
        + _hourly_span("2026-07-15T21:00:00Z", 48, "inv-2"),
    )

    store._rebuild_daily_local_sync(KYIV, "2026-07-20T00:00:00Z")

    conn = store._connect_for_test()
    rows = conn.execute(
        "SELECT inverter_id, sample_count FROM readings_daily "
        "WHERE day = '2026-07-16' ORDER BY inverter_id"
    ).fetchall()
    conn.close()
    assert [(r["inverter_id"], r["sample_count"]) for r in rows] == [
        ("inv-1", 24),
        ("inv-2", 24),
    ]


def test_rebuild_never_deletes_a_day_it_cannot_rederive(tmp_path):
    """The delete floor must be the first FULLY covered local day.

    readings_daily is never pruned but readings_hourly is (~2 years), so on an
    old install the earliest surviving hourly row sits mid-day and there are
    older, complete daily rows beneath it. Clearing down to the *partially*
    covered day wipes a full bar and re-inserts nothing -- and since the rollup
    only ever INSERTs OR REPLACEs completed days, the loss is permanent.
    """
    store = _store(tmp_path)
    # Earliest surviving hourly row is local Jul 16 01:00 -- so local Jul 15 is
    # only partially covered and must NOT be cleared.
    for r in [
        _reading("2026-07-15T22:00:00Z", 100.0),
        _reading("2026-07-16T10:00:00Z", 150.0),
    ]:
        store._append_sync(r)
    store._rollup_sync("2026-07-20T00:00:00Z")
    conn = store._connect_for_test()
    conn.execute(
        "INSERT OR REPLACE INTO readings_daily "
        "(inverter_id, day, sample_count, avgs, peaks, energy) "
        "VALUES ('inv-1', '2026-07-15', 288, '{}', '{}', '{}')"
    )
    conn.commit()
    conn.close()

    store._rebuild_daily_local_sync(KYIV, "2026-07-20T00:00:00Z")

    assert "2026-07-15" in _days(store)
    conn = store._connect_for_test()
    kept = conn.execute(
        "SELECT sample_count FROM readings_daily WHERE day = '2026-07-15'"
    ).fetchone()["sample_count"]
    conn.close()
    assert kept == 288  # untouched, not replaced by a partial sliver


def test_rebuild_does_not_truncate_a_partial_day_in_a_negative_offset_tz(tmp_path):
    """Mirror image of the above for a US household, where the local day runs
    BEHIND the UTC day: the partially covered boundary day must keep its
    complete pre-existing bar rather than be replaced by a 1-sample sliver."""
    store = _store(tmp_path)
    for r in [
        _reading("2026-07-16T02:00:00Z", 100.0),  # local Jul 15 22:00 in New York
        _reading("2026-07-16T18:00:00Z", 150.0),  # local Jul 16 14:00
    ]:
        store._append_sync(r)
    store._rollup_sync("2026-07-20T00:00:00Z")
    conn = store._connect_for_test()
    conn.execute(
        "INSERT OR REPLACE INTO readings_daily "
        "(inverter_id, day, sample_count, avgs, peaks, energy) "
        "VALUES ('inv-1', '2026-07-15', 288, '{}', '{}', '{}')"
    )
    conn.commit()
    conn.close()

    store._rebuild_daily_local_sync("America/New_York", "2026-07-20T00:00:00Z")

    conn = store._connect_for_test()
    kept = conn.execute(
        "SELECT sample_count FROM readings_daily WHERE day = '2026-07-15'"
    ).fetchone()["sample_count"]
    conn.close()
    assert kept == 288


def test_rebuild_seam_double_counts_the_boundary_hours(tmp_path):
    """CHARACTERIZATION: the seam row below the rebuilt span is kept, and its
    last `offset` hours are counted twice -- once there, once in the first
    rebuilt local day.

    This is the deliberate trade-off documented on _rebuild_daily_local_sync,
    not a regression. Deleting that row would take its non-overlapping portion
    with it, and nothing on disk distinguishes the two parts; an earlier
    attempt to clear it deleted complete, unrecoverable bars (see
    test_rebuild_never_deletes_a_day_it_cannot_rederive).

    Pinned here so the cost stays visible and bounded: exactly `offset` hours,
    in exactly one row, at the bottom of history. If a future change shrinks
    or removes it, this test should be updated -- if it GROWS, that is a bug.
    """
    store = _store(tmp_path)
    # Hourly coverage starts exactly on a local midnight (Kyiv local Jul 10
    # 00:00 == 2026-07-09T21:00Z) and runs 8 full local days.
    _seed_utc_bucketed(store, _hourly_span("2026-07-09T21:00:00Z", 24 * 8 + 3))

    before = sum(
        d["sample_count"] for d in store._history_range_sync("inv-1", "2000-01-01", "2099-12-31")
    )
    store._rebuild_daily_local_sync(KYIV, "2026-07-20T00:00:00Z")
    after = sum(
        d["sample_count"] for d in store._history_range_sync("inv-1", "2000-01-01", "2099-12-31")
    )

    # Kyiv is UTC+3 in July: exactly 3 hours are double-counted, no more.
    assert after - before == 3

    days = _days(store)
    # The seam row is the stale UTC-keyed day just below the rebuilt span, and
    # it holds exactly those 3 hours.
    conn = store._connect_for_test()
    seam = conn.execute(
        "SELECT sample_count FROM readings_daily WHERE day = '2026-07-09'"
    ).fetchone()
    conn.close()
    assert seam["sample_count"] == 3
    # Everything above the seam is a clean, fully rebuilt local day.
    assert "2026-07-10" in days
