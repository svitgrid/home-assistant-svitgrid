"""What the island liveness check reads from Home Assistant (svitgrid D-2149).

One entry per island household: its inverters with the time of their latest
stored reading, and its adopted edge boxes with their `lastContactAt` and
whether the owner paused them. A household whose cloud copy is on is left out:
the cloud's offline sweep covers it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..const import DOMAIN
from ..island_lan import _cloud_copy_of
from ..solar_strings import solar_strings_store
from .runtime import household_key
from .summaries import summary_totals

# A board is paused from its last acked `pause` until its next acked `resume`.
_PAUSE_COMMANDS = ("pause", "resume")


def _ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def entry_for_inverter(hass, store_entry, inverter_id: str):
    """The config entry an inverter's household belongs to.

    The entry that lists the inverter, else the entry whose store took the
    reading: an adopted edge box's inverters are listed by no entry, and their
    readings land in the first entry's store (`edge_api._entry_state`).
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if any(i.get("inverter_id") == inverter_id for i in entry.data.get("inverters") or []):
            return entry
    return store_entry


async def _paused(states: list[dict], device_id: str) -> bool:
    last = None
    for state in states:
        queue = state.get("edge_queue")
        if queue is None:
            continue
        found = await queue.async_last_success(device_id, _PAUSE_COMMANDS)
        if found is not None:
            last = found
    return last == "pause"


async def island_liveness(hass) -> list[dict]:
    """The liveness check's view of every island household."""
    domain = hass.data.get(DOMAIN) or {}
    entries = hass.config_entries.async_entries(DOMAIN)
    states = [(e, domain.get(e.entry_id)) for e in entries]
    states = [(e, s) for e, s in states if isinstance(s, dict) and s.get("store") is not None]
    if not states:
        return []
    first_entry, first_state = states[0]

    keystore = domain.get("keystore")
    ks = await keystore.load() if keystore is not None else None
    boxes = ks.edge_boxes_list() if ks is not None else []
    board_of = {i: box.device_id for box in boxes for i in box.inverter_ids}

    households: dict[str, dict] = {}

    def household(entry) -> dict | None:
        if _cloud_copy_of(entry):
            return None
        key = household_key(entry)
        return households.setdefault(key, {"householdId": key, "inverters": [], "boards": []})

    latest: dict[str, int] = {}
    for _, state in states:
        for row in await state["store"].live_snapshot():
            at = _ms(row.get("ts"))
            inverter_id = row.get("inverterId")
            if at is not None and isinstance(inverter_id, str):
                latest[inverter_id] = max(at, latest.get(inverter_id, at))

    known: dict[str, Any] = {}
    for entry, _ in states:
        for inv in entry.data.get("inverters") or []:
            if isinstance(inv.get("inverter_id"), str):
                known[inv["inverter_id"]] = entry
    for inverter_id in board_of:
        known.setdefault(inverter_id, entry_for_inverter(hass, first_entry, inverter_id))

    for inverter_id, entry in known.items():
        hh = household(entry)
        if hh is not None:
            hh["inverters"].append(
                {
                    "id": inverter_id,
                    "lastReadingMs": latest.get(inverter_id),
                    "boardId": board_of.get(inverter_id),
                }
            )

    edge_states = [s for _, s in states if s.get("edge_queue") is not None]
    for box in boxes:
        owner = (
            entry_for_inverter(hass, first_entry, box.inverter_ids[0])
            if box.inverter_ids
            else first_entry
        )
        hh = household(owner)
        if hh is None or box.api_key is None:
            continue  # a box that never bound has never reported
        contact = await first_state["store"].get_edge_box_state(box.device_id) or {}
        hh["boards"].append(
            {
                "deviceId": box.device_id,
                "lastContactMs": _ms(contact.get("lastContactAt")),
                "paused": await _paused(edge_states, box.device_id),
                "inverterIds": list(box.inverter_ids),
            }
        )

    return list(households.values())


async def island_summary_totals(hass, household_id: str, from_day: str, to_day: str):
    """An island household's energy over local days `from_day`..`to_day`, from
    the sealed daily rollups of every store (svitgrid D-2152). None when no day
    holds energy."""
    households = await island_liveness(hass)
    inverters = [
        inv["id"]
        for hh in households
        if hh["householdId"] == household_id
        for inv in hh["inverters"]
    ]
    stores = _stores(hass)
    rows = []
    for inverter_id in inverters:
        found: list[dict] = []
        for store in stores:
            found += await store.history_range(inverter_id, from_day, to_day)
        rows.append(found)
    return summary_totals(rows)


def _stores(hass) -> list:
    domain = hass.data.get(DOMAIN) or {}
    return [
        s["store"]
        for e in hass.config_entries.async_entries(DOMAIN)
        if isinstance(s := domain.get(e.entry_id), dict) and s.get("store") is not None
    ]


async def island_string_inputs(hass, since_ms: int) -> list[dict]:
    """Every island inverter with a string setup from the app, and the PV
    power of its readings since `since_ms` in every store (svitgrid D-2155).

    An inverter the app sent no setup for, or one with a single string, is
    left out before any reading is read.
    """
    layouts = await solar_strings_store(hass)
    since = (
        datetime.fromtimestamp(since_ms / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    stores = _stores(hass)
    out = []
    for hh in await island_liveness(hass):
        for inv in hh["inverters"]:
            layout = layouts.layout(inv["id"])
            if layout is None or len(layout["strings"]) < 2:
                continue
            readings: list[dict] = []
            for store in stores:
                readings += await store.string_power_rows(inv["id"], since)
            out.append(
                {
                    "householdId": hh["householdId"],
                    "inverterId": inv["id"],
                    "inverterName": layout["inverterName"],
                    "strings": layout["strings"],
                    "readings": readings,
                }
            )
    return out
