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
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .api_client import SvitgridApiClient
from .bootstrap import run_first_time
from .command_poller import run_loop as run_command_loop
from .const import (
    COMMAND_POLL_INTERVAL_S,
    DOMAIN,
    READINGS_INTERVAL_S,
    REQUIRED_FIELDS,
)
from .keystore import SvitgridKeystore
from .readings_publisher import run_loop as run_readings_loop

_LOGGER = logging.getLogger(__name__)


def _validate_entity_map(value: dict) -> dict:
    missing = REQUIRED_FIELDS - set(value.keys())
    if missing:
        raise vol.Invalid(f"entity_map missing required fields: {sorted(missing)}")
    return value


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

    # Trusted-keys cache: signingKeyId → publicKeyHex for each approved
    # household admin device. Plan A's bootstrap response gives us the IDs
    # but not the hex values (those arrive via add_trusted_key commands).
    # In B1 this dict stays empty — command_poller.process_command has a
    # documented fallback that sends signed rejection ACKs without admin-
    # signature verification when the cache is empty. This lets us validate
    # the wire-protocol contract end-to-end in B1. B2 replaces this with
    # live updates driven by add_trusted_key / revoke_trusted_key commands,
    # after which the fallback stops triggering.
    trusted_public_keys_hex: dict[str, str] = {}

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
            executor_version="0.1.0",
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
        "readings_task": readings_task,
        "poller_task": poller_task,
    }
    _LOGGER.info("Svitgrid integration started (device_id=%s)", device_id)
    return True
