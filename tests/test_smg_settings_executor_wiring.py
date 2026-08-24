"""Task 5, step 5: the two SMG II settings commands are dispatchable, and
`async_setup_entry` wires an `EybondSmgSettingsExecutor` for exactly the
inverters `is_eybond_harvest` recognises -- the SAME discriminator
`__init__.py` already uses to decide whether an inverter goes through the
shared EyBond hub at all (see `test_eybond_at_setup.py` for `is_eybond_harvest`
itself; this file does not re-implement that question, only checks that the
executor wiring follows its answer).

A name missing from DISPATCHABLE_COMMANDS is rejected before any executor is
consulted, and that failure looks identical to a broken executor -- hence the
standalone constant assertion below.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.const import DISPATCHABLE_COMMANDS, DOMAIN
from custom_components.svitgrid.executors.smg_settings_executor import (
    EybondSmgSettingsExecutor,
)
from custom_components.svitgrid.executors.yaml_dispatcher import YamlDispatcher
from custom_components.svitgrid.reading_store import ReadingStore

_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}


def test_both_commands_are_dispatchable():
    # A name missing here is rejected in command_poller before any executor
    # is consulted -- see the module docstring.
    assert "read_inverter_settings" in DISPATCHABLE_COMMANDS
    assert "set_inverter_setting" in DISPATCHABLE_COMMANDS


@pytest.fixture(autouse=True)
def _stub_store_side_effects():
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
    ):
        yield


def _make_entry():
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (SMG II settings wiring)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-smg",
            "signing_key_id": "ha-home-01",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-anenji",
                    "entity_map": {},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Anenji",
                    "model": "SMG II",
                    "phases": 1,
                    "has_battery": True,
                    "pv_strings": 1,
                    "preset_id": None,
                    "harvest_config": {
                        "protocol": "eybond_at",
                        "inverter_serial": "99432604107106",
                    },
                },
                {
                    # NOT an EyBond inverter: a relay inverter, same shape as
                    # the ones every other test in this repo uses. It must
                    # NOT be handed an EybondSmgSettingsExecutor -- one that
                    # cannot serve it, because it has no collector to read.
                    "inverter_id": "ha-relay",
                    "entity_map": {"batterySoc": "sensor.soc"},
                    "command_recipes": [
                        {"id": "set_battery_charge", "service": "modbus.write_register", "args": {}}
                    ],
                    "command_config": {"hub_name": "solarman", "slave_id": 1},
                    "brand": "Deye",
                    "model": "Y",
                    "phases": 1,
                    "has_battery": True,
                    "pv_strings": 1,
                    "preset_id": None,
                },
            ],
        },
        entry_id="entry-smg-settings",
    )


@pytest.mark.asyncio
async def test_eybond_inverter_gets_smg_settings_executor(hass, enable_custom_integrations):
    from custom_components.svitgrid import async_setup_entry

    entry = _make_entry()
    entry.add_to_hass(hass)

    fake_hub = object()

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
        patch(
            "custom_components.svitgrid.start_eybond_hub",
            AsyncMock(return_value=(fake_hub, {})),
        ) as start_hub,
    ):
        client = mock_cls.return_value
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    start_hub.assert_awaited_once()

    entry_state = hass.data[DOMAIN][entry.entry_id]
    executors = entry_state["executors_by_inverter"]

    # The EyBond inverter got the settings executor, bound to the hub
    # start_eybond_hub returned and to ITS OWN serial.
    smg_exec = executors["ha-anenji"]
    assert isinstance(smg_exec, EybondSmgSettingsExecutor)
    assert smg_exec._hub is fake_hub
    assert smg_exec._inverter_serial == "99432604107106"

    # The relay inverter is untouched: still its usual YamlDispatcher, never
    # an executor that has no collector to serve it.
    assert isinstance(executors["ha-relay"], YamlDispatcher)
    assert not isinstance(executors["ha-relay"], EybondSmgSettingsExecutor)


@pytest.mark.asyncio
async def test_no_smg_settings_executor_when_the_eybond_hub_never_starts(
    hass, enable_custom_integrations
):
    """Fail-open matches the existing eybond_hub-start try/except: if the hub
    cannot start, no inverter on that transport gets ANY executor -- there is
    nothing for one to talk to. That is a pre-existing property of the
    surrounding try/except this task did not change; asserted here so a
    future edit that narrows the except cannot silently start handing out
    executors bound to a hub that failed to come up."""
    from custom_components.svitgrid import async_setup_entry

    entry = _make_entry()
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
        patch(
            "custom_components.svitgrid.start_eybond_hub",
            AsyncMock(side_effect=RuntimeError("port in use")),
        ),
    ):
        client = mock_cls.return_value
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    executors = hass.data[DOMAIN][entry.entry_id]["executors_by_inverter"]
    assert "ha-anenji" not in executors
    assert isinstance(executors["ha-relay"], YamlDispatcher)
