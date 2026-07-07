"""Fleet coverage guard for HA presets.

The HA pairing dropdown is fed from the haPresets Firestore collection, seeded
from presets/*.yaml. Every inverter model the mobile onboarding picker shows
(InverterModels.pickerModels in the svitgrid monorepo,
packages/inverter_protocol/lib/src/registers/models_catalog.dart) must have a
preset here, or it is invisible in the HA flow.

EXPECTED_FLEET mirrors pickerModels. Adding a model to the catalog means adding
a line here AND the presets/<doc-id>.yaml file — otherwise this test fails CI.
The two repos have no shared source of truth, so this list is the seam; keep it
in step with models_catalog.dart.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from custom_components.svitgrid.const import ALL_FIELDS

PRESETS_DIR = pathlib.Path(__file__).resolve().parent.parent / "presets"

# catalog model id  ->  preset doc id (== presets/<doc-id>.yaml)
# Mirrors InverterModels.pickerModels. Solarman models use the `-solarman-v1`
# suffix; home_assistant (Victron/Huawei/Solplanet/Anenji-class) use `-v1`.
EXPECTED_FLEET: dict[str, str] = {
    # ── Deye 3-phase hybrids ──
    "deye_sg04lp3": "deye-sg04lp3-solarman-v1",
    "deye_sg01hp3": "deye-sg01hp3-solarman-v1",
    "deye_sg01hp3_50k": "deye-sg01hp3-50k-solarman-v1",
    "deye_sg05lp3": "deye-sg05lp3-solarman-v1",
    "deye_gb_s20k": "deye-gb-s20k-solarman-v1",
    # ── Deye 1-phase hybrids ──
    "deye_sg03lp1": "deye-sg03lp1-solarman-v1",
    "deye_sun_g3": "deye-sun-g3-solarman-v1",
    "deye_sg01lp1": "deye-sg01lp1-solarman-v1",
    "deye_sg02lp1": "deye-sg02lp1-solarman-v1",
    "deye_sg01lp1_us": "deye-sg01lp1-us-solarman-v1",
    "deye_sg04lp1": "deye-sg04lp1-solarman-v1",
    "deye_sg05lp1": "deye-sg05lp1-solarman-v1",
    "deye_sg01lp1_16k": "deye-sg01lp1-16k-solarman-v1",
    # ── Deye grid-tie (no battery) ──
    "deye_string": "deye-string-solarman-v1",
    "deye_micro": "deye-micro-solarman-v1",
    "deye_sun_60k_g03": "deye-sun-60k-g03-solarman-v1",
    # ── Victron (home_assistant) ──
    "victron_multiplus_ii_gx_6k5": "victron-multiplus-ii-gx-6k5-v1",
    "victron_multiplus_ii_3k": "victron-multiplus-ii-3k-v1",
    "victron_multiplus_ii_5k": "victron-multiplus-ii-5k-v1",
    "victron_multiplus_ii_10k": "victron-multiplus-ii-10k-v1",
    "victron_multiplus_ii_15k": "victron-multiplus-ii-15k-v1",
    "victron_multiplus_ii_4k": "victron-multiplus-ii-4k-v1",
    "victron_multiplus_ii_8k": "victron-multiplus-ii-8k-v1",
    "victron_quattro_ii_5k": "victron-quattro-ii-5k-v1",
    # ── Huawei SUN2000 (home_assistant) ──
    "huawei_sun2000_30ktl_m3": "huawei-sun2000-30ktl-m3-v1",
    "huawei_sun2000_50ktl_m3": "huawei-sun2000-50ktl-m3-v1",
    "huawei_sun2000_150k_mg0": "huawei-sun2000-150k-mg0-v1",
    # ── Solplanet ASW-LT (home_assistant) ──
    "solplanet_asw100k_lt": "solplanet-asw100k-lt-v1",
    "solplanet_asw110k_lt": "solplanet-asw110k-lt-v1",
}


def _load(doc_id: str) -> dict:
    return yaml.safe_load((PRESETS_DIR / f"{doc_id}.yaml").read_text())


def test_expected_fleet_size():
    # 29 pickerModels. Guards against a half-edited EXPECTED_FLEET.
    assert len(EXPECTED_FLEET) == 29


@pytest.mark.parametrize("model_id,doc_id", sorted(EXPECTED_FLEET.items()))
def test_every_picker_model_has_a_preset(model_id, doc_id):
    path = PRESETS_DIR / f"{doc_id}.yaml"
    assert path.exists(), f"{model_id}: missing preset {path.name}"
    preset = _load(doc_id)
    assert preset["id"] == doc_id, f"{doc_id}: id field must equal the doc id"


def _all_preset_files() -> list[pathlib.Path]:
    return sorted(PRESETS_DIR.glob("*.yaml"))


@pytest.mark.parametrize("path", _all_preset_files(), ids=lambda p: p.stem)
def test_every_preset_uses_canonical_entity_map_keys(path):
    """A typo'd entityMap key is sent and silently stripped by the API, so the
    field never lights up. Lock every preset to ALL_FIELDS, not just the
    3-phase-hybrid subset."""
    preset = yaml.safe_load(path.read_text())
    unknown = set(preset["entityMap"]) - ALL_FIELDS
    assert not unknown, f"{path.name}: non-canonical entityMap keys {sorted(unknown)}"
