"""Each inverter's battery maintenance settings, as the app sends them (svitgrid D-2163).

The cloud keeps `batteryMaintenance` on the inverter document and decides from
it whether a maintenance charge is due (`maintenance-due.ts`
`isMaintenanceDue`). Home Assistant never receives that document. Without it a
daily `battery_maintenance_charge` event copied from the cloud charged on every
occurrence instead of every `intervalDays`. The app sends the settings to
`POST /api/svitgrid/island/battery-maintenance` (`island_lan.py`) when the
owner saves them and when a station moves to Home Assistant.

Home Assistant then records each run's date itself, as the cloud's
`stampMaintenanceLastRun` does: a completed run sets `lastRunAt` and clears a
forced date, an incomplete one only sets the outcome. A later send from the app
carries the cloud's copy, which on an island is older, so it never moves the
last run back.

An inverter the app never sent settings for has none here, and the evaluator
holds its recurring maintenance event rather than charging every day.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = "svitgrid_battery_maintenance"
_SAVE_DELAY_S = 1

OUTCOMES = ("completed", "skipped", "incomplete")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The settings kept, in the cloud's names (BatteryMaintenanceSchema).
SETTINGS_KEYS = (
    "enabled",
    "intervalDays",
    "lastRunAt",
    "lastRunOutcome",
    "gridFallbackOffsetMinutes",
    "fillStartTime",
    "drainEnabled",
    "drainStartTime",
    "drainEndTime",
    "drainTargetSocPercent",
    "forcedNextRunDate",
)


def parse_instant(value: Any) -> datetime | None:
    """An ISO instant as an aware UTC datetime, or None when it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def iso_instant(at: datetime) -> str:
    """`at` as JavaScript's `toISOString` writes it: UTC, milliseconds, `Z`."""
    utc = at.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def days_between_utc(a: datetime, b: datetime) -> int:
    """Whole UTC calendar days from `a` to `b` (daysBetweenUtc)."""
    return (b.astimezone(UTC).date() - a.astimezone(UTC).date()).days


def is_maintenance_due(
    *,
    enabled: bool,
    interval_days: int,
    last_run_at: str | None,
    today: datetime,
    forced_next_run_date: str | None = None,
    today_local_date_str: str | None = None,
) -> bool:
    """Port of the cloud's isMaintenanceDue. A null `last_run_at` is due.

    A forced date equal to today's local date forces a run, and a future one
    holds the interval rule off until it comes. A past forced date neither
    forces nor blocks. The shared cases in
    tests/fixtures/maintenance_due_cases.json pin it to the cloud's.
    """
    if not enabled:
        return False
    if forced_next_run_date and today_local_date_str:
        if forced_next_run_date == today_local_date_str:
            return True
        if forced_next_run_date > today_local_date_str:
            return False
    if last_run_at is None:
        return True
    last = parse_instant(last_run_at)
    if last is None:
        return True
    return days_between_utc(last, today) >= interval_days


def _int_in(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _optional(raw: dict, key: str, ok) -> tuple[bool, Any]:
    value = raw.get(key)
    if value is None:
        return True, None
    return (True, value) if ok(value) else (False, None)


def valid_settings(raw: Any) -> dict[str, Any] | None:
    """The settings cut to the cloud's fields, or None when the cloud would
    refuse them (BatteryMaintenanceSchema's ranges and formats)."""
    if not isinstance(raw, dict):
        return None
    if not isinstance(raw.get("enabled"), bool):
        return None
    if not _int_in(raw.get("intervalDays"), 7, 90):
        return None
    if not _int_in(raw.get("gridFallbackOffsetMinutes"), 0, 360):
        return None
    out: dict[str, Any] = {
        "enabled": raw["enabled"],
        "intervalDays": raw["intervalDays"],
        "gridFallbackOffsetMinutes": raw["gridFallbackOffsetMinutes"],
    }
    checks = {
        "lastRunAt": lambda v: parse_instant(v) is not None,
        "lastRunOutcome": lambda v: v in OUTCOMES,
        "fillStartTime": lambda v: isinstance(v, str) and bool(_HHMM.match(v)),
        "drainStartTime": lambda v: isinstance(v, str) and bool(_HHMM.match(v)),
        "drainEndTime": lambda v: isinstance(v, str) and bool(_HHMM.match(v)),
        "drainTargetSocPercent": lambda v: _int_in(v, 5, 95),
        "forcedNextRunDate": lambda v: isinstance(v, str) and bool(_YMD.match(v)),
    }
    for key, check in checks.items():
        ok, value = _optional(raw, key, check)
        if not ok:
            return None
        out[key] = value
    drain = raw.get("drainEnabled", False)
    if drain is None:
        drain = False
    if not isinstance(drain, bool):
        return None
    out["drainEnabled"] = drain
    return {key: out[key] for key in SETTINGS_KEYS}


def _merge(stored: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """`incoming` from the app, keeping a later run Home Assistant recorded.

    The app sends the cloud's copy, whose last run is older on an island. That
    copy's forced date is one the recorded completed run already served, so a
    forced date on or before that run's date goes with it.
    """
    if stored is None:
        return incoming
    stored_at = parse_instant(stored.get("lastRunAt"))
    incoming_at = parse_instant(incoming.get("lastRunAt"))
    if stored_at is None or (incoming_at is not None and incoming_at >= stored_at):
        return incoming
    merged = {
        **incoming,
        "lastRunAt": stored["lastRunAt"],
        "lastRunOutcome": stored.get("lastRunOutcome"),
    }
    forced = merged.get("forcedNextRunDate")
    if (
        forced
        and stored.get("lastRunOutcome") == "completed"
        and forced <= stored_at.date().isoformat()
    ):
        merged["forcedNextRunDate"] = None
    return merged


class BatteryMaintenanceStore:
    """Loads and saves the maintenance settings, keyed by inverter id."""

    def __init__(self, hass) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()
        self._inverters: dict[str, dict[str, Any]] = {}

    def _data(self) -> dict[str, Any]:
        return {"inverters": self._inverters}

    async def load(self) -> None:
        async with self._lock:
            raw = await self._store.async_load()
            inverters = raw.get("inverters") if isinstance(raw, dict) else None
            self._inverters = {}
            for inverter_id, settings in (inverters or {}).items():
                kept = valid_settings(settings)
                if kept is not None:
                    self._inverters[inverter_id] = kept

    async def flush(self) -> None:
        async with self._lock:
            await self._store.async_save(self._data())

    async def async_set(self, inverter_id: str, settings: dict[str, Any] | None) -> dict | None:
        """Keep the app's `settings` for an inverter; None drops them. Returns what is kept."""
        async with self._lock:
            if settings is None:
                self._inverters.pop(inverter_id, None)
                kept = None
            else:
                kept = _merge(self._inverters.get(inverter_id), dict(settings))
                self._inverters[inverter_id] = kept
            self._store.async_delay_save(self._data, _SAVE_DELAY_S)
            return dict(kept) if kept is not None else None

    async def async_record_run(self, inverter_id: str, at: datetime, outcome: str) -> None:
        """Record a run's outcome, as the cloud's stampMaintenanceLastRun does.

        Only a completed run sets `lastRunAt`, and it clears a forced date it
        served. An inverter with no settings has nothing to record against.
        """
        async with self._lock:
            settings = self._inverters.get(inverter_id)
            if settings is None or outcome not in OUTCOMES:
                return
            settings = dict(settings)
            settings["lastRunOutcome"] = outcome
            if outcome == "completed":
                settings["lastRunAt"] = iso_instant(at)
                settings["forcedNextRunDate"] = None
            self._inverters[inverter_id] = settings
            self._store.async_delay_save(self._data, _SAVE_DELAY_S)

    def settings(self, inverter_id: str) -> dict[str, Any] | None:
        """The settings kept for an inverter, or None."""
        settings = self._inverters.get(inverter_id)
        return dict(settings) if settings is not None else None


async def battery_maintenance_store(hass) -> BatteryMaintenanceStore:
    """The one `BatteryMaintenanceStore`, created and loaded on first use."""
    domain = hass.data.setdefault(DOMAIN, {})
    store = domain.get("battery_maintenance")
    if not isinstance(store, BatteryMaintenanceStore):
        store = BatteryMaintenanceStore(hass)
        await store.load()
        domain["battery_maintenance"] = store
    return store
