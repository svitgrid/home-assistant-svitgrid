"""Durable local SQLite store for produced readings (Sub-project 1)."""
from __future__ import annotations

import json
import sqlite3
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

    # ── async wrappers ────────────────────────────────────────────────
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


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "inverter_id": row["inverter_id"],
        "ts": row["ts"],
        "payload": json.loads(row["payload"]),
        "sync_state": row["sync_state"],
        "attempts": row["attempts"],
    }
