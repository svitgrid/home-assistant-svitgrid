"""Durable local SQLite store for produced readings (Sub-project 1)."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from homeassistant.core import HomeAssistant


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _create_schema(conn)
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

    # ── async wrappers ────────────────────────────────────────────────
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
