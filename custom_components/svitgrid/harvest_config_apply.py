"""set_harvest_config apply helpers (mirror set_cloud_endpoint's probe→apply)."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging

try:  # normal package import
    from .const import DOMAIN
except ImportError:  # some unit tests load this module standalone by file path
    DOMAIN = "svitgrid"

_LOGGER = logging.getLogger(__name__)


def _suppress_listener_reload(hass) -> None:
    """Tell the config-entry update-listener (_async_reload_entry) to SKIP its
    reload for the next async_update_entry — the caller does its own explicit
    reload. Without this both fire, producing two overlapping setups (e.g. two
    direct-harvest loops contending for a single-connection Solarman logger)."""
    hass.data.setdefault(DOMAIN, {})["_skip_reload_once"] = True


async def probe_modbus_reachable(host: str, port: int) -> bool:
    """TCP-connect probe (fail-closed). We do NOT read registers here — a
    successful connect is enough to accept the new endpoint; wrong register
    maps are a separate (model/protocol) concern, which is immutable."""
    try:
        conn = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(conn, timeout=5)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True
    except Exception:  # noqa: BLE001
        _LOGGER.warning("probe_modbus_reachable failed for %s:%s", host, port)
        return False


async def apply_harvest_config_change(hass, entry, conn: dict) -> None:
    """Update ip/port/slave_id on the entry's single harvest_config, reload.

    Builds `new_data` from a deep copy of `entry.data` so the mutation below
    never touches the objects `entry.data` still references. A shallow
    `list(...)` copy shares the inner `harvest_config` dicts with
    `entry.data` — mutating them in place makes `new_data == entry.data` by
    the time `async_update_entry` runs, and Home Assistant treats an
    unchanged `data=` as a no-op, silently dropping the persisted change
    (reverts to the old connection on next restart).
    """
    # entry.data is a read-only MappingProxyType at runtime; copy.deepcopy can't
    # pickle a mappingproxy (Python 3.14 TypeError), so dict()-convert first.
    new_data = copy.deepcopy(dict(entry.data))
    for inv in new_data.get("inverters", []):
        hc = inv.get("harvest_config")
        if hc:
            hc["ip"] = conn["ip"]
            hc["port"] = conn["port"]
            hc["slave_id"] = conn["slaveId"]
            break
    _suppress_listener_reload(hass)
    hass.config_entries.async_update_entry(entry, data=new_data)
    hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))


async def apply_read_source_change(
    hass, entry, inverter_id: str, harvest_config: dict | None
) -> None:
    """Set (create/replace) or clear the `harvest_config` on the inverter with
    `inverter_id`, then reload the entry so async_setup_entry re-selects the
    read loop (harvest_config present → run_direct_harvest_loop; absent →
    run_readings_loop off the retained entity_map).

    Deep-copies entry.data — an in-place mutation would make new_data == entry.data
    and HA drops an unchanged `data=` as a no-op (same gotcha as
    apply_harvest_config_change).
    """
    # entry.data is a read-only MappingProxyType at runtime; copy.deepcopy can't
    # pickle a mappingproxy (Python 3.14 TypeError), so dict()-convert first.
    new_data = copy.deepcopy(dict(entry.data))
    for inv in new_data.get("inverters", []):
        if inv.get("inverter_id") == inverter_id:
            if harvest_config is None:
                inv.pop("harvest_config", None)
            else:
                inv["harvest_config"] = harvest_config
            break
    _suppress_listener_reload(hass)
    hass.config_entries.async_update_entry(entry, data=new_data)

    async def _do_reload() -> None:
        await hass.config_entries.async_reload(entry.entry_id)

    hass.async_create_task(_do_reload())
