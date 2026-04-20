"""Readings publisher: builds a payload from current HA entity states, omits
unavailable/non-numeric fields, flushes every 10s to /ingest/reading."""

from __future__ import annotations

from custom_components.svitgrid.readings_publisher import build_reading_payload


def test_build_payload_includes_mapped_entities(hass):
    hass.states.async_set("sensor.my_soc", "85", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.my_battery_power", "-1500", {"unit_of_measurement": "W"})

    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={
            "batterySoc": "sensor.my_soc",
            "batteryPower": "sensor.my_battery_power",
        },
    )

    assert payload["inverterId"] == "inv-1"
    assert payload["batterySoc"] == 85.0
    assert payload["batteryPower"] == -1500.0
    assert "timestamp" in payload
    assert payload["source"] == "edge-device"


def test_build_payload_omits_unavailable_entities(hass):
    hass.states.async_set("sensor.my_soc", "unavailable")
    hass.states.async_set("sensor.my_battery_power", "-1500", {})

    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={
            "batterySoc": "sensor.my_soc",
            "batteryPower": "sensor.my_battery_power",
        },
    )

    assert "batterySoc" not in payload
    assert payload["batteryPower"] == -1500.0


def test_build_payload_omits_non_numeric(hass):
    hass.states.async_set("sensor.soc", "unknown")
    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={"batterySoc": "sensor.soc"},
    )
    assert "batterySoc" not in payload


def test_build_payload_omits_missing_entity(hass):
    # Entity never registered
    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={"batterySoc": "sensor.does_not_exist"},
    )
    assert "batterySoc" not in payload


def test_build_payload_aggregates_pv_power(hass):
    hass.states.async_set("sensor.pv1", "1500", {})
    hass.states.async_set("sensor.pv2", "2000", {})
    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={
            "pv1Power": "sensor.pv1",
            "pv2Power": "sensor.pv2",
        },
    )
    assert payload["pv1Power"] == 1500.0
    assert payload["pv2Power"] == 2000.0
    assert payload["pvPower"] == 3500.0


def test_build_payload_single_mppt_aggregates_to_pv1_total(hass):
    hass.states.async_set("sensor.pv1", "1500", {})
    payload = build_reading_payload(
        hass=hass,
        inverter_id="inv-1",
        entity_map={"pv1Power": "sensor.pv1"},
    )
    assert payload["pvPower"] == 1500.0
