"""Durable local SQLite store for produced readings (Sub-project 1)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant

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
        try:
            parsed.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
        except (ValueError, AttributeError):
            pass  # malformed timestamp — treat as unknown, skip rather than crash
    if len(parsed) < 2:
        return None
    # Gaps between consecutive entries (list is desc → older − newer yields positive).
    gaps = [
        abs((parsed[i] - parsed[i + 1]).total_seconds())
        for i in range(len(parsed) - 1)
    ]
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
        return ts[:13] + ":00:00Z"   # "2026-06-24T10:..." → "2026-06-24T10:00:00Z"

    def _day_of(self, ts: str) -> str:
        return ts[:10]               # "2026-06-24"

    def _rollup_sync(self, now_iso: str) -> dict[str, int]:
        from . import rollup as _r
        cur_hour = self._hour_of(now_iso)
        cur_day = self._day_of(now_iso)
        conn = _connect(self._db_path)
        hours = days = 0
        try:
            # COMPLETED hours: group raw rows whose hour < current hour
            cur = conn.execute(
                "SELECT inverter_id, ts, payload FROM readings_raw ORDER BY inverter_id, ts")
            buckets: dict[tuple[str, str], list[dict]] = {}
            for r in cur.fetchall():
                hour = self._hour_of(r["ts"])
                if hour >= cur_hour:
                    continue
                buckets.setdefault((r["inverter_id"], hour), []).append(
                    {"payload": json.loads(r["payload"])})
            for (inv, hour), rows in buckets.items():
                agg = _r.aggregate(rows)
                conn.execute(
                    "INSERT OR REPLACE INTO readings_hourly "
                    "(inverter_id, hour_start, sample_count, avgs, peaks, energy) "
                    "VALUES (?,?,?,?,?,?)",
                    (inv, hour, agg["sample_count"], json.dumps(agg["avgs"]),
                     json.dumps(agg["peaks"]), json.dumps(agg["energy"])))
                hours += 1
            # COMPLETED days: group hourly rows whose day < current day
            cur = conn.execute(
                "SELECT inverter_id, hour_start, sample_count, avgs, peaks, energy "
                "FROM readings_hourly")
            dbuckets: dict[tuple[str, str], list[dict]] = {}
            for r in cur.fetchall():
                day = self._day_of(r["hour_start"])
                if day >= cur_day:
                    continue
                dbuckets.setdefault((r["inverter_id"], day), []).append({
                    "sample_count": r["sample_count"],
                    "avgs": json.loads(r["avgs"]), "peaks": json.loads(r["peaks"]),
                    "energy": json.loads(r["energy"])})
            for (inv, day), hrows in dbuckets.items():
                agg = _r.merge_hourly(hrows)
                conn.execute(
                    "INSERT OR REPLACE INTO readings_daily "
                    "(inverter_id, day, sample_count, avgs, peaks, energy) "
                    "VALUES (?,?,?,?,?,?)",
                    (inv, day, agg["sample_count"], json.dumps(agg["avgs"]),
                     json.dumps(agg["peaks"]), json.dumps(agg["energy"])))
                days += 1
            conn.commit()
            return {"hours": hours, "days": days}
        finally:
            conn.close()

    def _prune_sync(self, now_iso: str, raw_retention_s: int,
                    hourly_retention_s: int) -> dict[str, int]:
        raw_floor = self._cap_boundary(now_iso, raw_retention_s)
        hourly_floor = self._cap_boundary(now_iso, hourly_retention_s)
        conn = _connect(self._db_path)
        try:
            c1 = conn.execute("DELETE FROM readings_raw WHERE ts < ?", (raw_floor,))
            c2 = conn.execute("DELETE FROM readings_hourly WHERE hour_start < ?",
                              (hourly_floor,))
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
                "ON r.inverter_id = m.inverter_id AND r.ts = m.mts")
            rows = cur.fetchall()
            result = []
            for r in rows:
                inverter_id = r["inverter_id"]
                # Fetch up to 6 recent timestamps to compute the observed cadence.
                ts_cur = conn.execute(
                    "SELECT ts FROM readings_raw WHERE inverter_id = ? "
                    "ORDER BY ts DESC LIMIT 6",
                    (inverter_id,),
                )
                ts_list = [row["ts"] for row in ts_cur.fetchall()]
                interval_s = _median_gap_seconds(ts_list)
                result.append({
                    "inverterId": inverter_id,
                    "ts": r["ts"],
                    "payload": json.loads(r["payload"]),
                    "intervalS": interval_s,
                })
            return result
        finally:
            conn.close()

    def _today_summary_sync(self, day: str) -> list[dict]:
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT inverter_id, sample_count, peaks, energy FROM readings_daily "
                "WHERE day = ?", (day,))
            rows = [{"inverterId": r["inverter_id"], "sample_count": r["sample_count"],
                     "peaks": json.loads(r["peaks"]), "energy": json.loads(r["energy"])}
                    for r in cur.fetchall()]
            if rows:
                return rows
            # Fallback: aggregate today's raw (daily row not rolled up yet).
            from datetime import datetime, timedelta
            from . import rollup as _r
            next_day = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            cur = conn.execute(
                "SELECT inverter_id, payload FROM readings_raw WHERE ts >= ? AND ts < ?",
                (day + "T00:00:00Z", next_day + "T00:00:00Z"))
            buckets: dict[str, list[dict]] = {}
            for r in cur.fetchall():
                buckets.setdefault(r["inverter_id"], []).append(
                    {"payload": json.loads(r["payload"])})
            out = []
            for inv, rws in buckets.items():
                agg = _r.aggregate(rws)
                out.append({"inverterId": inv, "sample_count": agg["sample_count"],
                            "peaks": agg["peaks"], "energy": agg["energy"]})
            return out
        finally:
            conn.close()

    def _history_range_sync(self, inverter_id: str, start_day: str,
                            end_day: str) -> list[dict]:
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT day, sample_count, avgs, peaks, energy FROM readings_daily "
                "WHERE inverter_id = ? AND day >= ? AND day <= ? ORDER BY day",
                (inverter_id, start_day, end_day))
            return [{"day": r["day"], "sample_count": r["sample_count"],
                     "avgs": json.loads(r["avgs"]), "peaks": json.loads(r["peaks"]),
                     "energy": json.loads(r["energy"])} for r in cur.fetchall()]
        finally:
            conn.close()

    def _sync_status_sync(self) -> dict:
        conn = _connect(self._db_path)
        try:
            counts = {r["sync_state"]: r["c"] for r in conn.execute(
                "SELECT sync_state, COUNT(*) c FROM readings_raw GROUP BY sync_state")}
            row = conn.execute(
                "SELECT MAX(ts) m FROM readings_raw WHERE sync_state='sent'").fetchone()
            return {"counts": counts, "last_sent_ts": row["m"] if row else None}
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
                [("lifecycle_state", state),
                 ("lifecycle_reason", reason or ""),
                 ("lifecycle_since", since or "")],
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
        return await self._hass.async_add_executor_job(
            self._prune_inverters_not_in_sync, keep_ids
        )

    async def set_lifecycle(self, state: str, reason: str | None, since: str | None) -> None:
        await self._hass.async_add_executor_job(self._set_lifecycle_sync, state, reason, since)

    async def get_lifecycle(self) -> dict:
        return await self._hass.async_add_executor_job(self._get_lifecycle_sync)

    def _connect_for_test(self) -> sqlite3.Connection:
        return _connect(self._db_path)

    # ── async wrappers ────────────────────────────────────────────────
    async def rollup(self, now_iso: str) -> dict[str, int]:
        return await self._hass.async_add_executor_job(self._rollup_sync, now_iso)

    async def prune(self, now_iso: str, raw_retention_s: int,
                    hourly_retention_s: int) -> dict[str, int]:
        return await self._hass.async_add_executor_job(
            self._prune_sync, now_iso, raw_retention_s, hourly_retention_s)

    async def get_sendable(self, now_iso: str, cap_s: int, limit: int) -> list[dict]:
        return await self._hass.async_add_executor_job(
            self._get_sendable_sync, now_iso, cap_s, limit)

    async def mark_sent(self, keys: list[tuple[str, str]]) -> None:
        await self._hass.async_add_executor_job(self._mark_sent_sync, keys)

    async def mark_failed(self, keys: list[tuple[str, str]], now_iso: str) -> None:
        await self._hass.async_add_executor_job(self._mark_failed_sync, keys, now_iso)

    async def skip_aged(self, now_iso: str, cap_s: int) -> int:
        return await self._hass.async_add_executor_job(self._skip_aged_sync, now_iso, cap_s)

    async def append(self, reading: dict[str, Any]) -> None:
        await self._hass.async_add_executor_job(self._append_sync, reading)

    async def recent(self, inverter_id: str, limit: int) -> list[dict[str, Any]]:
        return await self._hass.async_add_executor_job(
            self._recent_sync, inverter_id, limit
        )

    async def count_by_state(self) -> dict[str, int]:
        return await self._hass.async_add_executor_job(self._count_by_state_sync)

    async def live_snapshot(self) -> list[dict]:
        return await self._hass.async_add_executor_job(self._live_snapshot_sync)

    async def today_summary(self, day: str) -> list[dict]:
        return await self._hass.async_add_executor_job(self._today_summary_sync, day)

    async def history_range(self, inverter_id: str, start_day: str,
                            end_day: str) -> list[dict]:
        return await self._hass.async_add_executor_job(
            self._history_range_sync, inverter_id, start_day, end_day)

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
