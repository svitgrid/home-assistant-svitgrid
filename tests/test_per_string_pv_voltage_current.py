"""Per-string PV voltage/current must be mappable, renamed and mapped in presets.

Why this exists
---------------
The app's solar detail card shows a "354.0 V · 1.0 A" subline under each PV
string. On 2026-08-18 a prod census found it blank for EVERY Home Assistant
household: `pvVoltage*` was present on 65 of 65 edge-firmware inverters and 0 of
7 HA ones. The cause here is that `MAPPABLE_FIELDS` only ever listed
`pv1Power..pv4Power` — there was no per-string voltage or current field to map,
so no entity map could ever carry one and no preset could ship one
(`test_every_preset_uses_canonical_entity_map_keys` locks preset keys to
`ALL_FIELDS`).

The failure is silent by construction: an absent key is a legal shape for a
model that doesn't report the value, so nothing errors and the blank subline
reads as a UI bug.

Three things have to line up, and each gets a test below:
  1. the fields exist in ALL_FIELDS / MAPPABLE_FIELDS (or nothing can map them),
  2. `assemble_payload` emits them under the API's canonical `pvVoltageN` /
     `pvCurrentN` names (the ingest schema does not recognise `pvNVoltage`;
     it is only rescued by a server-side alias we should not depend on), and
  3. the Solarman presets actually map them, or 113 of 122 HA households — the
     ones that carry a `presetId` and receive mappings by add-only merge — see
     no change at all.
"""

from __future__ import annotations

import glob
import os
import re

import pytest
import yaml

from custom_components.svitgrid.const import ALL_FIELDS, MAPPABLE_FIELDS
from custom_components.svitgrid.readings_publisher import (
    assemble_payload,
    unresolved_fields,
)

PRESETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "presets")

PER_STRING_VI = [f"pv{n}{q}" for n in (1, 2, 3, 4) for q in ("Voltage", "Current")]

# A preset whose pvNPower points at a REAL per-string sensor — `..._pvN_power`.
# Victron / Huawei / Solplanet / Solis map a single combined array total onto
# pv1Power (`sensor.victron_pv_power`, `sensor.inverter_total_dc_power`), which
# has no per-string voltage or current to go with it.
_REAL_STRING_SENSOR = re.compile(r"_pv(\d)_(?:average_)?power$")


def _presets():
    return sorted(glob.glob(os.path.join(PRESETS_DIR, "*.yaml")))


# Presets that map real per-string powers but are deliberately NOT extended.
# Each entry must say why.
EXEMPT_PRESETS = {
    # Anenji is ESPHome / a generic HA poller rather than the Solarman HACS
    # integration, so its per-string voltage/current entity ids (if it exposes
    # any) are not the `sensor.inverter_pvN_voltage` shape and nobody has read
    # them off a live install. 5 households. Guessing an id here would cost
    # nothing (the field is skipped) but would put an unverified claim in a
    # file that reads as verified.
    "anenji-esphome-v1",
    "anenji-generic-v1",
}


def test_per_string_voltage_and_current_are_mappable():
    keys = {field for field, _label in MAPPABLE_FIELDS}
    for field in PER_STRING_VI:
        assert field in ALL_FIELDS, f"{field} missing from ALL_FIELDS"
        assert field in keys, f"{field} missing from MAPPABLE_FIELDS"


def test_assemble_payload_emits_canonical_api_names():
    """`pv1Voltage` is not a key the ingest schema knows — `pvVoltage1` is."""
    payload = assemble_payload(
        inverter_id="inv-1",
        fields={
            "pv1Power": 2000.0,
            "pv1Voltage": 310.6,
            "pv1Current": 7.2,
            "pv2Power": 1800.0,
            "pv2Voltage": 354.0,
            "pv2Current": 1.0,
        },
    )
    assert payload["pvVoltage1"] == 310.6
    assert payload["pvCurrent1"] == 7.2
    assert payload["pvVoltage2"] == 354.0
    assert payload["pvCurrent2"] == 1.0
    for internal in ("pv1Voltage", "pv1Current", "pv2Voltage", "pv2Current"):
        assert internal not in payload, f"{internal} left under its internal name"


def test_per_string_voltage_current_do_not_inflate_pvpower():
    """The pvPower aggregate sums per-string POWER only. Adding similarly
    named voltage/current keys must not leak into that sum — 310.6 V counted
    as watts would read as a plausible-looking extra 310 W of solar."""
    payload = assemble_payload(
        inverter_id="inv-1",
        fields={
            "pv1Power": 2000.0,
            "pv1Voltage": 310.6,
            "pv1Current": 7.2,
        },
    )
    assert payload["pvPower"] == 2000.0


def test_unresolved_fields_accounts_for_the_rename():
    """A mapped per-string voltage that DID resolve must not be reported as
    unresolved just because its key changed on the way out — that would send
    every HA user to Diagnostics chasing a healthy sensor."""
    entity_map = {"pv1Voltage": "sensor.inverter_pv1_voltage"}
    payload = assemble_payload(inverter_id="i", fields={"pv1Voltage": 310.6})
    assert unresolved_fields(payload, entity_map) == []


def test_unresolved_fields_still_reports_a_missing_per_string_sensor():
    entity_map = {"pv1Voltage": "sensor.inverter_pv1_voltage"}
    payload = assemble_payload(inverter_id="i", fields={})
    assert unresolved_fields(payload, entity_map) == ["pv1Voltage"]


@pytest.mark.parametrize("path", _presets(), ids=lambda p: os.path.basename(p)[:-5])
def test_real_per_string_presets_map_voltage_and_current(path):
    """If a preset maps a REAL per-string power sensor, it must map that
    string's voltage and current too. Without this the add-on change is inert:
    the fields become mappable, and nobody maps them."""
    with open(path) as fh:
        preset = yaml.safe_load(fh)
    if preset["id"] in EXEMPT_PRESETS:
        pytest.skip(f"{preset['id']} is a documented exemption")
    entity_map = preset["entityMap"]

    strings = sorted(
        int(m.group(1))
        for field, entity in entity_map.items()
        if field.endswith("Power")
        for m in [_REAL_STRING_SENSOR.search(entity)]
        if m and field == f"pv{m.group(1)}Power"
    )
    if not strings:
        pytest.skip("no real per-string power sensors in this preset")

    for n in strings:
        for quantity in ("Voltage", "Current"):
            field = f"pv{n}{quantity}"
            assert field in entity_map, (
                f"{preset['id']} maps pv{n}Power but not {field} — the app's "
                f"per-string 'V · A' subline stays blank for these households"
            )
