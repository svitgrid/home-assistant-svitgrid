import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid import _inverters_from_entry, async_migrate_entry
from custom_components.svitgrid.const import DOMAIN


def _legacy_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            "api_base": "https://api.test",
            "api_key": "k",
            "edge_device_id": "edge1",
            "hardware_id": "ha-aaa",
            "household_id": "hh1",
            "signing_key_id": "sk",
            "private_key_pem": "pem",
            "public_key_hex": "pub",
            "trusted_keys": [],
            "preset_id": "deye-sg04lp3",
            "entity_map": {"batterySoc": "sensor.soc"},
            "brand": "Deye",
            "model": "SG04LP3",
            "phases": 3,
            "has_battery": True,
            "pv_strings": 2,
            "commands": [],
        },
    )


@pytest.mark.asyncio
async def test_migrate_wraps_legacy_scalar_into_inverters_list(hass):
    entry = _legacy_entry()
    entry.add_to_hass(hass)
    ok = await async_migrate_entry(hass, entry)
    assert ok is True
    assert entry.version == 2
    invs = entry.data["inverters"]
    assert len(invs) == 1
    assert invs[0]["inverter_id"] == "ha-aaa"
    assert invs[0]["entity_map"] == {"batterySoc": "sensor.soc"}
    assert invs[0]["brand"] == "Deye"
    assert invs[0]["command_recipes"] == []


def test_inverters_from_entry_prefers_new_list():
    e = _legacy_entry()
    e2 = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            **e.data,
            "inverters": [
                {
                    "inverter_id": "ha-aaa",
                    "entity_map": {"batterySoc": "sensor.s"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "X",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                },
            ],
        },
    )
    out = _inverters_from_entry(e2)
    assert len(out) == 1 and out[0]["inverter_id"] == "ha-aaa"


def test_inverters_from_entry_options_entity_map_overrides_first_inverter():
    e = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            "inverters": [
                {
                    "inverter_id": "ha-aaa",
                    "entity_map": {"batterySoc": "sensor.old"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "X",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                },
            ]
        },
        options={"entity_map": {"batterySoc": "sensor.new"}},
    )
    out = _inverters_from_entry(e)
    assert out[0]["entity_map"] == {"batterySoc": "sensor.new"}
