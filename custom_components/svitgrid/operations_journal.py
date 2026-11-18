"""The journal of what Svitgrid told the station to do, kept on Home Assistant.

On an island (true island and edge island) the cloud's operations feed has
nothing to read, so this store keeps it locally, one row per operation, in the
shape the app's ``HouseholdOperationEntry`` reads:

- an inverter command from the app over the LAN (``/api/svitgrid/commands``),
- an inverter command a rule or calendar event sent, and a rule run skipped,
- a board command the edge queue held, mirrored as its state changes.

``GET /api/svitgrid/operations`` (http_views.SvitgridOperationsView) pages it
newest first. ``nextBefore`` is an opaque cursor (the row's sequence number),
echoed back as ``?before=``, so paging neither repeats nor skips a row. Rows
older than ``RETENTION_DAYS`` are pruned on every write and read.

Mirrors the ``island_event_store.py`` pattern: sync ``_*_sync`` methods that
open, operate and close the connection, run in HA's executor pool. A failure
to journal never fails the operation it records: callers use ``record_safely``.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

RETENTION_DAYS = 90
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

SOURCE_INVERTER = "inverter"
SOURCE_EDGE_DEVICE = "edge_device"

RETENTION_BY_SOURCE = {SOURCE_INVERTER: RETENTION_DAYS, SOURCE_EDGE_DEVICE: RETENTION_DAYS}

# Tracks db_paths whose schema has already been created in this process.
_INITIALIZED: set[str] = set()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    """Now, in the journal's timestamp format."""
    return _iso(_utcnow())


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"))


def _loads_map(text: str | None) -> dict | None:
    if not text:
        return None
    value = json.loads(text)
    return value if isinstance(value, dict) else {"value": value}


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
        CREATE TABLE IF NOT EXISTS operations (
            seq                 INTEGER PRIMARY KEY AUTOINCREMENT,
            id                  TEXT NOT NULL UNIQUE,
            source              TEXT NOT NULL,
            object_id           TEXT NOT NULL,
            object_name         TEXT,
            command             TEXT NOT NULL,
            status              TEXT NOT NULL,
            payload_json        TEXT,
            result_json         TEXT,
            created_by          TEXT,
            created_at          TEXT NOT NULL,
            executed_at         TEXT,
            calendar_event_name TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_operations_created ON operations (created_at);
        CREATE INDEX IF NOT EXISTS idx_operations_object ON operations (object_id, seq);
        """
    )
    conn.commit()


def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source": row["source"],
        "objectId": row["object_id"],
        "objectName": row["object_name"],
        "command": row["command"],
        "status": row["status"],
        "payload": _loads_map(row["payload_json"]),
        "result": _loads_map(row["result_json"]),
        "createdBy": row["created_by"],
        "createdAt": row["created_at"],
        "executedAt": row["executed_at"],
        "calendarEventName": row["calendar_event_name"],
    }


class OperationsJournal:
    """SQLite-backed journal of operations, newest first.

    Pass *hass* to use HA's bounded ``async_add_executor_job`` pool; ``None``
    falls back to ``run_in_executor`` for tests.
    """

    def __init__(self, db_path: str, hass: HomeAssistant | None = None) -> None:
        self._db_path = db_path
        self._hass = hass

    # ── sync core ─────────────────────────────────────────────────────────────

    def _prune_sync(self, conn: sqlite3.Connection, now: datetime) -> None:
        cutoff = _iso(now - timedelta(days=RETENTION_DAYS))
        conn.execute("DELETE FROM operations WHERE created_at < ?", (cutoff,))

    def record_sync(
        self,
        *,
        source: str,
        object_id: str,
        command: str,
        status: str,
        payload: Any = None,
        result: Any = None,
        created_by: str | None = None,
        object_name: str | None = None,
        calendar_event_name: str | None = None,
        executed_at: str | None = None,
        op_id: str | None = None,
    ) -> str:
        """Insert a row, or, for an *op_id* already held, update its status,
        result and executedAt (a board command as the box answers it). The
        row keeps its place in the feed. Returns the row's id."""
        op_id = op_id or str(uuid.uuid4())
        now = _utcnow()
        conn = _connect(self._db_path)
        try:
            self._prune_sync(conn, now)
            conn.execute(
                "INSERT INTO operations (id, source, object_id, object_name, command, "
                "status, payload_json, result_json, created_by, created_at, executed_at, "
                "calendar_event_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status = excluded.status, "
                "result_json = excluded.result_json, executed_at = excluded.executed_at",
                (
                    op_id,
                    source,
                    object_id,
                    object_name,
                    command,
                    status,
                    _dumps(payload),
                    _dumps(result),
                    created_by,
                    _iso(now),
                    executed_at,
                    calendar_event_name,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return op_id

    def _list_sync(
        self, before: int | None, limit: int, object_id: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        conn = _connect(self._db_path)
        try:
            self._prune_sync(conn, _utcnow())
            conn.commit()
            clauses: list[str] = []
            args: list[Any] = []
            if before is not None:
                clauses.append("seq < ?")
                args.append(before)
            if object_id:
                clauses.append("object_id = ?")
                args.append(object_id)
            where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM operations {where}ORDER BY seq DESC LIMIT ?",  # noqa: S608
                (*args, limit + 1),
            ).fetchall()
        finally:
            conn.close()
        page = rows[:limit]
        next_before = str(page[-1]["seq"]) if len(rows) > limit else None
        return [_row_to_entry(r) for r in page], next_before

    # ── async wrappers ────────────────────────────────────────────────────────

    async def _run(self, func, *args, **kwargs):
        call = functools.partial(func, *args, **kwargs)
        if self._hass is not None:
            return await self._hass.async_add_executor_job(call)
        return await asyncio.get_running_loop().run_in_executor(None, call)

    async def async_record(self, **fields: Any) -> str:
        """Record one operation; see ``record_sync``."""
        return await self._run(self.record_sync, **fields)

    async def async_list(
        self,
        *,
        before: str | None = None,
        limit: int = DEFAULT_LIMIT,
        object_id: str | None = None,
    ) -> dict[str, Any]:
        """One page of the feed, newest first, in the cloud's shape.

        Raises ``ValueError`` for a *before* this journal did not hand out.
        """
        cursor = None
        if before:
            if not before.isdigit():
                raise ValueError("before")
            cursor = int(before)
        limit = max(1, min(int(limit), MAX_LIMIT))
        operations, next_before = await self._run(self._list_sync, cursor, limit, object_id)
        return {
            "operations": operations,
            "nextBefore": next_before,
            "retentionDays": dict(RETENTION_BY_SOURCE),
        }


async def record_safely(journal: OperationsJournal | None, **fields: Any) -> None:
    """Record an operation; a missing journal or a failed write only logs."""
    if journal is None:
        return
    try:
        await journal.async_record(**fields)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Could not journal operation %s", fields.get("command"), exc_info=True)
