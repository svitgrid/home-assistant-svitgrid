"""The Zigbee devices an adopted edge box reports, and its permit-join windows.

On an edge island the box posts its Zigbee roster here instead of the cloud
(`/api/v1/edge-devices/zigbee/device-joined` and `/state-update`, `edge_api.py`).
Home Assistant names its own devices (spec decision 26): a device's id is
`zb-` plus its IEEE address, lowercased, without separators, so the same
device keeps its id across boxes and rejoins. A device is stored when it
joins a box while that box's permit-join window is open (opened by the
`permit_join` route), and updated by the box's state reports after that.

Its readings are kept here too, as the cloud keeps them under
`smartDevices/{id}/readings`: a plug's power, voltage, current and energy,
scaled by its model's profile (`zigbee_device_profiles.json`, vendored from
the monorepo by `scripts/sync-edge-command-fixtures.sh`), and a sensor's
temperature, humidity and battery. A 5-minute usage rollup mirrors the
cloud's `smartUsage_5min`. Home Assistant is one household, so the rollup is
keyed by slot and box only.

SQLite in `zigbee_devices.db` beside `edge_commands.db`, the
`edge_command_queue.py` pattern: sync `_*_sync` methods that open, operate and
close the connection, run in HA's executor pool.
"""

from __future__ import annotations

import asyncio
import functools
import json
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Tracks db_paths whose schema has already been created in this process.
_INITIALIZED: set[str] = set()

_PROFILES_PATH = Path(__file__).parent / "zigbee_device_profiles.json"
_FIVE_MIN = timedelta(minutes=5)

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
    "applianceType": "appliance_type",
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
            appliance_type    TEXT,
            added_at          TEXT NOT NULL,
            last_seen         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_zigbee_devices_box_addr
            ON zigbee_devices (device_id, addr);
        CREATE TABLE IF NOT EXISTS zigbee_permit_join (
            device_id TEXT PRIMARY KEY,
            until     TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS zigbee_socket_readings (
            zigbee_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            power     REAL NOT NULL,
            voltage   REAL NOT NULL,
            current   REAL NOT NULL,
            energy    REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_zigbee_socket_readings
            ON zigbee_socket_readings (zigbee_id, timestamp);
        CREATE TABLE IF NOT EXISTS zigbee_sensor_readings (
            zigbee_id   TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            age_ms      INTEGER NOT NULL,
            temperature INTEGER,
            humidity    INTEGER,
            battery     INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_zigbee_sensor_readings
            ON zigbee_sensor_readings (zigbee_id, captured_at);
        CREATE TABLE IF NOT EXISTS zigbee_usage_5min (
            period_start TEXT NOT NULL,
            box_id       TEXT NOT NULL,
            avg_power_w  INTEGER NOT NULL,
            device_count INTEGER NOT NULL,
            devices_json TEXT NOT NULL,
            PRIMARY KEY (period_start, box_id)
        );
        CREATE TABLE IF NOT EXISTS zigbee_history (
            zigbee_id  TEXT NOT NULL,
            timestamp  TEXT NOT NULL,
            event      TEXT NOT NULL,
            state_json TEXT,
            command_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_zigbee_history
            ON zigbee_history (zigbee_id, timestamp);
        """
    )
    # A database written before `appliance_type` existed gains it here.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(zigbee_devices)")}
    if "appliance_type" not in columns:
        conn.execute("ALTER TABLE zigbee_devices ADD COLUMN appliance_type TEXT")
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


@functools.cache
def load_zigbee_profiles() -> dict[str, Any]:
    """The vendored `config/zigbeeDeviceProfiles`, by Zigbee model. Read in the
    executor (`_store_socket_readings_sync`), never on the event loop."""
    profiles = json.loads(_PROFILES_PATH.read_text())
    return {k: v for k, v in profiles.items() if isinstance(v, dict)}


def resolve_zigbee_profile(
    profiles: dict[str, Any] | None, model: str | None, manufacturer: str | None
) -> dict[str, Any] | None:
    """The profile for one device, with its manufacturer's override applied,
    as `zigbee-device-profiles.ts` `resolveZigbeeProfile`: one model string can
    name both a metering and a non-metering relay (#1075)."""
    if not profiles or not model:
        return None
    base = profiles.get(model)
    if not isinstance(base, dict):
        return None
    override = (base.get("manufacturers") or {}).get(manufacturer) if manufacturer else None
    return {**base, **override} if isinstance(override, dict) else base


def _js_round(value: float) -> int:
    """`Math.round`: halves round up, as the cloud rounds the rollup."""
    return math.floor(value + 0.5)


def _divisor(profile: dict | None, key: str) -> float:
    """The profile's divisor, or 1 when it has none (or 0), as the cloud's `|| 1`."""
    value = (profile or {}).get(key)
    return value if isinstance(value, int | float) and not isinstance(value, bool) and value else 1


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def five_min_slot(timestamp: str) -> str:
    """The start of the timestamp's 5-minute slot (`fiveMinSlotIso`)."""
    dt = _parse_iso(timestamp)
    return _iso(dt.replace(minute=dt.minute - dt.minute % 5, second=0, microsecond=0))


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

    def _store_socket_readings_sync(
        self, box_id: str, timestamp: str, readings: list[dict], profiles: dict | None
    ) -> int:
        """`processSocketReadings` (`socket-readings-ingest.ts`): each reading of
        a plug the box holds, scaled by its profile, then the box's snapshot in
        the slot's rollup. A non-metering plug and an unknown address are
        skipped. Returns how many were stored."""
        if profiles is None:
            profiles = load_zigbee_profiles()
        conn = _connect(self._db_path)
        try:
            samples: dict[str, dict] = {}
            for reading in readings:
                row = conn.execute(
                    "SELECT * FROM zigbee_devices WHERE device_id = ? AND addr = ? LIMIT 1",
                    (box_id, reading["addr"]),
                ).fetchone()
                if row is None:
                    continue
                device = _row_to_dict(row)
                model = reading.get("model") or device["model"]
                profile = resolve_zigbee_profile(profiles, model, device["manufacturer"])
                if profile is not None and not profile.get("metering"):
                    continue
                scaled = {
                    key: reading[key] / _divisor(profile, f"{key}Divisor")
                    for key in ("power", "voltage", "current", "energy")
                }
                conn.execute(
                    "INSERT INTO zigbee_socket_readings "
                    "(zigbee_id, timestamp, power, voltage, current, energy) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (device["id"], timestamp, *scaled.values()),
                )
                # A reading proves the plug reachable.
                update = {"reachable": 1, "unreachable_since": None, "last_seen": _iso(_utcnow())}
                if reading.get("model") and not device["model"]:
                    update["model"] = reading["model"]
                conn.execute(
                    f"UPDATE zigbee_devices SET {', '.join(f'{c} = ?' for c in update)} "
                    "WHERE id = ?",
                    [*update.values(), device["id"]],
                )
                samples[device["id"]] = {
                    "avgPowerW": scaled["power"],
                    "energyCounterWh": scaled["energy"],
                }
            if samples:
                # Each ingest is a full snapshot of the box's plugs: it
                # replaces the box's earlier snapshot in the slot.
                conn.execute(
                    "INSERT OR REPLACE INTO zigbee_usage_5min "
                    "(period_start, box_id, avg_power_w, device_count, devices_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        five_min_slot(timestamp),
                        box_id,
                        _js_round(sum(s["avgPowerW"] for s in samples.values())),
                        len(samples),
                        json.dumps(
                            {
                                k: {**v, "avgPowerW": _js_round(v["avgPowerW"])}
                                for k, v in samples.items()
                            }
                        ),
                    ),
                )
            conn.commit()
            return len(samples)
        finally:
            conn.close()

    def _store_sensor_readings_sync(
        self, box_id: str, readings: list[dict], received_at: datetime
    ) -> tuple[int, int]:
        conn = _connect(self._db_path)
        stored = ignored = 0
        try:
            for reading in readings:
                row = conn.execute(
                    "SELECT id FROM zigbee_devices WHERE device_id = ? AND addr = ? LIMIT 1",
                    (box_id, reading["addr"]),
                ).fetchone()
                if row is None:
                    # Never created from a reading: that races pairing and
                    # resurrects a device the owner removed.
                    ignored += 1
                    continue
                captured_at = received_at - timedelta(milliseconds=reading["ageMs"])
                conn.execute(
                    "INSERT INTO zigbee_sensor_readings (zigbee_id, captured_at, received_at, "
                    "age_ms, temperature, humidity, battery) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["id"],
                        _iso(captured_at),
                        _iso(received_at),
                        reading["ageMs"],
                        reading.get("temperature"),
                        reading.get("humidity"),
                        reading.get("battery"),
                    ),
                )
                conn.execute(
                    "UPDATE zigbee_devices SET last_seen = ? WHERE id = ?",
                    (_iso(received_at), row["id"]),
                )
                stored += 1
            conn.commit()
            return stored, ignored
        finally:
            conn.close()

    def _socket_readings_sync(self, device_id: str) -> list[dict]:
        conn = _connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT timestamp, power, voltage, current, energy FROM zigbee_socket_readings "
                "WHERE zigbee_id = ? ORDER BY timestamp, rowid",
                (device_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _sensor_readings_sync(self, device_id: str) -> list[dict]:
        conn = _connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT captured_at, received_at, age_ms, temperature, humidity, battery "
                "FROM zigbee_sensor_readings WHERE zigbee_id = ? ORDER BY captured_at, rowid",
                (device_id,),
            ).fetchall()
            return [
                {
                    "capturedAt": r["captured_at"],
                    "receivedAt": r["received_at"],
                    "ageMs": r["age_ms"],
                    "temperature": r["temperature"],
                    "humidity": r["humidity"],
                    "battery": r["battery"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def _usage_5min_sync(self, period_start: str) -> dict | None:
        conn = _connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM zigbee_usage_5min WHERE period_start = ? ORDER BY box_id",
                (period_start,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        devices: dict[str, dict] = {}
        for r in rows:
            devices.update(json.loads(r["devices_json"]))
        return {
            "avgPowerW": sum(r["avg_power_w"] for r in rows),
            "deviceCount": sum(r["device_count"] for r in rows),
            "periodStart": period_start,
            "periodEnd": _iso(_parse_iso(period_start) + _FIVE_MIN),
            "boards": {
                r["box_id"]: {"avgPowerW": r["avg_power_w"], "deviceCount": r["device_count"]}
                for r in rows
            },
            "devices": devices,
        }

    def _remove_sync(self, device_id: str) -> None:
        conn = _connect(self._db_path)
        try:
            for table, column in (
                ("zigbee_devices", "id"),
                ("zigbee_socket_readings", "zigbee_id"),
                ("zigbee_sensor_readings", "zigbee_id"),
                ("zigbee_history", "zigbee_id"),
            ):
                conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (device_id,))
            conn.commit()
        finally:
            conn.close()

    def _latest_sync(self, device_id: str) -> tuple[dict | None, dict | None]:
        conn = _connect(self._db_path)
        try:
            socket = conn.execute(
                "SELECT timestamp, power, voltage, current, energy FROM zigbee_socket_readings "
                "WHERE zigbee_id = ? ORDER BY timestamp DESC, rowid DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            sensor = conn.execute(
                "SELECT captured_at, temperature, humidity, battery FROM zigbee_sensor_readings "
                "WHERE zigbee_id = ? ORDER BY captured_at DESC, rowid DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            return (dict(socket) if socket else None, dict(sensor) if sensor else None)
        finally:
            conn.close()

    def _add_history_sync(self, device_id: str, event: str, state: dict, command_id: str) -> None:
        conn = _connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO zigbee_history (zigbee_id, timestamp, event, state_json, command_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (device_id, _iso(_utcnow()), event, json.dumps(state), command_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _history_sync(self, device_id: str, limit: int) -> list[dict]:
        conn = _connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT rowid, * FROM zigbee_history WHERE zigbee_id = ? "
                "ORDER BY timestamp DESC, rowid DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
            return [
                {
                    "id": str(r["rowid"]),
                    "timestamp": r["timestamp"],
                    "event": r["event"],
                    "state": json.loads(r["state_json"]) if r["state_json"] else None,
                    "commandId": r["command_id"],
                    "triggeredBy": None,
                }
                for r in rows
            ]
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

    async def async_store_socket_readings(
        self, box_id: str, timestamp: str, readings: list[dict], profiles: dict | None = None
    ) -> int:
        """Store the box's plug readings taken at `timestamp`, scaled by each
        plug's profile (the vendored ones unless `profiles` is given), and
        roll them into the slot. Returns how many were stored."""
        return await self._run(
            self._store_socket_readings_sync, box_id, timestamp, readings, profiles
        )

    async def async_store_sensor_readings(
        self, box_id: str, readings: list[dict], received_at: datetime | None = None
    ) -> tuple[int, int]:
        """Store the box's sensor readings, each captured `ageMs` before
        `received_at`. Returns (stored, ignored)."""
        return await self._run(
            self._store_sensor_readings_sync, box_id, readings, received_at or _utcnow()
        )

    async def async_socket_readings(self, device_id: str) -> list[dict]:
        """The plug's stored readings, oldest first."""
        return await self._run(self._socket_readings_sync, device_id)

    async def async_sensor_readings(self, device_id: str) -> list[dict]:
        """The sensor's stored readings, oldest first."""
        return await self._run(self._sensor_readings_sync, device_id)

    async def async_usage_5min(self, period_start: str) -> dict | None:
        """The slot's usage in `smartUsage_5min`'s shape: each box's latest
        snapshot under `boards`, their sum at the top, and every plug's
        sample under `devices`. None for a slot with no metering plug."""
        return await self._run(self._usage_5min_sync, period_start)

    async def async_remove(self, device_id: str) -> None:
        """Forget the device, its readings and its history."""
        await self._run(self._remove_sync, device_id)

    async def async_latest(self, device_id: str) -> tuple[dict | None, dict | None]:
        """The device's latest plug reading and latest sensor reading."""
        return await self._run(self._latest_sync, device_id)

    async def async_add_history(
        self, device_id: str, event: str, state: dict, command_id: str
    ) -> None:
        """Record a history event (`command_sent`), as the cloud's
        `smartDevices/{id}/history`."""
        await self._run(self._add_history_sync, device_id, event, state, command_id)

    async def async_history(self, device_id: str, limit: int = 50) -> list[dict]:
        """The device's history, newest first."""
        return await self._run(self._history_sync, device_id, limit)

    async def async_permit_join_open(self, box_id: str) -> bool:
        """Whether the box's permit-join window is open now."""
        until = await self._run(self._permit_join_until_sync, box_id)
        return until is not None and until > _iso(_utcnow())


def now_iso() -> str:
    """Now, as the store writes its timestamps."""
    return _iso(_utcnow())
