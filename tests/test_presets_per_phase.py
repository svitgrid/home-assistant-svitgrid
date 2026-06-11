"""3-phase hybrid Solarman presets: per-phase power mappings.

The app's per-phase grid display needs phaseVoltages + phaseGridPowers (and
phaseLoads for the home node). The API folds gridVoltageL*/gridPowerL*/
loadPowerL* scalars into those arrays at ingest, so the 3-phase hybrid
presets must map all three groups — voltages alone (the pre-v4 state) only
light up the voltage boxes, with no per-phase amps or load split.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from custom_components.svitgrid.const import ALL_FIELDS

PRESETS_DIR = pathlib.Path(__file__).resolve().parent.parent / "presets"

# 3-phase HYBRID Solarman presets (deye-string excluded: grid-tie, no load
# port, and the string profile's sensor set is unverified).
THREE_PHASE_HYBRID_IDS = [
    "deye-sg04lp3-solarman-v1",
    "deye-sg01hp3-solarman-v1",
    "deye-sg01hp3-50k-solarman-v1",
    "deye-sg05lp3-solarman-v1",
    "deye-gb-s20k-solarman-v1",
]

PER_PHASE_FIELDS = [
    "gridVoltageL1",
    "gridVoltageL2",
    "gridVoltageL3",
    "gridPowerL1",
    "gridPowerL2",
    "gridPowerL3",
    "loadPowerL1",
    "loadPowerL2",
    "loadPowerL3",
]


def _load(preset_id: str) -> dict:
    return yaml.safe_load((PRESETS_DIR / f"{preset_id}.yaml").read_text())


@pytest.mark.parametrize("preset_id", THREE_PHASE_HYBRID_IDS)
def test_three_phase_hybrid_presets_map_per_phase_fields(preset_id):
    p = _load(preset_id)
    entity_map = p["entityMap"]
    for field in PER_PHASE_FIELDS:
        assert field in entity_map, f"{preset_id}: entityMap missing {field}"
        assert entity_map[field].startswith("sensor."), f"{preset_id}: {field} not a sensor"


@pytest.mark.parametrize("preset_id", THREE_PHASE_HYBRID_IDS)
def test_preset_entity_map_keys_are_canonical(preset_id):
    """Every entityMap key must be a recognized canonical field — a typo'd
    key would be sent and silently stripped by the API."""
    p = _load(preset_id)
    unknown = set(p["entityMap"]) - ALL_FIELDS
    assert not unknown, f"{preset_id}: non-canonical entityMap keys {sorted(unknown)}"
