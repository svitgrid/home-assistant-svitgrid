"""Durable local SQLite store for produced readings (Sub-project 1)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant

from .local_time import local_day_of, local_day_window

# Tracks db_paths whose schema has already been created in this process so that
# _create_schema (DDL) is not executed on every connection open.
_INITIALIZED: set[str] = set()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    # PRAGMAs are connection-scoped; always apply them.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if db_path not in _INITIALIZED:
        _create_schema(conn)
        _INITIALIZED.add(db_path)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS readings_raw (
            inverter_id     TEXT NOT NULL,
            ts              TEXT NOT NULL,
            payload         TEXT NOT NULL,
            sync_state      TEXT NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            PRIMARY KEY (inverter_id, ts)
        );
        CREATE INDEX IF NOT EXISTS idx_raw_sync ON readings_raw (sync_state, ts);
        CREATE INDEX IF NOT EXISTS idx_raw_inv_ts ON readings_raw (inverter_id, ts);

        CREATE TABLE IF NOT EXISTS readings_hourly (
            inverter_id  TEXT NOT NULL,
            hour_start   TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            avgs         TEXT NOT NULL,
            peaks        TEXT NOT NULL,
            energy       TEXT NOT NULL,
            PRIMARY KEY (inverter_id, hour_start)
        );

        CREATE TABLE IF NOT EXISTS readings_daily (
            inverter_id  TEXT NOT NULL,
            day          TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            avgs         TEXT NOT NULL,
            peaks        TEXT NOT NULL,
            energy       TEXT NOT NULL,
            PRIMARY KEY (inverter_id, day)
        );

        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.commit()


def _median_gap_seconds(ts_list: list[str]) -> float | None:
    """Return the median gap in seconds between consecutive timestamps.

    ts_list must be sorted descending (newest first) — the same order returned
    by ``ORDER BY ts DESC``.  Returns None when fewer than 2 entries are given.
    """
    if len(ts_list) < 2:
        return None
    # Parse each ISO-8601 UTC timestamp, skipping malformed entries.
    parsed = []
    for ts in ts_list:
        with contextlib.suppress(ValueError, AttributeError):
            parsed.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
    if len(parsed) < 2:
        return None
    # Gaps between consecutive entries (list is desc → older − newer yields positive).
    gaps = [abs((parsed[i] - parsed[i + 1]).total_seconds()) for i in range(len(parsed) - 1)]
    # Median: sort and pick middle element (or average of two middle for even N).
    gaps_sorted = sorted(gaps)
    n = len(gaps_sorted)
    mid = n // 2
    if n % 2 == 1:
        return float(gaps_sorted[mid])
    return float((gaps_sorted[mid - 1] + gaps_sorted[mid]) / 2)


class ReadingStore:
    """Tiered SQLite store. Sync `_*_sync` core; async wrappers offload to the
    HA executor so the event loop never blocks on disk I/O."""

    def __init__(self, hass: HomeAssistant | None, db_path: str) -> None:
        self._hass = hass
        self._db_path = db_path
        # Whether this entry uploads readings to the cloud. Default True so
        # non-island installs are unaffected; _start_local_store stamps it False
        # in pure island mode. Surfaced in sync_status() so the
        # panel can render "local only" instead of a false ⚠ pending-sync
        # warning — in island mode the cloud sender never runs, so readings
        # legitimately sit in sync_state='pending' forever.
        self.cloud_ingest_enabled: bool = True
        # Lazily created asyncio.Event — do NOT create at __init__ time because
        # that would bind to whatever loop happens to be current at construction,
        # which may differ from the running loop used by the sender.
        self._data_event: asyncio.Event | None = None

    def _ensure_event(self) -> asyncio.Event:
        """Return the data-available Event, creating it in the running loop on first use."""
        if self._data_event is None:
            self._data_event = asyncio.Event()
        return self._data_event

    def _signal_data_available(self) -> None:
        """Set the data-available event.  Must be called from the event-loop thread."""
        if self._data_event is not None:
            self._data_event.set()

    async def wait_for_data(self, wait_s: float) -> None:  # noqa: ASYNC109
        """Wait until a reading is appended or *wait_s* seconds elapse (never raises)."""
        event = self._ensure_event()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=wait_s)
        event.clear()

    # ── sync core (unit-tested directly) ──────────────────────────────
    def _append_sync(self, reading: dict[str, Any]) -> None:
        ts = reading["timestamp"]
        inverter_id = reading["inverterId"]
        conn = _connect(self._db_path)
        try:
            # INSERT OR REPLACE resets sync_state='pending' and attempts=0 on
            # collision. This is intentional: the publisher emits each
            # (inverter_id, ts) exactly once, so a replace means a fresh
            # capture — not a re-queue of partially-synced state.
            conn.execute(
                "INSERT OR REPLACE INTO readings_raw "
                "(inverter_id, ts, payload, sync_state, attempts) "
                "VALUES (?, ?, ?, 'pending', 0)",
                (inverter_id, ts, json.dumps(reading, separators=(",", ":"))),
            )
            conn.commit()
        finally:
            conn.close()

    def _recent_sync(self, inverter_id: str, limit: int) -> list[dict[str, Any]]:
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT inverter_id, ts, payload, sync_state, attempts "
                "FROM readings_raw WHERE inverter_id = ? ORDER BY ts DESC LIMIT ?",
                (inverter_id, limit),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def _count_by_state_sync(self) -> dict[str, int]:
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT sync_state, COUNT(*) c FROM readings_raw GROUP BY sync_state"
            )
            return {r["sync_state"]: r["c"] for r in cur.fetchall()}
        finally:
            conn.close()

    def _cap_boundary(self, now_iso: str, cap_s: int) -> str:
        from datetime import datetime, timedelta

        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return (now - timedelta(seconds=cap_s)).isoformat().replace("+00:00", "Z")

    def _get_sendable_sync(self, now_iso: str, cap_s: int, limit: int) -> list[dict]:
        floor = self._cap_boundary(now_iso, cap_s)
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT inverter_id, ts, payload, sync_state, attempts "
                "FROM readings_raw WHERE sync_state IN ('pending','failed') "
                "AND ts >= ? ORDER BY ts ASC LIMIT ?",
                (floor, limit),
            )
            return [_row_to_dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def _mark_sent_sync(self, keys: list[tuple[str, str]]) -> None:
        conn = _connect(self._db_path)
        try:
            conn.executemany(
                "UPDATE readings_raw SET sync_state='sent' WHERE inverter_id=? AND ts=?",
                keys,
            )
            conn.commit()
        finally:
            conn.close()

    def _mark_failed_sync(self, keys: list[tuple[str, str]], now_iso: str) -> None:
        conn = _connect(self._db_path)
        try:
            conn.executemany(
                "UPDATE readings_raw SET sync_state='failed', attempts=attempts+1, "
                "last_attempt_at=? WHERE inverter_id=? AND ts=?",
                [(now_iso, inv, ts) for (inv, ts) in keys],
            )
            conn.commit()
        finally:
            conn.close()

    def _skip_aged_sync(self, now_iso: str, cap_s: int) -> int:
        floor = self._cap_boundary(now_iso, cap_s)
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "UPDATE readings_raw SET sync_state='skipped' "
                "WHERE sync_state IN ('pending','failed') AND ts < ?",
                (floor,),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def _hour_of(self, ts: str) -> str:
        return ts[:13] + ":00:00Z"  # "2026-06-24T10:..." → "2026-06-24T10:00:00Z"

    def _day_of(self, ts: str, tz_name: str | None = None) -> str:
        """The calendar date owning *ts*.

        With *tz_name* this is the HOUSEHOLD-LOCAL date -- the bucket key for
        ``readings_daily`` and the boundary for "today". Without it, the plain
        UTC prefix (the pre-fix behaviour), which every UTC household and the
        existing test suite still exercise.
        """
        if not tz_name or tz_name == "UTC":
            return ts[:10]  # "2026-06-24"
        return local_day_of(ts, tz_name)

    def _five_min_of(self, ts: str) -> str:
        # Floor a UTC ISO timestamp to its 5-minute bucket start.
        # "2026-06-24T10:17:30Z" → "2026-06-24T10:15:00Z"
        return ts[:14] + f"{(int(ts[14:16]) // 5) * 5:02d}:00Z"

    def _rollup_sync(self, now_iso: str, tz_name: str | None = None) -> dict[str, int]:
        from . import rollup as _r

        cur_hour = self._hour_of(now_iso)
        # Days are HOUSEHOLD-LOCAL: readings_daily is what the Month/Year bars
        # read, and a user's "day" is their wall clock, not UTC. Hours stay UTC
        # (an hour_start is an absolute instant); only the day bucket is local.
        cur_day = self._day_of(now_iso, tz_name)
        conn = _connect(self._db_path)
        hours = days = 0
        try:
            # COMPLETED hours: group raw rows whose hour < current hour
            cur = conn.execute(
                "SELECT inverter_id, ts, payload FROM readings_raw ORDER BY inverter_id, ts"
            )
            buckets: dict[tuple[str, str], list[dict]] = {}
            for r in cur.fetchall():
                hour = self._hour_of(r["ts"])
                if hour >= cur_hour:
                    continue
                buckets.setdefault((r["inverter_id"], hour), []).append(
                    {"payload": json.loads(r["payload"])}
                )
            for (inv, hour), rows in buckets.items():
                agg = _r.aggregate(rows)
                conn.execute(
                    "INSERT OR REPLACE INTO readings_hourly "
                    "(inverter_id, hour_start, sample_count, avgs, peaks, energy) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        inv,
                        hour,
                        agg["sample_count"],
                        json.dumps(agg["avgs"]),
                        json.dumps(agg["peaks"]),
                        json.dumps(agg["energy"]),
                    ),
                )
                hours += 1
            # COMPLETED days: group hourly rows whose day < current day
            cur = conn.execute(
                "SELECT inverter_id, hour_start, sample_count, avgs, peaks, energy "
                "FROM readings_hourly"
            )
            dbuckets: dict[tuple[str, str], list[dict]] = {}
            for r in cur.fetchall():
                day = self._day_of(r["hour_start"], tz_name)
                if day >= cur_day:
                    continue
                dbuckets.setdefault((r["inverter_id"], day), []).append(
                    {
                        "sample_count": r["sample_count"],
                        "avgs": json.loads(r["avgs"]),
                        "peaks": json.loads(r["peaks"]),
                        "energy": json.loads(r["energy"]),
                    }
                )
            for (inv, day), hrows in dbuckets.items():
                agg = _r.merge_hourly(hrows)
                conn.execute(
                    "INSERT OR REPLACE INTO readings_daily "
                    "(inverter_id, day, sample_count, avgs, peaks, energy) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        inv,
                        day,
                        agg["sample_count"],
                        json.dumps(agg["avgs"]),
                        json.dumps(agg["peaks"]),
                        json.dumps(agg["energy"]),
                    ),
                )
                days += 1
            conn.commit()
            return {"hours": hours, "days": days}
        finally:
            conn.close()

    def _next_day(self, day: str) -> str:
        from datetime import timedelta

        return (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    def _rebuild_daily_local_sync(self, tz_name: str | None, now_iso: str) -> dict[str, int]:
        """Re-key ``readings_daily`` to household-local days, once per timezone.

        Existing installs sealed daily rows with a UTC date key. Now that the
        rollup keys by local date, the table would otherwise hold a mix of the
        two schemes and the Month/Year bars would show ~3h of energy
        misattributed across midnight for every pre-existing day.

        This re-derives every day it still can from ``readings_hourly``
        (retained ~2 years) under the local key. Days whose hourly rows have
        already been pruned CANNOT be re-derived and are left untouched: a
        slightly misattributed old bar is better than a missing one.

        The current local day is skipped -- sealing it here would freeze a
        partial day that ``_rollup_sync`` (which only seals completed days)
        would then never revisit.

        Idempotent via a ``meta`` marker holding the tz the table is keyed to,
        so it runs once per install and again only if the household changes
        its Home Assistant timezone.
        """
        from . import rollup as _r

        marker = tz_name or "UTC"
        if self._get_meta_sync("daily_tz_bucket") == marker:
            return {"rebuilt": 0}

        cur_day = self._day_of(now_iso, tz_name)
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT inverter_id, hour_start, sample_count, avgs, peaks, energy "
                "FROM readings_hourly"
            )
            dbuckets: dict[tuple[str, str], list[dict]] = {}
            earliest_hour: str | None = None
            for r in cur.fetchall():
                if earliest_hour is None or r["hour_start"] < earliest_hour:
                    earliest_hour = r["hour_start"]
                day = self._day_of(r["hour_start"], tz_name)
                if day >= cur_day:
                    continue
                dbuckets.setdefault((r["inverter_id"], day), []).append(
                    {
                        "sample_count": r["sample_count"],
                        "avgs": json.loads(r["avgs"]),
                        "peaks": json.loads(r["peaks"]),
                        "energy": json.loads(r["energy"]),
                    }
                )

            # Clear only the span the hourly table can FULLY re-derive, so
            # rows that would otherwise linger under their OLD key (e.g. a UTC
            # day that no longer exists locally) go away, while anything the
            # rebuild cannot reproduce survives untouched.
            #
            # The floor is the first local day the hourly table covers from its
            # very first hour. readings_daily is never pruned but
            # readings_hourly is (~2 years), so on an old install the earliest
            # surviving hourly row sits mid-day: that day can only be rebuilt
            # as a partial sliver, and any complete daily row below it cannot
            # be rebuilt at all. Clearing down to it would replace a full bar
            # with a sliver, or delete one outright -- and since _rollup_sync
            # only ever seals COMPLETED days, neither ever comes back.
            rebuilt = 0
            if dbuckets and earliest_hour is not None:
                boundary_day = self._day_of(earliest_hour, tz_name)
                boundary_start, _ = local_day_window(boundary_day, tz_name)
                floor_day = (
                    boundary_day
                    if earliest_hour <= boundary_start
                    else self._next_day(boundary_day)
                )
                dbuckets = {
                    key: rows for key, rows in dbuckets.items() if key[1] >= floor_day
                }
                if not dbuckets:
                    conn.commit()
                    self._set_meta_sync("daily_tz_bucket", marker)
                    return {"rebuilt": 0}
                conn.execute(
                    "DELETE FROM readings_daily WHERE day >= ? AND day < ?",
                    (floor_day, cur_day),
                )
                for (inv, day), hrows in dbuckets.items():
                    agg = _r.merge_hourly(hrows)
                    conn.execute(
                        "INSERT OR REPLACE INTO readings_daily "
                        "(inverter_id, day, sample_count, avgs, peaks, energy) "
                        "VALUES (?,?,?,?,?,?)",
                        (
                            inv,
                            day,
                            agg["sample_count"],
                            json.dumps(agg["avgs"]),
                            json.dumps(agg["peaks"]),
                            json.dumps(agg["energy"]),
                        ),
                    )
                    rebuilt += 1
            conn.commit()
        finally:
            conn.close()

        self._set_meta_sync("daily_tz_bucket", marker)
        return {"rebuilt": rebuilt}

    def _prune_sync(
        self, now_iso: str, raw_retention_s: int, hourly_retention_s: int
    ) -> dict[str, int]:
        raw_floor = self._cap_boundary(now_iso, raw_retention_s)
        hourly_floor = self._cap_boundary(now_iso, hourly_retention_s)
        conn = _connect(self._db_path)
        try:
            c1 = conn.execute("DELETE FROM readings_raw WHERE ts < ?", (raw_floor,))
            c2 = conn.execute("DELETE FROM readings_hourly WHERE hour_start < ?", (hourly_floor,))
            conn.commit()
            return {"raw": c1.rowcount, "hourly": c2.rowcount}
        finally:
            conn.close()

    def _live_snapshot_sync(self) -> list[dict]:
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT r.inverter_id, r.ts, r.payload FROM readings_raw r "
                "JOIN (SELECT inverter_id, MAX(ts) mts FROM readings_raw GROUP BY inverter_id) m "
                "ON r.inverter_id = m.inverter_id AND r.ts = m.mts"
            )
            rows = cur.fetchall()
            result = []
            for r in rows:
                inverter_id = r["inverter_id"]
                # Fetch up to 6 recent timestamps to compute the observed cadence.
                ts_cur = conn.execute(
                    "SELECT ts FROM readings_raw WHERE inverter_id = ? ORDER BY ts DESC LIMIT 6",
                    (inverter_id,),
                )
                ts_list = [row["ts"] for row in ts_cur.fetchall()]
                interval_s = _median_gap_seconds(ts_list)
                result.append(
                    {
                        "inverterId": inverter_id,
                        "ts": r["ts"],
                        "payload": json.loads(r["payload"]),
                        "intervalS": interval_s,
                    }
                )
            return result
        finally:
            conn.close()

    def _today_summary_sync(self, day: str, tz_name: str | None = None) -> list[dict]:
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT inverter_id, sample_count, peaks, energy FROM readings_daily WHERE day = ?",
                (day,),
            )
            rows = [
                {
                    "inverterId": r["inverter_id"],
                    "sample_count": r["sample_count"],
                    "peaks": json.loads(r["peaks"]),
                    "energy": json.loads(r["energy"]),
                }
                for r in cur.fetchall()
            ]
            if rows:
                return rows
            # Fallback: aggregate today's raw (daily row not rolled up yet).
            from . import rollup as _r

            day_start, next_day_start = local_day_window(day, tz_name)
            cur = conn.execute(
                "SELECT inverter_id, payload FROM readings_raw WHERE ts >= ? AND ts < ?",
                (day_start, next_day_start),
            )
            buckets: dict[str, list[dict]] = {}
            for r in cur.fetchall():
                buckets.setdefault(r["inverter_id"], []).append(
                    {"payload": json.loads(r["payload"])}
                )
            out = []
            for inv, rws in buckets.items():
                agg = _r.aggregate(rws)
                out.append(
                    {
                        "inverterId": inv,
                        "sample_count": agg["sample_count"],
                        "peaks": agg["peaks"],
                        "energy": agg["energy"],
                    }
                )
            return out
        finally:
            conn.close()

    def _hourly_range_sync(
        self, inverter_id: str, day: str, tz_name: str | None = None
    ) -> list[dict]:
        # *day* is a HOUSEHOLD-LOCAL calendar date; the window is the UTC span
        # of that local day (half-open, so it is 23/24/25h across DST).
        day_start, day_end = local_day_window(day, tz_name)
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT hour_start, sample_count, avgs, peaks, energy "
                "FROM readings_hourly "
                "WHERE inverter_id = ? AND hour_start >= ? AND hour_start < ? "
                "ORDER BY hour_start",
                (inverter_id, day_start, day_end),
            )
            return [
                {
                    "hour": r["hour_start"],
                    "sample_count": r["sample_count"],
                    "avgs": json.loads(r["avgs"]),
                    "peaks": json.loads(r["peaks"]),
                    "energy": json.loads(r["energy"]),
                }
                for r in cur.fetchall()
            ]
        finally:
            conn.close()

    def _hourly_range_live_sync(
        self, inverter_id: str, day: str, tz_name: str | None = None
    ) -> list[dict]:
        """Compute hourly buckets on demand from readings_raw for *day*.

        Unlike ``_hourly_range_sync`` (which reads the pre-sealed
        ``readings_hourly`` table populated only every ROLLUP_INTERVAL_S and
        only for COMPLETED hours), this groups the day's raw rows by hour and
        aggregates each with ``rollup.aggregate`` — INCLUDING the current
        in-progress hour.  A local HA box has ample compute for a day of raw
        (a few thousand rows), so a fresh household still gets a Day chart
        before the first rollup runs.

        Returns the SAME shape as ``_hourly_range_sync``:
        ``[{"hour", "sample_count", "avgs", "peaks", "energy"}]`` sorted by hour.
        """
        from . import rollup as _r

        # *day* is a HOUSEHOLD-LOCAL calendar date. The window is half-open on
        # the next LOCAL midnight: readings carry sub-second ts
        # (e.g. "...T23:59:59.743Z"), so an inclusive bound would drop the
        # day's last second.
        day_start, next_day_start = local_day_window(day, tz_name)
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT ts, payload FROM readings_raw "
                "WHERE inverter_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                (inverter_id, day_start, next_day_start),
            )
            # Mirror _rollup_sync's bucket construction so the numbers match.
            buckets: dict[str, list[dict]] = {}
            for r in cur.fetchall():
                hour = self._hour_of(r["ts"])
                buckets.setdefault(hour, []).append({"payload": json.loads(r["payload"])})
            result = []
            for hour in sorted(buckets):
                agg = _r.aggregate(buckets[hour])
                result.append(
                    {
                        "hour": hour,
                        "sample_count": agg["sample_count"],
                        "avgs": agg["avgs"],
                        "peaks": agg["peaks"],
                        "energy": agg["energy"],
                    }
                )
            return result
        finally:
            conn.close()

    def _five_min_range_live_sync(
        self, inverter_id: str, day: str, tz_name: str | None = None
    ) -> list[dict]:
        """Compute 5-minute buckets on demand from readings_raw for *day*.

        Same as ``_hourly_range_live_sync`` but at 5-minute resolution, so the
        island Day charts get ~288 fine-grained points/day (matching the
        cloud's 5-minute buckets) instead of 24 coarse hourly ones — including
        the current in-progress 5-minute bucket. readings_raw is retained 14
        days, so this covers any Day view within that window; older days have
        only hourly aggregates and use the hourly path.

        Returns the SAME shape/keys as ``_hourly_range_live_sync``
        (``[{"hour", "sample_count", "avgs", "peaks", "energy"}]`` sorted), so
        the mobile bucket mapper is reused unchanged; the ``"hour"`` field
        carries the 5-minute bucket start (kept named ``"hour"`` for wire
        compatibility with the hourly path).
        """
        from . import rollup as _r

        # *day* is a HOUSEHOLD-LOCAL calendar date; half-open on the next local
        # midnight (readings carry sub-second ts).
        day_start, next_day_start = local_day_window(day, tz_name)
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT ts, payload FROM readings_raw "
                "WHERE inverter_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                (inverter_id, day_start, next_day_start),
            )
            buckets: dict[str, list[dict]] = {}
            for r in cur.fetchall():
                bucket = self._five_min_of(r["ts"])
                buckets.setdefault(bucket, []).append({"payload": json.loads(r["payload"])})
            result = []
            for bucket in sorted(buckets):
                agg = _r.aggregate(buckets[bucket])
                result.append(
                    {
                        "hour": bucket,
                        "sample_count": agg["sample_count"],
                        "avgs": agg["avgs"],
                        "peaks": agg["peaks"],
                        "energy": agg["energy"],
                    }
                )
            return result
        finally:
            conn.close()

    def _history_range_sync(self, inverter_id: str, start_day: str, end_day: str) -> list[dict]:
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT day, sample_count, avgs, peaks, energy FROM readings_daily "
                "WHERE inverter_id = ? AND day >= ? AND day <= ? ORDER BY day",
                (inverter_id, start_day, end_day),
            )
            return [
                {
                    "day": r["day"],
                    "sample_count": r["sample_count"],
                    "avgs": json.loads(r["avgs"]),
                    "peaks": json.loads(r["peaks"]),
                    "energy": json.loads(r["energy"]),
                }
                for r in cur.fetchall()
            ]
        finally:
            conn.close()

    def _history_range_live_sync(
        self,
        inverter_id: str,
        start_day: str,
        end_day: str,
        now_iso: str | None = None,
        tz_name: str | None = None,
    ) -> list[dict]:
        """Sealed ``readings_daily`` rows for days < today, plus today aggregated
        live from ``readings_raw`` (if today falls within [start_day, end_day]).

        Mirrors ``_rollup_sync``'s aggregation exactly: group today's raw rows
        by hour → ``rollup.aggregate`` per hour → ``rollup.merge_hourly`` → daily
        bucket ``{"day": today, "sample_count", "avgs", "peaks", "energy"}``.
        Result is sorted by day.  ``now_iso`` defaults to the real UTC clock;
        tests pass an explicit value to pin "today".
        """
        from datetime import datetime, timedelta

        from . import rollup as _r

        if now_iso is None:
            now_iso = datetime.now(UTC).isoformat()
        today = self._day_of(now_iso, tz_name)

        # Yesterday = last completed day for the sealed query upper bound.
        today_dt: datetime | None = None
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            yesterday = (today_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            yesterday = today

        conn = _connect(self._db_path)
        try:
            result: list[dict] = []

            # 1. Sealed prior days: start_day..min(end_day, yesterday)
            sealed_end = min(end_day, yesterday)
            if sealed_end >= start_day:
                cur = conn.execute(
                    "SELECT day, sample_count, avgs, peaks, energy "
                    "FROM readings_daily "
                    "WHERE inverter_id = ? AND day >= ? AND day <= ? ORDER BY day",
                    (inverter_id, start_day, sealed_end),
                )
                for r in cur.fetchall():
                    result.append(
                        {
                            "day": r["day"],
                            "sample_count": r["sample_count"],
                            "avgs": json.loads(r["avgs"]),
                            "peaks": json.loads(r["peaks"]),
                            "energy": json.loads(r["energy"]),
                        }
                    )

            # 2. Today's live bucket (only if today is within the requested range)
            if today_dt is not None and start_day <= today <= end_day:
                # Exclusive next-LOCAL-midnight bound: our readings carry
                # sub-second ts (e.g. "...T23:59:59.743Z"), so an inclusive
                # "<= T23:59:59Z" bound would drop the day's last second.
                day_start, next_day_start = local_day_window(today, tz_name)
                cur = conn.execute(
                    "SELECT ts, payload FROM readings_raw "
                    "WHERE inverter_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                    (inverter_id, day_start, next_day_start),
                )
                buckets: dict[str, list[dict]] = {}
                for r in cur.fetchall():
                    hour = self._hour_of(r["ts"])
                    buckets.setdefault(hour, []).append({"payload": json.loads(r["payload"])})
                if buckets:
                    hour_aggs = [_r.aggregate(buckets[h]) for h in sorted(buckets)]
                    daily_agg = _r.merge_hourly(hour_aggs)
                    result.append({"day": today, **daily_agg})

            result.sort(key=lambda r: r["day"])
            return result
        finally:
            conn.close()

    def _month_bounds(self, month: str) -> tuple[str, str]:
        """Return (start_day, end_day) inclusive, both 'YYYY-MM-DD', for a 'YYYY-MM' month.

        Raises ValueError for a malformed *month* (bad shape, non-numeric, or
        month out of 1..12); the endpoint maps that to HTTP 400.
        """
        import calendar

        try:
            year, mon = int(month[:4]), int(month[5:7])
        except (ValueError, TypeError) as err:
            raise ValueError(f"malformed month: {month!r}") from err
        if not 1 <= mon <= 12:
            raise ValueError(f"month out of range: {month!r}")
        start_day = f"{year:04d}-{mon:02d}-01"
        last_day = calendar.monthrange(year, mon)[1]
        end_day = f"{year:04d}-{mon:02d}-{last_day:02d}"
        return start_day, end_day

    def _month_hourly_range_live_sync(
        self,
        inverter_id: str,
        month: str,
        now_iso: str | None = None,
        tz_name: str | None = None,
    ) -> list[dict]:
        """Sealed ``readings_hourly`` rows for the month's hours before today,
        plus today's hourly buckets computed live from ``readings_raw`` (if
        today falls within the requested month).

        Mirrors ``_history_range_live_sync``'s sealed-prior + live-today
        union pattern, but spans a whole month of HOURS rather than a range
        of DAYS: sealed rows already covering earlier hours of today (the
        rollup seals completed hours regardless of day boundary) are
        excluded and fully superseded by the live recompute for today, so
        today is never double-counted.

        Returns rows shaped like ``readings_hourly``:
        ``[{"hour_start", "sample_count", "avgs", "peaks", "energy"}]``,
        sorted by ``hour_start``. ``now_iso`` defaults to the real UTC
        clock; tests pass an explicit value to pin "today".
        """
        from datetime import datetime

        if now_iso is None:
            now_iso = datetime.now(UTC).isoformat()
        today = self._day_of(now_iso, tz_name)

        start_day, end_day = self._month_bounds(month)
        # The month spans from the first local midnight of start_day to the
        # local midnight after end_day -- so a UTC+3 household's month does not
        # lose its first three local hours to the previous month.
        month_start, _ = local_day_window(start_day, tz_name)
        _, month_end_exclusive = local_day_window(end_day, tz_name)
        today_start, _ = local_day_window(today, tz_name)

        result: list[dict] = []

        # 1. Sealed hours strictly before today, within the month range.
        sealed_upper = min(month_end_exclusive, today_start)
        if sealed_upper > month_start:
            conn = _connect(self._db_path)
            try:
                cur = conn.execute(
                    "SELECT hour_start, sample_count, avgs, peaks, energy "
                    "FROM readings_hourly "
                    "WHERE inverter_id = ? AND hour_start >= ? AND hour_start < ? "
                    "ORDER BY hour_start",
                    (inverter_id, month_start, sealed_upper),
                )
                for r in cur.fetchall():
                    result.append(
                        {
                            "hour_start": r["hour_start"],
                            "sample_count": r["sample_count"],
                            "avgs": json.loads(r["avgs"]),
                            "peaks": json.loads(r["peaks"]),
                            "energy": json.loads(r["energy"]),
                        }
                    )
            finally:
                conn.close()

        # 2. Today's hourly buckets, computed live -- only if today is
        # within the requested month.
        if start_day <= today <= end_day:
            for row in self._hourly_range_live_sync(inverter_id, today, tz_name):
                result.append(
                    {
                        "hour_start": row["hour"],
                        "sample_count": row["sample_count"],
                        "avgs": row["avgs"],
                        "peaks": row["peaks"],
                        "energy": row["energy"],
                    }
                )

        result.sort(key=lambda r: r["hour_start"])
        return result

    def _sync_status_sync(self) -> dict:
        conn = _connect(self._db_path)
        try:
            counts = {
                r["sync_state"]: r["c"]
                for r in conn.execute(
                    "SELECT sync_state, COUNT(*) c FROM readings_raw GROUP BY sync_state"
                )
            }
            row = conn.execute(
                "SELECT MAX(ts) m FROM readings_raw WHERE sync_state='sent'"
            ).fetchone()
            return {
                "counts": counts,
                "last_sent_ts": row["m"] if row else None,
                "cloud_ingest_enabled": self.cloud_ingest_enabled,
            }
        finally:
            conn.close()

    def _set_meta_sync(self, key: str, value: str) -> None:
        conn = _connect(self._db_path)
        try:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
        finally:
            conn.close()

    def _get_meta_sync(self, key: str) -> str | None:
        conn = _connect(self._db_path)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def _set_lifecycle_sync(self, state: str, reason: str | None, since: str | None) -> None:
        conn = _connect(self._db_path)
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                [
                    ("lifecycle_state", state),
                    ("lifecycle_reason", reason or ""),
                    ("lifecycle_since", since or ""),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def _get_lifecycle_sync(self) -> dict:
        st = self._get_meta_sync("lifecycle_state") or "active"
        rs = self._get_meta_sync("lifecycle_reason") or None
        sn = self._get_meta_sync("lifecycle_since") or None
        return {"state": st, "reason": rs or None, "since": sn or None}

    def _prune_inverters_not_in_sync(self, keep_ids: set) -> int:
        """Delete readings_raw rows for inverters not in keep_ids.

        Only prunes readings_raw (the outbound queue — the bug vector).
        readings_hourly / readings_daily are the local archive and are
        intentionally left untouched.

        Returns the number of rows deleted.
        """
        keep = list(keep_ids)
        conn = _connect(self._db_path)
        try:
            if not keep:
                cur = conn.execute("DELETE FROM readings_raw")
            else:
                placeholders = ",".join("?" * len(keep))
                cur = conn.execute(
                    f"DELETE FROM readings_raw WHERE inverter_id NOT IN ({placeholders})",
                    keep,
                )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    async def prune_inverters_not_in(self, keep_ids: set) -> int:
        return await self._hass.async_add_executor_job(self._prune_inverters_not_in_sync, keep_ids)

    async def set_lifecycle(self, state: str, reason: str | None, since: str | None) -> None:
        await self._hass.async_add_executor_job(self._set_lifecycle_sync, state, reason, since)

    async def get_lifecycle(self) -> dict:
        return await self._hass.async_add_executor_job(self._get_lifecycle_sync)

    def _connect_for_test(self) -> sqlite3.Connection:
        return _connect(self._db_path)

    # ── async wrappers ────────────────────────────────────────────────
    async def rebuild_daily_local(self, tz_name: str | None, now_iso: str) -> dict[str, int]:
        return await self._hass.async_add_executor_job(
            self._rebuild_daily_local_sync, tz_name, now_iso
        )

    async def rollup(self, now_iso: str, tz_name: str | None = None) -> dict[str, int]:
        return await self._hass.async_add_executor_job(self._rollup_sync, now_iso, tz_name)

    async def prune(
        self, now_iso: str, raw_retention_s: int, hourly_retention_s: int
    ) -> dict[str, int]:
        return await self._hass.async_add_executor_job(
            self._prune_sync, now_iso, raw_retention_s, hourly_retention_s
        )

    async def get_sendable(self, now_iso: str, cap_s: int, limit: int) -> list[dict]:
        return await self._hass.async_add_executor_job(
            self._get_sendable_sync, now_iso, cap_s, limit
        )

    async def mark_sent(self, keys: list[tuple[str, str]]) -> None:
        await self._hass.async_add_executor_job(self._mark_sent_sync, keys)

    async def mark_failed(self, keys: list[tuple[str, str]], now_iso: str) -> None:
        await self._hass.async_add_executor_job(self._mark_failed_sync, keys, now_iso)

    async def skip_aged(self, now_iso: str, cap_s: int) -> int:
        return await self._hass.async_add_executor_job(self._skip_aged_sync, now_iso, cap_s)

    async def append(self, reading: dict[str, Any]) -> None:
        await self._hass.async_add_executor_job(self._append_sync, reading)
        # Wake the sender immediately so the fresh reading is pushed within ~ms.
        self._signal_data_available()

    async def recent(self, inverter_id: str, limit: int) -> list[dict[str, Any]]:
        return await self._hass.async_add_executor_job(self._recent_sync, inverter_id, limit)

    async def count_by_state(self) -> dict[str, int]:
        return await self._hass.async_add_executor_job(self._count_by_state_sync)

    async def live_snapshot(self) -> list[dict]:
        return await self._hass.async_add_executor_job(self._live_snapshot_sync)

    async def today_summary(self, day: str, tz_name: str | None = None) -> list[dict]:
        return await self._hass.async_add_executor_job(self._today_summary_sync, day, tz_name)

    async def history_range(self, inverter_id: str, start_day: str, end_day: str) -> list[dict]:
        return await self._hass.async_add_executor_job(
            self._history_range_sync, inverter_id, start_day, end_day
        )

    async def history_range_live(
        self, inverter_id: str, start_day: str, end_day: str, tz_name: str | None = None
    ) -> list[dict]:
        return await self._hass.async_add_executor_job(
            self._history_range_live_sync, inverter_id, start_day, end_day, None, tz_name
        )

    async def hourly_range(
        self, inverter_id: str, day: str, tz_name: str | None = None
    ) -> list[dict]:
        return await self._hass.async_add_executor_job(
            self._hourly_range_sync, inverter_id, day, tz_name
        )

    async def hourly_range_live(
        self, inverter_id: str, day: str, tz_name: str | None = None
    ) -> list[dict]:
        return await self._hass.async_add_executor_job(
            self._hourly_range_live_sync, inverter_id, day, tz_name
        )

    async def five_min_range_live(
        self, inverter_id: str, day: str, tz_name: str | None = None
    ) -> list[dict]:
        return await self._hass.async_add_executor_job(
            self._five_min_range_live_sync, inverter_id, day, tz_name
        )

    async def month_hourly_range_live(
        self, inverter_id: str, month: str, tz_name: str | None = None
    ) -> list[dict]:
        return await self._hass.async_add_executor_job(
            self._month_hourly_range_live_sync, inverter_id, month, None, tz_name
        )

    async def sync_status(self) -> dict:
        return await self._hass.async_add_executor_job(self._sync_status_sync)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "inverter_id": row["inverter_id"],
        "ts": row["ts"],
        "payload": json.loads(row["payload"]),
        "sync_state": row["sync_state"],
        "attempts": row["attempts"],
    }
