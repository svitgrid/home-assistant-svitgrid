import copy
from unittest.mock import MagicMock
import pytest
from custom_components.svitgrid.harvest_config_apply import apply_read_source_change


def _entry(inverters):
    e = MagicMock()
    e.data = {"inverters": inverters}
    e.entry_id = "entry-1"
    return e


def _hass():
    h = MagicMock()
    return h


@pytest.mark.asyncio
async def test_create_harvest_config_on_relay_inverter():
    entry = _entry([{"inverter_id": "ha-1", "entity_map": {"batterySoc": "sensor.x"}}])
    hass = _hass()
    hc = {"protocol": "solarman_v5", "ip": "192.168.1.133", "port": 8899,
          "slave_id": 1, "model_id": "deye_sg04lp3", "logger_serial": ""}
    await apply_read_source_change(hass, entry, "ha-1", hc)
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    inv = new_data["inverters"][0]
    assert inv["harvest_config"] == hc
    assert inv["entity_map"] == {"batterySoc": "sensor.x"}  # entity_map retained
    hass.config_entries.async_reload.assert_not_called()  # reload is via async_create_task
    hass.async_create_task.assert_called_once()


@pytest.mark.asyncio
async def test_clear_harvest_config_reverts_to_relay():
    entry = _entry([{"inverter_id": "ha-1", "entity_map": {"batterySoc": "sensor.x"},
                     "harvest_config": {"ip": "192.168.1.133", "port": 8899, "slave_id": 1}}])
    hass = _hass()
    await apply_read_source_change(hass, entry, "ha-1", None)
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    inv = new_data["inverters"][0]
    assert "harvest_config" not in inv
    assert inv["entity_map"] == {"batterySoc": "sensor.x"}


@pytest.mark.asyncio
async def test_targets_only_the_named_inverter():
    entry = _entry([
        {"inverter_id": "ha-1", "entity_map": {"a": "b"}},
        {"inverter_id": "ha-2", "entity_map": {"c": "d"}},
    ])
    hass = _hass()
    hc = {"protocol": "solarman_v5", "ip": "10.0.0.5", "port": 8899, "slave_id": 1,
          "model_id": "deye_sg04lp3", "logger_serial": ""}
    await apply_read_source_change(hass, entry, "ha-2", hc)
    new_data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert "harvest_config" not in new_data["inverters"][0]
    assert new_data["inverters"][1]["harvest_config"] == hc
