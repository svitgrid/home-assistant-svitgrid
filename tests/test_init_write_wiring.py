"""Task 10 wiring: a harvest_config inverter gets a native WriteExecutor that
reuses the SAME spec_holder built for the read loop; a relay inverter (with
command_recipes, no harvest_config) keeps its YamlDispatcher. Each inverter
gets exactly one executor."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.executors.yaml_dispatcher import YamlDispatcher
from custom_components.svitgrid.harvest.write_executor import WriteExecutor
from custom_components.svitgrid.reading_store import ReadingStore

_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}

_MINIMAL_SPEC = {
    "modelId": "deye_sg04lp3",
    "version": 1,
    "protocol": "solarman_v5",
    "port": 8899,
    "defaultSlaveId": 1,
    "reads": [],
    "derivations": [],
}


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
        title="Svitgrid (write wiring)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-write",
            "signing_key_id": "ha-home-01",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-harvest",
                    "entity_map": {},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                    "harvest_config": {
                        "model_id": "deye_sg04lp3",
                        "host": "10.0.0.5",
                        "port": 8899,
                        "slave_id": 1,
                    },
                },
                {
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
        entry_id="entry-write",
    )


@pytest.mark.asyncio
async def test_harvest_inverter_gets_write_executor_reusing_spec_holder(
    hass, enable_custom_integrations
):
    from custom_components.svitgrid import async_setup_entry

    entry = _make_entry()
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch(
            "custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock
        ) as harvest,
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
    ):
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(return_value=dict(_MINIMAL_SPEC))
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True

    entry_state = hass.data[DOMAIN][entry.entry_id]
    executors = entry_state["executors_by_inverter"]

    # Harvest inverter → native WriteExecutor.
    harvest_exec = executors["ha-harvest"]
    assert isinstance(harvest_exec, WriteExecutor)

    # It must REUSE the same spec_holder object passed to run_direct_harvest_loop.
    spec_holder_passed = harvest.call_args.kwargs["spec_holder"]
    assert harvest_exec._spec_holder is spec_holder_passed
    # And the cfg is the inverter's harvest_config.
    assert harvest_exec._cfg["model_id"] == "deye_sg04lp3"

    # Relay inverter → YamlDispatcher (unchanged path).
    assert isinstance(executors["ha-relay"], YamlDispatcher)

    # Exactly two executors, one per inverter.
    assert set(executors.keys()) == {"ha-harvest", "ha-relay"}
