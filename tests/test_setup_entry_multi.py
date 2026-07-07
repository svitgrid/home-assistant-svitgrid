from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid import async_migrate_entry, async_setup_entry
from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.reading_store import ReadingStore

# The HA test harness blocks SQLite file opens under its testing_config dir, so
# the store's real get_lifecycle() can't run here. Patch it to the default
# "active" lifecycle the unset-meta store would return.
_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}


def _two_inverter_entry():
    base = {
        "api_base": "https://api.test",
        "api_key": "k",
        "edge_device_id": "edge1",
        "household_id": "hh1",
        "signing_key_id": "sk",
        "private_key_pem": "pem",
        "public_key_hex": "pub",
        "trusted_keys": [],
    }
    invs = [
        {
            "inverter_id": "ha-aaa",
            "entity_map": {"batterySoc": "sensor.a"},
            "command_recipes": [],
            "command_config": {},
            "brand": "Deye",
            "model": "X",
            "phases": 3,
            "has_battery": True,
            "pv_strings": 2,
            "preset_id": None,
        },
        {
            "inverter_id": "ha-bbb",
            "entity_map": {"batterySoc": "sensor.b"},
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
    ]
    return MockConfigEntry(domain=DOMAIN, version=2, data={**base, "inverters": invs})


@pytest.mark.asyncio
async def test_setup_spawns_one_readings_loop_per_inverter(hass):
    entry = _two_inverter_entry()
    entry.add_to_hass(hass)
    started = []

    async def _fake_readings_loop(*, inverter_id, **kwargs):
        started.append(inverter_id)

    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
        patch("custom_components.svitgrid.run_readings_loop", side_effect=_fake_readings_loop),
        patch("custom_components.svitgrid.run_command_loop", return_value=None),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", return_value=None),
        patch("custom_components.svitgrid.run_sender_loop", return_value=None),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=True),
    ):
        await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert sorted(started) == ["ha-aaa", "ha-bbb"]
    state = hass.data[DOMAIN][entry.entry_id]
    assert set(state["readings_tasks"].keys()) == {"ha-aaa", "ha-bbb"}
    # only the inverter WITH command_recipes gets an executor
    assert set(state["executors_by_inverter"].keys()) == {"ha-bbb"}


@pytest.mark.asyncio
async def test_migrated_v1_entry_sets_up_without_error(hass):
    """End-to-end: a v1 scalar entry migrates then sets up cleanly (no KeyError
    on the removed top-level hardware_id)."""
    v1 = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            "api_base": "https://api.test",
            "api_key": "k",
            "edge_device_id": "edge1",
            "hardware_id": "ha-legacy",
            "household_id": "hh1",
            "signing_key_id": "sk",
            "private_key_pem": "pem",
            "public_key_hex": "pub",
            "trusted_keys": [],
            "preset_id": None,
            "entity_map": {"batterySoc": "sensor.soc"},
            "brand": "Deye",
            "model": "SG04LP3",
            "phases": 3,
            "has_battery": True,
            "pv_strings": 2,
            "commands": [],
        },
    )
    v1.add_to_hass(hass)
    await async_migrate_entry(hass, v1)
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
        patch("custom_components.svitgrid.run_readings_loop", return_value=None),
        patch("custom_components.svitgrid.run_command_loop", return_value=None),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", return_value=None),
        patch("custom_components.svitgrid.run_sender_loop", return_value=None),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(hass.config_entries, "async_forward_entry_setups", return_value=True),
    ):
        ok = await async_setup_entry(hass, v1)
        await hass.async_block_till_done()
    assert ok is True
    state = hass.data[DOMAIN][v1.entry_id]
    assert set(state["readings_tasks"].keys()) == {"ha-legacy"}
