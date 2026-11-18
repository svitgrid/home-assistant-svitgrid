"""Each inverter's solar string setup, as the app sends it (svitgrid D-2155).

The cloud keeps `solarConfig.strings` on the inverter document; Home Assistant
never receives that document. The island's string-outage alert needs each
string's `peakPowerWp`, so the app sends the strings to
`POST /api/svitgrid/island/solar-strings` (`island_lan.py`) when the owner saves
them and when a station moves to Home Assistant. Only the `id` and
`peakPowerWp` of each string are kept, with the inverter's name for the message.

An inverter the app never sent a setup for has none here, and its strings are
not checked.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = "svitgrid_solar_strings"
MAX_STRINGS = 16
MAX_ID_LENGTH = 40
MAX_NAME_LENGTH = 120
_SAVE_DELAY_S = 1


def valid_strings(raw: Any) -> list[dict[str, Any]] | None:
    """The strings cut to `id` and `peakPowerWp`, or None when malformed.

    Each entry is an object; `id`, when present, a string; `peakPowerWp`, when
    present, a number of at least 0. Both may be absent, as on the cloud's
    document, where the rule then names the string by position and never
    flags it.
    """
    if not isinstance(raw, list) or len(raw) > MAX_STRINGS:
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        kept: dict[str, Any] = {}
        if "id" in item:
            sid = item["id"]
            if not isinstance(sid, str) or not sid or len(sid) > MAX_ID_LENGTH:
                return None
            kept["id"] = sid
        if "peakPowerWp" in item:
            wp = item["peakPowerWp"]
            if isinstance(wp, bool) or not isinstance(wp, int | float) or not 0 <= wp < 1e7:
                return None
            kept["peakPowerWp"] = wp
        out.append(kept)
    return out


def valid_name(raw: Any) -> tuple[bool, str | None]:
    """`(ok, name)`: an absent or null name is fine and reads as None."""
    if raw is None:
        return True, None
    if not isinstance(raw, str) or len(raw) > MAX_NAME_LENGTH:
        return False, None
    return True, raw.strip() or None


class SolarStringsStore:
    """Loads and saves the string setups, keyed by inverter id."""

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
            for inverter_id, layout in (inverters or {}).items():
                if not isinstance(layout, dict):
                    continue
                strings = valid_strings(layout.get("strings"))
                ok, name = valid_name(layout.get("inverterName"))
                if strings and ok:
                    self._inverters[inverter_id] = {"strings": strings, "inverterName": name}

    async def flush(self) -> None:
        async with self._lock:
            await self._store.async_save(self._data())

    async def async_set(
        self, inverter_id: str, strings: list[dict[str, Any]], name: str | None
    ) -> None:
        """Keep `strings` for an inverter. An empty list drops it."""
        async with self._lock:
            if strings:
                self._inverters[inverter_id] = {"strings": strings, "inverterName": name}
            else:
                self._inverters.pop(inverter_id, None)
            self._store.async_delay_save(self._data, _SAVE_DELAY_S)

    def layout(self, inverter_id: str) -> dict[str, Any] | None:
        """`{"strings", "inverterName"}` for an inverter, or None."""
        layout = self._inverters.get(inverter_id)
        if layout is None:
            return None
        return {"strings": list(layout["strings"]), "inverterName": layout["inverterName"]}


async def solar_strings_store(hass) -> SolarStringsStore:
    """The one `SolarStringsStore`, created and loaded on first use."""
    domain = hass.data.setdefault(DOMAIN, {})
    store = domain.get("solar_strings")
    if not isinstance(store, SolarStringsStore):
        store = SolarStringsStore(hass)
        await store.load()
        domain["solar_strings"] = store
    return store
