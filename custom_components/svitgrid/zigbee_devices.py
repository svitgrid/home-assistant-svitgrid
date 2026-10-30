"""The Zigbee devices an adopted edge box reports, and its permit-join windows.

On an edge island the box posts its Zigbee roster here instead of the cloud
(`/api/v1/edge-devices/zigbee/device-joined` and `/state-update`, `edge_api.py`).
Home Assistant names its own devices (spec decision 26): a device's id is
`zb-` plus its IEEE address, lowercased, without separators, so the same
device keeps its id across boxes and rejoins. A device is stored when it
joins a box while that box's permit-join window is open (opened by the
`permit_join` route), and updated by the box's state reports after that.

SQLite in `zigbee_devices.db` beside `edge_commands.db`, the
`edge_command_queue.py` pattern: sync `_*_sync` methods that open, operate and
close the connection, run in HA's executor pool.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Tracks db_paths whose schema has already been created in this process.
_INITIALIZED: set[str] = set()

# The device fields a state report may set, by the column that holds them.
_COLUMNS = {
    "ieee": "ieee",
    "addr": "addr",
    "endpoint": "endpoint",
    "deviceId": "device_id",
    "on": "on_state",
    "reachable": "reachable",
    "unreachableSince": "unreachable_since",
    "caps": "caps",
    "model": "model",
    "manufacturer": "manufacturer",
    "name": "name",
    "room": "room",
    "addedAt": "added_at",
    "lastSeen": "last_seen",
}
_BOOL_FIELDS = ("on", "reachable")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def device_id_for_ieee(ieee: str) -> str:
    """`zb-` + the IEEE address lowercased, without separators (spec 26):
    `A4:C1:38:11:11:46:FF:FF` is `zb-a4c138111146ffff`."""
    hex_digits = re.sub(r"[^0-9a-f]", "", ieee.lower().removeprefix("0x"))
    return "zb-" + hex_digits


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
        CREATE TABLE IF NOT EXISTS zigbee_devices (
            id                TEXT PRIMARY KEY,
            ieee              TEXT NOT NULL,
            addr              TEXT NOT NULL,
            endpoint          INTEGER NOT NULL,
            device_id         TEXT NOT NULL,
            on_state          INTEGER,
            reachable         INTEGER NOT NULL DEFAULT 1,
            unreachable_since TEXT,
            caps              INTEGER,
            model             TEXT,
            manufacturer      TEXT,
            name              TEXT,
            room              TEXT,
            added_at          TEXT NOT NULL,
            last_seen         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_zigbee_devices_box_addr
            ON zigbee_devices (device_id, addr);
        CREATE TABLE IF NOT EXISTS zigbee_permit_join (
            device_id TEXT PRIMARY KEY,
            until     TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    device = {"id": row["id"]}
    for field, column in _COLUMNS.items():
        value = row[column]
        device[field] = bool(value) if field in _BOOL_FIELDS and value is not None else value
    return device


def _to_columns(fields: dict[str, Any]) -> dict[str, Any]:
    """`fields` with the booleans as SQLite stores them."""
    return {f: int(v) if f in _BOOL_FIELDS and v is not None else v for f, v in fields.items()}


class ZigbeeDeviceStore:
    """SQLite-backed Zigbee roster for adopted edge boxes.

    Pass *hass* to use HA's bounded ``async_add_executor_job`` pool; ``None``
    falls back to ``run_in_executor`` for tests.
    """

    def __init__(self, db_path: str, hass: HomeAssistant | None = None) -> None:
        self._db_path = db_path
        self._hass = hass

    # ── sync core ─────────────────────────────────────────────────────────────

    def _get_sync(self, device_id: str) -> dict[str, Any] | None:
        conn = _connect(self._db_path)
        try:
            row = conn.execute("SELECT * FROM zigbee_devices WHERE id = ?", (device_id,)).fetchone()
            return _row_to_dict(row) if row is not None else None
        finally:
            conn.close()

    def _find_on_box_sync(self, box_id: str, ieee: str | None, addr: str) -> dict | None:
        conn = _connect(self._db_path)
        try:
            if ieee:
                row = conn.execute(
                    "SELECT * FROM zigbee_devices WHERE id = ? AND device_id = ?",
                    (device_id_for_ieee(ieee), box_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM zigbee_devices WHERE device_id = ? AND addr = ? LIMIT 1",
                    (box_id, addr),
                ).fetchone()
            return _row_to_dict(row) if row is not None else None
        finally:
            conn.close()

    def _list_sync(self, box_id: str | None) -> list[dict[str, Any]]:
        conn = _connect(self._db_path)
        try:
            if box_id is None:
                rows = conn.execute("SELECT * FROM zigbee_devices ORDER BY added_at, id")
            else:
                rows = conn.execute(
                    "SELECT * FROM zigbee_devices WHERE device_id = ? ORDER BY added_at, id",
                    (box_id,),
                )
            return [_row_to_dict(r) for r in rows.fetchall()]
        finally:
            conn.close()

    def _insert_sync(self, device: dict[str, Any]) -> None:
        columns = ["id", *(_COLUMNS[f] for f in device if f != "id")]
        values = [device["id"], *(v for f, v in device.items() if f != "id")]
        conn = _connect(self._db_path)
        try:
            conn.execute(
                f"INSERT INTO zigbee_devices ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                values,
            )
            conn.commit()
        finally:
            conn.close()

    def _update_sync(self, device_id: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{_COLUMNS[f]} = ?" for f in fields)
        conn = _connect(self._db_path)
        try:
            conn.execute(
                f"UPDATE zigbee_devices SET {assignments} WHERE id = ?",
                [*fields.values(), device_id],
            )
            conn.commit()
        finally:
            conn.close()

    def _open_permit_join_sync(self, box_id: str, until: str) -> None:
        conn = _connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO zigbee_permit_join (device_id, until) VALUES (?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET until = excluded.until",
                (box_id, until),
            )
            conn.commit()
        finally:
            conn.close()

    def _permit_join_until_sync(self, box_id: str) -> str | None:
        conn = _connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT until FROM zigbee_permit_join WHERE device_id = ?", (box_id,)
            ).fetchone()
            return row["until"] if row is not None else None
        finally:
            conn.close()

    # ── async wrappers ────────────────────────────────────────────────────────

    async def _run(self, func, *args):
        if self._hass is not None:
            return await self._hass.async_add_executor_job(func, *args)
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    async def async_get(self, device_id: str) -> dict[str, Any] | None:
        """The device with this `zb-` id, or None."""
        return await self._run(self._get_sync, device_id)

    async def async_find_on_box(self, box_id: str, ieee: str | None, addr: str) -> dict | None:
        """The box's device by IEEE, or by short address when the report has
        no IEEE (the short address changes on every rejoin)."""
        return await self._run(self._find_on_box_sync, box_id, ieee, addr)

    async def async_list(self, box_id: str | None = None) -> list[dict[str, Any]]:
        """Every device, or the box's, oldest first."""
        return await self._run(self._list_sync, box_id)

    async def async_add(self, box_id: str, ieee: str, addr: str, endpoint: int) -> dict:
        """Store a device that joined `box_id` inside its permit-join window."""
        device = {
            "id": device_id_for_ieee(ieee),
            "ieee": ieee,
            "addr": addr,
            "endpoint": endpoint,
            "deviceId": box_id,
            "reachable": True,
            "addedAt": _iso(_utcnow()),
            "lastSeen": _iso(_utcnow()),
        }
        await self._run(self._insert_sync, _to_columns(device))
        return await self.async_get(device["id"])

    async def async_update(self, device_id: str, fields: dict[str, Any]) -> None:
        """Set `fields` (the device's keys, as `async_get` returns them)."""
        await self._run(self._update_sync, device_id, _to_columns(fields))

    async def async_open_permit_join(self, box_id: str, duration_s: int) -> str:
        """Open the box's permit-join window for `duration_s` seconds; returns
        when it closes."""
        until = _iso(_utcnow() + timedelta(seconds=duration_s))
        await self._run(self._open_permit_join_sync, box_id, until)
        return until

    async def async_permit_join_open(self, box_id: str) -> bool:
        """Whether the box's permit-join window is open now."""
        until = await self._run(self._permit_join_until_sync, box_id)
        return until is not None and until > _iso(_utcnow())


def now_iso() -> str:
    """Now, as the store writes its timestamps."""
    return _iso(_utcnow())
