"""Anenji HA presets: shape validation + readings behavior.

Anenji support ships as data-only haPresets (no protocol code). These tests
lock the two preset YAMLs' shape and prove the ESPHome preset's entity map
drives a correct /ingest/reading payload through build_reading_payload.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from custom_components.svitgrid.readings_publisher import build_reading_payload

PRESETS_DIR = pathlib.Path(__file__).resolve().parent.parent / "presets"
ANENJI_IDS = ["anenji-esphome-v1", "anenji-generic-v1"]
ALLOWED_PROTOCOLS = {"home_assistant", "home_assistant_solarman"}
REQUIRED = ["id", "version", "brand", "model", "phases", "hasBattery", "protocolId", "entityMap"]


def _load(preset_id: str) -> dict:
    path = PRESETS_DIR / f"{preset_id}.yaml"
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize("preset_id", ANENJI_IDS)
def test_preset_shape_is_valid(preset_id):
    p = _load(preset_id)
    for field in REQUIRED:
        assert field in p, f"{preset_id}: missing {field}"
    assert p["id"] == preset_id
    assert p["brand"] == "Anenji"
    # version is a numeric string per HaPresetSchema (/^\d+$/).
    assert isinstance(p["version"], str) and p["version"].isdigit()
    assert p["phases"] in (1, 2, 3)
    assert p["protocolId"] in ALLOWED_PROTOCOLS
    # Anenji uses a raw HA poller (Modbus/ESPHome/MQTT), never Solarman.
    assert p["protocolId"] == "home_assistant"
    assert isinstance(p["entityMap"], dict) and p["entityMap"], "entityMap must be non-empty"
    # Control is deferred until hardware verification — no recipes yet.
    assert p.get("commands", []) == []


def test_esphome_map_produces_canonical_reading(hass):
    p = _load("anenji-esphome-v1")
    em = p["entityMap"]
    # Feed HA states named exactly as the ESPHome Anenji config slugifies them.
    hass.states.async_set(em["batterySoc"], "73", {"unit_of_measurement": "%"})
    hass.states.async_set(em["batteryPower"], "-1200", {"unit_of_measurement": "W"})
    hass.states.async_set(em["pv1Power"], "1500", {"unit_of_measurement": "W"})
    hass.states.async_set(em["pv2Power"], "900", {"unit_of_measurement": "W"})
    hass.states.async_set(em["gridPower"], "300", {"unit_of_measurement": "W"})
    hass.states.async_set(em["loadPower"], "2700", {"unit_of_measurement": "W"})

    payload = build_reading_payload(hass=hass, inverter_id="anenji-1", entity_map=em)

    assert payload["batterySoc"] == 73.0
    assert payload["batteryPower"] == -1200.0
    assert payload["gridPower"] == 300.0
    assert payload["loadPower"] == 2700.0
    # Per-string PV is renamed to the API's canonical pvPowerN, total summed.
    assert payload["pvPower1"] == 1500.0
    assert payload["pvPower2"] == 900.0
    assert payload["pvPower"] == 2400.0
    assert "pv1Power" not in payload
