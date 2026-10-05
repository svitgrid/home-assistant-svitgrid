"""`gridPortPower` end to end inside Home Assistant (svitgrid#843).

`tests/test_grid_port_power.py` proves the field is in the catalogue. These
tests prove the two things a CT household actually does with it:

1. map it in the options form and save, through Home Assistant's own flow
   manager, so the form's voluptuous schema has to accept the key. The older
   options-flow tests call the step methods directly and would pass even if the
   field were missing from the form;
2. have its sensor read, aggregated and uploaded beside `gridPower`, with the
   sign intact, so the API's `gridPower - gridPortPower` sees the same window
   for both figures.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.readings_publisher import (
    _aggregate_samples,
    build_reading_payload,
)

INV_ID = "ha-ct"

# The CT mapping from svitgrid#843: the clamp as the mains, the port beside it.
CT_MAP = {
    "batteryPower": "sensor.inverter_battery_power",
    "batteryVoltage": "sensor.inverter_battery_voltage",
    "loadPower": "sensor.inverter_load_power",
    "gridPower": "sensor.inverter_external_power",
    "gridPortPower": "sensor.inverter_grid_power",
}


def _entry(entity_map: dict[str, str]):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            "api_base": "https://api.test",
            "api_key": "k",
            "edge_device_id": "e1",
            "household_id": "hh1",
            "signing_key_id": "sk",
            "private_key_pem": "pem",
            "public_key_hex": "pub",
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": INV_ID,
                    "entity_map": entity_map,
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG03LP1",
                    "phases": 1,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": "deye-sg03lp1-solarman-v1",
                }
            ],
        },
    )


async def _open_mapping_form(hass: HomeAssistant, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_inverter"}
    )
    if result["type"] == FlowResultType.FORM and "inverter_id" in result["data_schema"].schema:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"inverter_id": INV_ID}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "edit_inverter"
    return result


def _suggested(form, field: str):
    for key in form["data_schema"].schema:
        if str(key) == field:
            return (key.description or {}).get("suggested_value")
    raise AssertionError(f"{field} is not in the mapping form")


@pytest.mark.asyncio
async def test_ct_household_maps_and_saves_grid_port_power_through_the_form(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    preset_map = {**CT_MAP, "gridPower": "sensor.inverter_grid_power"}
    del preset_map["gridPortPower"]
    entry = _entry(preset_map)
    entry.add_to_hass(hass)

    form = await _open_mapping_form(hass, entry)
    assert _suggested(form, "gridPortPower") is None

    result = await hass.config_entries.options.async_configure(form["flow_id"], dict(CT_MAP))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    saved = next(i for i in entry.data["inverters"] if i["inverter_id"] == INV_ID)
    assert saved["entity_map"]["gridPower"] == "sensor.inverter_external_power"
    assert saved["entity_map"]["gridPortPower"] == "sensor.inverter_grid_power"

    # Reopening the form shows the saved mapping, so an owner who edits some
    # other field does not silently clear the port by resubmitting.
    form = await _open_mapping_form(hass, entry)
    assert _suggested(form, "gridPortPower") == "sensor.inverter_grid_power"
    assert _suggested(form, "gridPower") == "sensor.inverter_external_power"


@pytest.mark.asyncio
async def test_ct_sensors_are_read_and_aggregated_side_by_side(hass: HomeAssistant) -> None:
    """Three ticks of the reporter's screen (CT 2400 W, port 743 W), one with
    the port exporting. The aggregate keeps both keys and their signs, and its
    difference is the mean of the per-tick differences."""
    ticks = [(2400.0, 743.0), (2380.0, 731.0), (40.0, -2160.0)]
    samples = []
    for ct, port in ticks:
        hass.states.async_set("sensor.inverter_battery_power", "500")
        hass.states.async_set("sensor.inverter_battery_voltage", "52.1")
        hass.states.async_set("sensor.inverter_load_power", "725")
        hass.states.async_set("sensor.inverter_external_power", str(ct))
        hass.states.async_set("sensor.inverter_grid_power", str(port))
        sample = build_reading_payload(hass=hass, inverter_id=INV_ID, entity_map=CT_MAP)
        assert sample["gridPower"] == ct
        assert sample["gridPortPower"] == port
        samples.append(sample)

    agg = _aggregate_samples(samples, period_s=30)
    assert agg["gridPower"] == pytest.approx(sum(c for c, _ in ticks) / 3)
    assert agg["gridPortPower"] == pytest.approx(sum(p for _, p in ticks) / 3)
    assert agg["gridPower"] - agg["gridPortPower"] == pytest.approx(
        sum(c - p for c, p in ticks) / 3
    )


@pytest.mark.asyncio
async def test_unavailable_port_sensor_drops_only_the_port(hass: HomeAssistant) -> None:
    """A port sensor that goes unavailable must not take the reading with it,
    and must not be sent as 0: a 0 port beside a 2400 W clamp would read as
    2400 W of grid-side load."""
    hass.states.async_set("sensor.inverter_battery_power", "500")
    hass.states.async_set("sensor.inverter_battery_voltage", "52.1")
    hass.states.async_set("sensor.inverter_load_power", "725")
    hass.states.async_set("sensor.inverter_external_power", "2400")
    hass.states.async_set("sensor.inverter_grid_power", "unavailable")
    payload = build_reading_payload(hass=hass, inverter_id=INV_ID, entity_map=CT_MAP)
    assert payload["gridPower"] == 2400.0
    assert "gridPortPower" not in payload
