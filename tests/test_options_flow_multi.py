import pytest
from unittest.mock import AsyncMock, patch
from custom_components.svitgrid.config_flow import SvitgridOptionsFlow
from custom_components.svitgrid.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _entry(hass):
    e = MockConfigEntry(domain=DOMAIN, version=2, data={
        "api_base": "https://api.test", "api_key": "k", "edge_device_id": "edge1",
        "household_id": "hh1", "signing_key_id": "sk", "private_key_pem": "pem",
        "public_key_hex": "pub", "trusted_keys": [],
        "inverters": [{"inverter_id": "ha-aaa", "entity_map": {"batterySoc": "sensor.a"}, "command_recipes": [], "command_config": {}, "brand": "Deye", "model": "X", "phases": 3, "has_battery": True, "pv_strings": 2, "preset_id": None}],
    })
    e.add_to_hass(hass)
    return e


@pytest.mark.asyncio
async def test_init_shows_menu(hass):
    flow = SvitgridOptionsFlow(_entry(hass))
    flow.hass = hass
    res = await flow.async_step_init()
    assert res["type"] == "menu"
    assert set(res["menu_options"]) >= {"add_inverter", "edit_inverter", "remove_inverter"}


@pytest.mark.asyncio
async def test_add_inverter_manual_calls_api_and_appends(hass):
    entry = _entry(hass)
    flow = SvitgridOptionsFlow(entry)
    flow.hass = hass

    fake_client = AsyncMock()
    fake_client.add_inverter.return_value = {
        "inverterId": "ha-bbb", "entityMap": {"batterySoc": "sensor.b"},
        "brand": "Deye", "model": "Y", "phases": 1, "hasBattery": True, "pvStrings": 1,
        "commands": [], "presetId": None,
    }
    with patch("custom_components.svitgrid.config_flow.SvitgridApiClient", return_value=fake_client):
        await flow.async_step_add_inverter({"brand": "Deye", "model": "Y", "phases": "1", "has_battery": True, "pv_strings": 1})
        res = await flow.async_step_add_inverter_entities({"batterySoc": "sensor.b", "hub_name": "solarman", "slave_id": 1})

    fake_client.add_inverter.assert_awaited_once()
    invs = entry.data["inverters"]
    assert [i["inverter_id"] for i in invs] == ["ha-aaa", "ha-bbb"]
    assert invs[1]["entity_map"] == {"batterySoc": "sensor.b"}
    assert res["type"] == "create_entry"


@pytest.mark.asyncio
async def test_remove_inverter_drops_from_list(hass):
    entry = _entry(hass)
    # seed a second inverter so removal leaves a non-empty list
    invs = list(entry.data["inverters"]) + [{"inverter_id": "ha-bbb", "entity_map": {"batterySoc": "sensor.b"}, "command_recipes": [], "command_config": {}, "brand": "Deye", "model": "Y", "phases": 1, "has_battery": True, "pv_strings": 1, "preset_id": None}]
    hass.config_entries.async_update_entry(entry, data={**entry.data, "inverters": invs})
    flow = SvitgridOptionsFlow(entry)
    flow.hass = hass
    res = await flow.async_step_remove_inverter({"inverter_id": "ha-aaa"})
    assert res["type"] == "create_entry"
    assert [i["inverter_id"] for i in entry.data["inverters"]] == ["ha-bbb"]
