"""Svitgrid custom component — B1 MVP.

Wires everything together:
  - Reads `svitgrid:` block from configuration.yaml
  - Validates entity_map has all 5 required fields
  - Bootstraps if first-run; loads saved state otherwise
  - Starts readings publisher + command poller as long-running tasks
  - Unregisters cleanly on hass stop
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .activity import ActivityTracker
from .api_client import SvitgridApiClient
from .bootstrap import run_first_time
from .command_poller import run_loop as run_command_loop
from .executors.yaml_dispatcher import YamlDispatcher
from .mqtt_wake import run_loop as run_mqtt_wake_loop
from .const import (
    COMMAND_POLL_INTERVAL_S,
    DOMAIN,
    READINGS_INTERVAL_S,
    REQUIRED_FIELDS,
)
from .executors import create_executor
from .keystore import SvitgridKeystore
from .readings_publisher import run_loop as run_readings_loop

_LOGGER = logging.getLogger(__name__)


def _validate_entity_map(value: dict) -> dict:
    missing = REQUIRED_FIELDS - set(value.keys())
    if missing:
        raise vol.Invalid(f"entity_map missing required fields: {sorted(missing)}")
    return value


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener: reload the entry so a new entity_map takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)


def _inverters_from_entry(entry: ConfigEntry) -> list[dict]:
    """Return the per-inverter config list. v2+ entries store `inverters`;
    options may override a single inverter's entity_map (legacy edit flow).
    Always returns a list of dicts with keys: inverter_id, entity_map,
    command_recipes, command_config, brand, model, phases, has_battery,
    pv_strings, preset_id."""
    invs = entry.data.get("inverters")
    if invs:
        result = [dict(i) for i in invs]
        # Legacy options.entity_map applied to the FIRST inverter only (the
        # pre-multi edit flow wrote a flat entity_map). New per-inverter edits
        # write entry.data directly (Task 9), so this is back-compat only.
        opt_map = entry.options.get("entity_map")
        if opt_map is not None and result:
            result[0]["entity_map"] = dict(opt_map)
        return result
    return []


def _migrate_v1_to_v2(data: dict) -> dict:
    """Wrap legacy scalar fields into a single-element inverters list."""
    new = {
        k: data[k]
        for k in ("api_base", "api_key", "edge_device_id", "household_id",
                  "signing_key_id", "private_key_pem", "public_key_hex", "trusted_keys")
        if k in data
    }
    new["inverters"] = [{
        "inverter_id": data.get("hardware_id"),
        "entity_map": data.get("entity_map") or {},
        "command_recipes": data.get("commands") or [],
        "command_config": {"hub_name": "solarman", "slave_id": 1, "battery_voltage": 52.8},
        "brand": data.get("brand"),
        "model": data.get("model"),
        "phases": data.get("phases"),
        "has_battery": data.get("has_battery"),
        "pv_strings": data.get("pv_strings"),
        "preset_id": data.get("preset_id"),
    }]
    return new


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate v1 (scalar single-inverter) entries to v2 (inverters list)."""
    if entry.version >= 2:
        return True
    new_data = _migrate_v1_to_v2(dict(entry.data))
    hass.config_entries.async_update_entry(entry, data=new_data, version=2)
    _LOGGER.info("Migrated Svitgrid entry %s to v2 (%d inverter(s))", entry.entry_id, len(new_data["inverters"]))
    return True


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required("api_base"): cv.url,
                vol.Required("device_id"): cv.string,
                vol.Required("signing_key_id"): cv.string,
                vol.Required("entity_map"): vol.All(
                    {cv.string: cv.entity_id}, _validate_entity_map
                ),
                vol.Optional("readings_interval_seconds", default=READINGS_INTERVAL_S): vol.All(
                    vol.Coerce(int), vol.Range(min=5, max=300)
                ),
                vol.Optional(
                    "command_poll_interval_seconds", default=COMMAND_POLL_INTERVAL_S
                ): vol.All(vol.Coerce(int), vol.Range(min=2, max=60)),
                vol.Optional("executor"): vol.Schema(
                    {
                        vol.Optional("type", default="read_only"): vol.In(["read_only", "smg_ii"]),
                        vol.Optional("modbus_hub"): cv.string,
                        vol.Optional("modbus_slave", default=1): vol.All(
                            vol.Coerce(int), vol.Range(min=1, max=247)
                        ),
                        vol.Optional("battery_nominal_voltage", default=48.0): vol.All(
                            vol.Coerce(float), vol.Range(min=12.0, max=600.0)
                        ),
                    }
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Svitgrid integration from YAML config."""
    conf = config.get(DOMAIN)
    if not conf:
        return True  # no svitgrid: block — nothing to do

    api_base = conf["api_base"]
    device_id = conf["device_id"]
    signing_key_id = conf["signing_key_id"]
    entity_map = conf["entity_map"]
    readings_interval = conf["readings_interval_seconds"]
    command_interval = conf["command_poll_interval_seconds"]

    session = aiohttp_client.async_get_clientsession(hass)
    api_client = SvitgridApiClient(session, api_base=api_base)
    keystore = SvitgridKeystore(hass)

    state = await keystore.load()
    if state is None:
        try:
            state = await run_first_time(
                api_client=api_client,
                keystore=keystore,
                device_id=device_id,
                signing_key_id=signing_key_id,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Svitgrid bootstrap failed. Integration will not start. "
                "Check device_id and that the mobile app opened a bootstrap window."
            )
            return False

    # In-memory cache populated from the keystore (which is written by
    # bootstrap + updated live via add_trusted_key / revoke_trusted_key
    # commands handled in command_poller.process_command).
    trusted_public_keys_hex: dict[str, str] = dict(state.trusted_public_keys_hex)

    # Build executor from YAML config (or None for read_only / missing block)
    executor_config = conf.get("executor") or {}
    executor = create_executor(executor_config, hass)

    # Inverter ID — B1 uses device_id as a placeholder. B2's config flow
    # resolves the real inverter ID from the bootstrap response.
    inverter_id = device_id

    readings_task = hass.async_create_background_task(
        run_readings_loop(
            hass=hass,
            api_client=api_client,
            api_key=state.api_key,
            inverter_id=inverter_id,
            entity_map=entity_map,
            interval_s=readings_interval,
        ),
        name="svitgrid_readings_publisher",
    )

    poller_task = hass.async_create_background_task(
        run_command_loop(
            hass=hass,
            api_client=api_client,
            keystore=keystore,
            trusted_public_keys_hex=trusted_public_keys_hex,
            executor_version="0.2.0",
            executors_by_inverter=({inverter_id: executor} if executor else {}),
            interval_s=command_interval,
        ),
        name="svitgrid_command_poller",
    )

    async def _on_stop(_event: Event) -> None:
        for task in (readings_task, poller_task):
            task.cancel()
        await asyncio.gather(readings_task, poller_task, return_exceptions=True)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)

    hass.data[DOMAIN] = {
        "session": session,
        "api_client": api_client,
        "keystore": keystore,
        "executor": executor,
        "trusted_public_keys_hex": trusted_public_keys_hex,
        "readings_task": readings_task,
        "poller_task": poller_task,
    }
    _LOGGER.info("Svitgrid integration started (device_id=%s)", device_id)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Svitgrid integration from a config entry.

    Iterates the per-inverter list from `_inverters_from_entry`, spawning one
    readings loop and (when recipes are present) one YamlDispatcher per inverter.
    A single command poller and MQTT wake loop are shared across all inverters.
    """
    data = entry.data
    session = aiohttp_client.async_get_clientsession(hass)
    api_client = SvitgridApiClient(session, api_base=data["api_base"])
    api_key = data["api_key"]
    activity = ActivityTracker()
    inverters = _inverters_from_entry(entry)

    if not inverters:
        _LOGGER.warning(
            "Config entry %s has no inverters configured; nothing to publish.",
            entry.entry_id,
        )

    readings_tasks: dict[str, asyncio.Task] = {}
    executors_by_inverter: dict[str, YamlDispatcher] = {}

    for inv in inverters:
        inverter_id = inv["inverter_id"]
        entity_map = dict(inv.get("entity_map") or {})
        if not entity_map:
            _LOGGER.warning(
                "Inverter %s has an empty entity_map — it will not publish readings.",
                inverter_id,
            )
        readings_tasks[inverter_id] = hass.async_create_background_task(
            run_readings_loop(
                hass=hass,
                api_client=api_client,
                api_key=api_key,
                inverter_id=inverter_id,
                entity_map=entity_map,
                activity=activity,
            ),
            name=f"svitgrid_readings_{inverter_id}",
        )
        recipes = inv.get("command_recipes") or []
        if recipes:
            executors_by_inverter[inverter_id] = YamlDispatcher(
                hass=hass,
                commands=recipes,
                config=dict(inv.get("command_config") or {}),
            )

    wake_event = asyncio.Event()
    command_task = hass.async_create_background_task(
        run_command_loop(
            hass=hass,
            api_client=api_client,
            keystore=None,
            entry_data=dict(data),
            wake_event=wake_event,
            activity=activity,
            executors_by_inverter=executors_by_inverter,
        ),
        name="svitgrid_command_poller",
    )

    mqtt_wake_task = hass.async_create_background_task(
        run_mqtt_wake_loop(
            hass=hass, api_client=api_client, api_key=api_key, wake_event=wake_event,
        ),
        name="svitgrid_mqtt_wake",
    )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api_client": api_client,
        "readings_tasks": readings_tasks,
        "command_task": command_task,
        "mqtt_wake_task": mqtt_wake_task,
        "executors_by_inverter": executors_by_inverter,
        "activity": activity,
        "entry_data": dict(data),
    }
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    _LOGGER.info(
        "Svitgrid started from config entry %s with %d inverter(s)",
        entry.entry_id, len(inverters),
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Cancel background tasks when the user removes the integration."""
    await hass.config_entries.async_unload_platforms(entry, ["sensor"])
    state = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if state is None:
        return True
    for task in (state.get("readings_tasks") or {}).values():
        if task and not task.done():
            task.cancel()
    for key in ("command_task", "mqtt_wake_task"):
        task = state.get(key)
        if task and not task.done():
            task.cancel()
    return True
