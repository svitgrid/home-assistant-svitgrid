"""Tests that every day-scoped store query buckets to the HOUSEHOLD-LOCAL day.

Regression suite for the Kyiv (UTC+3) Day-chart bug: the panel passes a local
calendar date, the store treated it as a UTC window, so the chart showed
local 03:00..03:00-next-day and plotted the solar curve three hours early.

The store stays UTC internally (``ts``, ``hour_start`` are absolute instants).
Only the *windowing* and the *daily* bucket key become local. Every method
keeps a ``tz_name`` default of UTC, so a UTC household -- and every existing
test -- sees byte-identical behaviour.
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


def _seed(store, readings):
    for r in readings:
        store._append_sync(r)


# --------------------------------------------------------------------- #
# hourly_range_live -- the reported bug
# --------------------------------------------------------------------- #


def test_hourly_range_live_includes_local_early_morning(tmp_path):
    """01:00 LOCAL Jul 15 in Kyiv is 22:00Z Jul 14. Under UTC windowing that
    reading fell outside the Jul 15 chart entirely -- the missing first three
    hours of every Kyiv Day chart."""
    store = _store(tmp_path)
    _seed(store, [_reading("2026-07-14T22:00:00Z", 120.0)])

    rows = store._hourly_range_live_sync("inv-1", "2026-07-15", KYIV)

    assert [r["hour"] for r in rows] == ["2026-07-14T22:00:00Z"]


def test_hourly_range_live_excludes_next_local_day(tmp_path):
    """22:00Z Jul 15 is 01:00 LOCAL Jul 16. Under UTC windowing it was folded
    onto the right edge of the Jul 15 chart as if it were that evening."""
    store = _store(tmp_path)
    _seed(store, [_reading("2026-07-15T22:00:00Z", 130.0)])

    rows = store._hourly_range_live_sync("inv-1", "2026-07-15", KYIV)

    assert rows == []


def test_hourly_range_live_defaults_to_utc_window(tmp_path):
    """No tz argument == the pre-fix UTC behaviour, byte for byte."""
    store = _store(tmp_path)
    _seed(store, [_reading("2026-07-14T22:00:00Z", 120.0)])

    assert store._hourly_range_live_sync("inv-1", "2026-07-15") == []
    rows = store._hourly_range_live_sync("inv-1", "2026-07-14")
    assert [r["hour"] for r in rows] == ["2026-07-14T22:00:00Z"]


def test_hourly_range_live_covers_the_whole_local_day(tmp_path):
    """Local 00:00 and local 23:00 both land in the same local-day chart."""
    store = _store(tmp_path)
    _seed(
        store,
        [
            _reading("2026-07-14T21:00:00Z", 10.0),  # local Jul 15 00:00
            _reading("2026-07-15T05:00:00Z", 1900.0),  # local Jul 15 08:00
            _reading("2026-07-15T20:59:00Z", 20.0),  # local Jul 15 23:59
        ],
    )

    rows = store._hourly_range_live_sync("inv-1", "2026-07-15", KYIV)

    assert [r["hour"] for r in rows] == [
        "2026-07-14T21:00:00Z",
        "2026-07-15T05:00:00Z",
        "2026-07-15T20:00:00Z",
    ]


# --------------------------------------------------------------------- #
# five_min_range_live -- the twin path
# --------------------------------------------------------------------- #


def test_five_min_range_live_uses_the_local_day_window(tmp_path):
    store = _store(tmp_path)
    _seed(
        store,
        [
            _reading("2026-07-14T22:07:00Z", 120.0),  # local Jul 15 01:07
            _reading("2026-07-15T22:07:00Z", 130.0),  # local Jul 16 01:07
        ],
    )

    rows = store._five_min_range_live_sync("inv-1", "2026-07-15", KYIV)

    assert [r["hour"] for r in rows] == ["2026-07-14T22:05:00Z"]


# --------------------------------------------------------------------- #
# hourly_range (sealed) -- same window over readings_hourly
# --------------------------------------------------------------------- #


def test_hourly_range_sealed_uses_the_local_day_window(tmp_path):
    store = _store(tmp_path)
    _seed(
        store,
        [
            _reading("2026-07-14T22:00:00Z", 120.0),  # local Jul 15 01:00
            _reading("2026-07-15T22:00:00Z", 130.0),  # local Jul 16 01:00
        ],
    )
    # Seal everything: "now" is well past both hours.
    store._rollup_sync("2026-07-17T00:00:00Z", KYIV)

    rows = store._hourly_range_sync("inv-1", "2026-07-15", KYIV)

    assert [r["hour"] for r in rows] == ["2026-07-14T22:00:00Z"]


# --------------------------------------------------------------------- #
# rollup -- readings_daily keyed by LOCAL day
# --------------------------------------------------------------------- #


def test_rollup_buckets_daily_rows_by_local_day(tmp_path):
    """Two readings an hour apart across local midnight must land in
    DIFFERENT local days, even though they share a UTC day."""
    store = _store(tmp_path)
    _seed(
        store,
        [
            _reading("2026-07-15T20:00:00Z", 100.0),  # local Jul 15 23:00
            _reading("2026-07-15T21:00:00Z", 200.0),  # local Jul 16 00:00
        ],
    )

    store._rollup_sync("2026-07-17T00:00:00Z", KYIV)
    days = store._history_range_sync("inv-1", "2026-07-01", "2026-07-31")

    assert [d["day"] for d in days] == ["2026-07-15", "2026-07-16"]


def test_rollup_defaults_to_utc_day_bucketing(tmp_path):
    store = _store(tmp_path)
    _seed(
        store,
        [
            _reading("2026-07-15T20:00:00Z", 100.0),
            _reading("2026-07-15T21:00:00Z", 200.0),
        ],
    )

    store._rollup_sync("2026-07-17T00:00:00Z")
    days = store._history_range_sync("inv-1", "2026-07-01", "2026-07-31")

    assert [d["day"] for d in days] == ["2026-07-15"]


def test_rollup_does_not_seal_the_current_local_day(tmp_path):
    """The in-progress LOCAL day must stay unsealed -- sealing it early would
    freeze a partial day into readings_daily. At 2026-07-15T21:30Z it is
    already Jul 16 locally in Kyiv, so Jul 15 is complete and Jul 16 is not."""
    store = _store(tmp_path)
    _seed(
        store,
        [
            _reading("2026-07-15T18:00:00Z", 100.0),  # local Jul 15 21:00
            _reading("2026-07-15T21:00:00Z", 200.0),  # local Jul 16 00:00
        ],
    )

    store._rollup_sync("2026-07-16T05:00:00Z", KYIV)  # local Jul 16 08:00
    days = store._history_range_sync("inv-1", "2026-07-01", "2026-07-31")

    assert [d["day"] for d in days] == ["2026-07-15"]


# --------------------------------------------------------------------- #
# today_summary -- raw fallback window
# --------------------------------------------------------------------- #


def test_today_summary_raw_fallback_uses_local_window(tmp_path):
    """With no sealed daily row, today_summary aggregates raw -- and must use
    the local day, or a Kyiv household's "today" starts at 03:00."""
    store = _store(tmp_path)
    _seed(
        store,
        [
            _reading("2026-07-14T22:00:00Z", 100.0),  # local Jul 15 01:00
            _reading("2026-07-15T05:00:00Z", 300.0),  # local Jul 15 08:00
        ],
    )

    rows = store._today_summary_sync("2026-07-15", KYIV)

    assert len(rows) == 1
    assert rows[0]["sample_count"] == 2


# --------------------------------------------------------------------- #
# history_range_live -- live "today" bucket
# --------------------------------------------------------------------- #


def test_history_range_live_today_bucket_is_the_local_day(tmp_path):
    """At 2026-07-15T05:00Z it is local Jul 15 08:00 in Kyiv. The live today
    bucket must be keyed 2026-07-15 and include the 22:00Z-yesterday reading
    that is local Jul 15 01:00."""
    store = _store(tmp_path)
    _seed(
        store,
        [
            _reading("2026-07-14T22:00:00Z", 100.0),  # local Jul 15 01:00
            _reading("2026-07-15T05:00:00Z", 300.0),  # local Jul 15 08:00
        ],
    )

    rows = store._history_range_live_sync(
        "inv-1", "2026-07-01", "2026-07-31", "2026-07-15T05:00:00Z", KYIV
    )

    today = [r for r in rows if r["day"] == "2026-07-15"]
    assert len(today) == 1
    assert today[0]["sample_count"] == 2
