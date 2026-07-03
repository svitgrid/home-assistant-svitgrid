"""set_harvest_config apply helpers (mirror set_cloud_endpoint's probe→apply)."""

from __future__ import annotations

import asyncio
import contextlib
import logging

_LOGGER = logging.getLogger(__name__)


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
    """Update ip/port/slave_id on the entry's single harvest_config, reload."""
    inverters = list(entry.data.get("inverters", []))
    for inv in inverters:
        hc = inv.get("harvest_config")
        if hc:
            hc["ip"] = conn["ip"]
            hc["port"] = conn["port"]
            hc["slave_id"] = conn["slaveId"]
            break
    new_data = {**entry.data, "inverters": inverters}
    hass.config_entries.async_update_entry(entry, data=new_data)
    hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
