"""Local SQLite store for island-mode calendar events and their execution state.

Mirrors the ReadingStore connection/executor pattern: sync ``_*_sync`` core
methods that open → operate → close the connection, plus async wrappers that
run them in the HA thread-pool executor so the event loop never blocks on I/O.

When *hass* is provided, async wrappers delegate to
``hass.async_add_executor_job`` (HA's bounded, lifecycle-managed pool) — the
same pattern used by ``reading_store.py``.  When *hass* is ``None`` (e.g. in
unit tests that construct the store without a hass fixture), the wrappers fall
back to ``asyncio.get_running_loop().run_in_executor(None, ...)``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Tracks db_paths whose schema has already been created in this process.
_INITIALIZED: set[str] = set()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if db_path not in _INITIALIZED:
        _create_schema(conn)
        _INITIALIZED.add(db_path)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS island_events (
            event_id        TEXT PRIMARY KEY,
            event_json      TEXT NOT NULL,
            enabled         INTEGER NOT NULL DEFAULT 1,
            execution_state TEXT NOT NULL DEFAULT '{}',
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_island_events_updated
            ON island_events (updated_at);
        """
    )
    conn.commit()


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    """Merge stored event JSON with executionState into a single dict."""
    event = json.loads(row["event_json"])
    state = json.loads(row["execution_state"]) if row["execution_state"] else {}
    event["executionState"] = state
    return event


class IslandEventStore:
    """SQLite-backed store for island-mode calendar events.

    ``db_path`` is the path to the SQLite file (use ``:memory:`` for tests).
    Pass *hass* to use HA's bounded ``async_add_executor_job`` pool; omit it
    (or pass ``None``) to fall back to
    ``asyncio.get_running_loop().run_in_executor`` for standalone use in tests.
    """

    def __init__(self, db_path: str, hass: HomeAssistant | None = None) -> None:
        self._db_path = db_path
        self._hass = hass

    # ── sync core ─────────────────────────────────────────────────────────────

    def _upsert_event_sync(self, event: dict[str, Any]) -> None:
        """INSERT OR REPLACE the event keyed on event['id'].

        ``enabled`` is stored as 1 if event['enabled'] is truthy (default 1).
        """
        event_id: str = event["id"]
        enabled: int = 1 if event.get("enabled", True) else 0
        conn = _connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO island_events "
                "(event_id, event_json, enabled, execution_state, updated_at) "
                "VALUES (?, ?, ?, COALESCE("
                "    (SELECT execution_state FROM island_events WHERE event_id = ?),"
                "    '{}'"
                "), ?)",
                (
                    event_id,
                    json.dumps(event, separators=(",", ":")),
                    enabled,
                    event_id,
                    _now_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _delete_event_sync(self, event_id: str) -> bool:
        """Delete event by id.  Returns True if a row was removed."""
        conn = _connect(self._db_path)
        try:
            cur = conn.execute("DELETE FROM island_events WHERE event_id = ?", (event_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def _list_events_sync(self) -> list[dict[str, Any]]:
        """Return all events, each merged with its executionState."""
        conn = _connect(self._db_path)
        try:
            cur = conn.execute(
                "SELECT event_id, event_json, execution_state "
                "FROM island_events ORDER BY updated_at ASC"
            )
            return [_row_to_event(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def _get_event_sync(self, event_id: str) -> dict[str, Any] | None:
        """Return a single event merged with its executionState, or None."""
        conn = _connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT event_id, event_json, execution_state "
                "FROM island_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            return _row_to_event(row) if row else None
        finally:
            conn.close()

    def _set_execution_state_sync(self, event_id: str, state: dict[str, Any]) -> None:
        """Persist execution state for an event (no-op if event_id unknown)."""
        conn = _connect(self._db_path)
        try:
            conn.execute(
                "UPDATE island_events SET execution_state = ?, updated_at = ? WHERE event_id = ?",
                (json.dumps(state, separators=(",", ":")), _now_iso(), event_id),
            )
            conn.commit()
        finally:
            conn.close()

    # ── async wrappers ────────────────────────────────────────────────────────

    async def async_upsert_event(self, event: dict[str, Any]) -> None:
        if self._hass is not None:
            await self._hass.async_add_executor_job(self._upsert_event_sync, event)
        else:
            await asyncio.get_running_loop().run_in_executor(None, self._upsert_event_sync, event)

    async def async_delete_event(self, event_id: str) -> bool:
        if self._hass is not None:
            return await self._hass.async_add_executor_job(self._delete_event_sync, event_id)
        return await asyncio.get_running_loop().run_in_executor(
            None, self._delete_event_sync, event_id
        )

    async def async_list_events(self) -> list[dict[str, Any]]:
        if self._hass is not None:
            return await self._hass.async_add_executor_job(self._list_events_sync)
        return await asyncio.get_running_loop().run_in_executor(None, self._list_events_sync)

    async def async_get_event(self, event_id: str) -> dict[str, Any] | None:
        if self._hass is not None:
            return await self._hass.async_add_executor_job(self._get_event_sync, event_id)
        return await asyncio.get_running_loop().run_in_executor(
            None, self._get_event_sync, event_id
        )

    async def async_set_execution_state(self, event_id: str, state: dict[str, Any]) -> None:
        if self._hass is not None:
            await self._hass.async_add_executor_job(self._set_execution_state_sync, event_id, state)
        else:
            await asyncio.get_running_loop().run_in_executor(
                None, self._set_execution_state_sync, event_id, state
            )
