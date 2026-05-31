import pytest
from custom_components.svitgrid.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.mark.asyncio
async def test_one_device_per_inverter(hass):
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={
        "api_base": "https://api.test", "api_key": "k", "edge_device_id": "edge1",
        "household_id": "hh1", "signing_key_id": "sk", "private_key_pem": "pem",
        "public_key_hex": "pub", "trusted_keys": [],
        "inverters": [
            {"inverter_id": "ha-aaa", "entity_map": {"batterySoc": "sensor.a"}, "command_recipes": [], "command_config": {}, "brand": "Deye", "model": "X", "phases": 3, "has_battery": True, "pv_strings": 2, "preset_id": None},
            {"inverter_id": "ha-bbb", "entity_map": {"batterySoc": "sensor.b"}, "command_recipes": [], "command_config": {}, "brand": "Deye", "model": "Y", "phases": 1, "has_battery": True, "pv_strings": 1, "preset_id": None},
        ],
    })
    entry.add_to_hass(hass)
    added = []

    def _capture(entities, *a, **k):
        added.extend(entities)

    from custom_components.svitgrid.activity import ActivityTracker
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "activity": ActivityTracker(), "entry_data": dict(entry.data),
    }
    from custom_components.svitgrid import sensor as sensor_mod
    await sensor_mod.async_setup_entry(hass, entry, _capture)

    # 5 sensors per inverter, 2 inverters = 10
    assert len(added) == 10
    # device identifiers reference each inverter id
    idents = set()
    for e in added:
        idents |= set(e.device_info["identifiers"])
    assert (DOMAIN, "ha-aaa") in idents
    assert (DOMAIN, "ha-bbb") in idents
    # unique_ids are distinct (no collisions across inverters)
    uids = [e.unique_id for e in added]
    assert len(uids) == len(set(uids))
