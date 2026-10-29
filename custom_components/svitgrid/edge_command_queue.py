"""The commands an adopted edge box drains from this integration.

On an edge island the box polls `GET /api/v3/executors/commands` here instead
of the cloud, takes one command per poll and acks it on
`/api/v3/executors/commands/{id}/ack` (`edge_api.py`). This queue holds those
commands in SQLite, one row per command:

- `pending` until a poll hands it out, then `delivered`;
- `success` or `failed` when the box acks it;
- `expired` when its TTL passes while pending, or when it was delivered
  twice and never acked. A delivered command with no ack after
  `REDELIVER_AFTER_S` goes back to `pending` once, so a lost ack does not lose
  the command, and a box that keeps dropping it does not see it forever.

A caller awaits the box's answer with `async_wait`, woken by the ack or the
expiry. Mirrors the `island_event_store.py` pattern: sync `_*_sync` methods
that open, operate and close the connection, run in HA's executor pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# A delivered command with no ack after this long goes back to `pending`.
REDELIVER_AFTER_S = 120
# How many times a command is handed to the box before it expires unacked.
MAX_DELIVERIES = 2
DEFAULT_TTL_S = 900

_DONE_STATES = ("success", "failed", "expired")

# Tracks db_paths whose schema has already been created in this process.
_INITIALIZED: set[str] = set()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
        CREATE TABLE IF NOT EXISTS edge_commands (
            id           TEXT PRIMARY KEY,
            device_id    TEXT NOT NULL,
            inverter_id  TEXT,
            command      TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            signing_json TEXT,
            state        TEXT NOT NULL CHECK(state IN
                             ('pending','delivered','success','failed','expired')),
            source       TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            delivered_at TEXT,
            acked_at     TEXT,
            result_json  TEXT,
            reason       TEXT,
            expires_at   TEXT NOT NULL,
            deliveries   INTEGER NOT NULL DEFAULT 0,
            rejected     INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_edge_commands_device_state
            ON edge_commands (device_id, state, created_at);
        """
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "inverter_id": row["inverter_id"],
        "command": row["command"],
        "payload": json.loads(row["payload_json"]),
        "signing": json.loads(row["signing_json"]) if row["signing_json"] else None,
        "state": row["state"],
        "source": row["source"],
        "created_at": row["created_at"],
        "delivered_at": row["delivered_at"],
        "acked_at": row["acked_at"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "rejected": None if row["rejected"] is None else bool(row["rejected"]),
        "reason": row["reason"],
        "expires_at": row["expires_at"],
        "deliveries": row["deliveries"],
    }


def _get_sync(conn: sqlite3.Connection, command_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM edge_commands WHERE id = ?", (command_id,)).fetchone()
    return _row_to_dict(row) if row is not None else None


class EdgeCommandQueue:
    """SQLite-backed command queue for adopted edge boxes, keyed by device id.

    Pass *hass* to use HA's bounded ``async_add_executor_job`` pool; ``None``
    falls back to ``run_in_executor`` for tests.
    """

    def __init__(self, db_path: str, hass: HomeAssistant | None = None) -> None:
        self._db_path = db_path
        self._hass = hass
        self._events: dict[str, asyncio.Event] = {}

    # ── sync core ─────────────────────────────────────────────────────────────

    def _sweep_sync(self, conn: sqlite3.Connection, device_id: str, now: datetime) -> list[str]:
        """Return unacked deliveries to `pending` (or expire them), expire
        pending rows past their TTL. Returns the ids that expired."""
        stale = _iso(now - timedelta(seconds=REDELIVER_AFTER_S))
        now_iso = _iso(now)
        expired = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM edge_commands WHERE device_id = ? AND ("
                " (state = 'delivered' AND delivered_at <= ? AND deliveries >= ?)"
                " OR (state = 'pending' AND expires_at <= ?))",
                (device_id, stale, MAX_DELIVERIES, now_iso),
            )
        ]
        conn.execute(
            "UPDATE edge_commands SET state = 'expired', reason = 'not_acked' "
            "WHERE device_id = ? AND state = 'delivered' AND delivered_at <= ? "
            "AND deliveries >= ?",
            (device_id, stale, MAX_DELIVERIES),
        )
        conn.execute(
            "UPDATE edge_commands SET state = 'pending' "
            "WHERE device_id = ? AND state = 'delivered' AND delivered_at <= ?",
            (device_id, stale),
        )
        conn.execute(
            "UPDATE edge_commands SET state = 'expired', reason = 'ttl' "
            "WHERE device_id = ? AND state = 'pending' AND expires_at <= ?",
            (device_id, now_iso),
        )
        conn.commit()
        return expired

    def _enqueue_sync(
        self,
        device_id: str,
        command: str,
        payload: dict,
        signing: dict | None,
        inverter_id: str | None,
        source: str,
        ttl_s: int,
    ) -> str:
        command_id = str(uuid.uuid4())
        now = _utcnow()
        conn = _connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO edge_commands (id, device_id, inverter_id, command, "
                "payload_json, signing_json, state, source, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    command_id,
                    device_id,
                    inverter_id,
                    command,
                    json.dumps(payload, separators=(",", ":")),
                    json.dumps(signing, separators=(",", ":")) if signing else None,
                    source,
                    _iso(now),
                    _iso(now + timedelta(seconds=ttl_s)),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return command_id

    def _next_pending_sync(self, device_id: str) -> tuple[dict[str, Any] | None, list[str]]:
        now = _utcnow()
        conn = _connect(self._db_path)
        try:
            expired = self._sweep_sync(conn, device_id, now)
            row = conn.execute(
                "SELECT id FROM edge_commands WHERE device_id = ? AND state = 'pending' "
                "ORDER BY created_at ASC, rowid ASC LIMIT 1",
                (device_id,),
            ).fetchone()
            if row is None:
                return None, expired
            conn.execute(
                "UPDATE edge_commands SET state = 'delivered', delivered_at = ?, "
                "deliveries = deliveries + 1 WHERE id = ?",
                (_iso(now), row["id"]),
            )
            conn.commit()
            return _get_sync(conn, row["id"]), expired
        finally:
            conn.close()

    def _ack_sync(
        self,
        command_id: str,
        success: bool,
        result: Any,
        rejected: bool | None,
        reason: str | None,
        device_id: str | None,
    ) -> dict[str, Any] | None:
        conn = _connect(self._db_path)
        try:
            row = _get_sync(conn, command_id)
            if row is None or (device_id is not None and row["device_id"] != device_id):
                return None
            if row["state"] in ("success", "failed"):
                return row
            conn.execute(
                "UPDATE edge_commands SET state = ?, acked_at = ?, result_json = ?, "
                "rejected = ?, reason = ? WHERE id = ?",
                (
                    "success" if success else "failed",
                    _iso(_utcnow()),
                    json.dumps(result, separators=(",", ":")) if result is not None else None,
                    None if rejected is None else int(bool(rejected)),
                    reason,
                    command_id,
                ),
            )
            conn.commit()
            return _get_sync(conn, command_id)
        finally:
            conn.close()

    def _get_by_id_sync(self, command_id: str) -> dict[str, Any] | None:
        conn = _connect(self._db_path)
        try:
            return _get_sync(conn, command_id)
        finally:
            conn.close()

    def _recent_sync(self, device_id: str, limit: int) -> list[dict[str, Any]]:
        conn = _connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM edge_commands WHERE device_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def _pending_count_sync(self, device_id: str) -> int:
        conn = _connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM edge_commands "
                "WHERE device_id = ? AND state = 'pending' AND expires_at > ?",
                (device_id, _iso(_utcnow())),
            ).fetchone()
            return int(row[0])
        finally:
            conn.close()

    def _forget_device_sync(self, device_id: str) -> list[str]:
        conn = _connect(self._db_path)
        try:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT id FROM edge_commands WHERE device_id = ?", (device_id,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM edge_commands WHERE device_id = ?", (device_id,))
            conn.commit()
            return ids
        finally:
            conn.close()

    # ── async wrappers ────────────────────────────────────────────────────────

    async def _run(self, func, *args):
        if self._hass is not None:
            return await self._hass.async_add_executor_job(func, *args)
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    def _wake(self, command_id: str) -> None:
        event = self._events.get(command_id)
        if event is not None:
            event.set()

    async def async_enqueue(
        self,
        device_id: str,
        command: str,
        payload: dict,
        *,
        signing: dict | None,
        inverter_id: str | None,
        source: str,
        ttl_s: int = DEFAULT_TTL_S,
    ) -> str:
        """Queue `command` for the box; returns its id. `signing` holds the
        phone's `signedEventData`, `signature` and `signingKeyId`, untouched."""
        return await self._run(
            self._enqueue_sync, device_id, command, payload, signing, inverter_id, source, ttl_s
        )

    async def async_next_pending(self, device_id: str) -> dict[str, Any] | None:
        """The box's oldest pending command, now marked `delivered`."""
        row, expired = await self._run(self._next_pending_sync, device_id)
        for command_id in expired:
            self._wake(command_id)
        return row

    async def async_ack(
        self,
        command_id: str,
        *,
        success: bool,
        result: Any,
        rejected: bool | None,
        reason: str | None,
        device_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record the box's answer; None for an id the queue does not hold
        (or, with `device_id`, holds for another box). An already acked
        command keeps its first answer."""
        row = await self._run(
            self._ack_sync, command_id, success, result, rejected, reason, device_id
        )
        if row is not None:
            self._wake(command_id)
        return row

    async def async_wait(self, command_id: str, timeout_s: float) -> dict[str, Any] | None:
        """The command's row once acked or expired, or as it stands after
        `timeout_s`. None for an unknown id."""
        event = self._events.setdefault(command_id, asyncio.Event())
        try:
            row = await self._run(self._get_by_id_sync, command_id)
            if row is None or row["state"] in _DONE_STATES:
                return row
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout_s)
            return await self._run(self._get_by_id_sync, command_id)
        finally:
            if self._events.get(command_id) is event:
                del self._events[command_id]

    async def async_recent(self, device_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """The box's latest commands, newest first."""
        return await self._run(self._recent_sync, device_id, limit)

    async def async_pending_count(self, device_id: str) -> int:
        """Commands waiting for the box's next poll."""
        return await self._run(self._pending_count_sync, device_id)

    async def async_forget_device(self, device_id: str) -> int:
        """Drop every command of a forgotten box; returns how many. A waiter
        on one of them wakes and reads it as unknown."""
        removed = await self._run(self._forget_device_sync, device_id)
        for command_id in removed:
            self._wake(command_id)
        return len(removed)
