# tests/harvest/test_load_energy_balance.py
#
# `load_energy_balance` — the add-on half of a builtin added for SolaX
# X3-Hybrid G4, the first family in the catalogue that publishes NO house-load
# register.
#
# This file exists because of the failure mode CLAUDE.md names for
# BUILTIN_CATALOG: a builtin present in Dart's `kBuiltinCatalog` and absent
# here raises on EVERY tick, the harvest loop swallows it, and the inverter
# pairs fine then reports nothing indefinitely behind a debug-level log.
#
# `loadPower` is in CORE_PAYLOAD_FIELDS, so getting this wrong is not a missing
# field — it is a device that never publishes at all.
#
# The identity is `load = pv - battery + grid` on POST-normalise values:
# battery positive = charging (a consumer, subtracted), grid positive =
# importing (a source, added). Clamped at zero.
import math

import pytest

from custom_components.svitgrid.harvest.decoder import decode
from custom_components.svitgrid.harvest.register_spec import (
    BUILTIN_CATALOG,
    RegisterSpec,
)


def _spec():
    return RegisterSpec.from_dict(
        {
            "modelId": "m",
            "version": 1,
            "protocol": "modbus_tcp",
            "port": 8899,
            "defaultSlaveId": 1,
            "flags": {},
            "reads": [
                {"field": "totalPvPower", "address": 10, "signed": True},
                {"field": "batteryPower", "address": 11, "signed": True},
                {"field": "gridPower", "address": 12, "signed": True},
            ],
            "derivations": [
                {
                    "field": "loadPower",
                    "op": "builtin",
                    "inputs": ["totalPvPower", "batteryPower", "gridPower"],
                    "builtin": "load_energy_balance",
                }
            ],
            "writes": [],
        }
    )


def _s16(v):
    return v + 65536 if v < 0 else v


def _load(pv, battery, grid):
    raw = {1: {10: _s16(pv), 11: _s16(battery), 12: _s16(grid)}}
    return decode(_spec(), raw)["loadPower"]


def test_builtin_is_in_the_catalogue():
    # Must stay identical to Dart's kBuiltinCatalog
    # (packages/inverter_protocol/lib/src/spec/builtin_catalog.dart).
    assert "load_energy_balance" in BUILTIN_CATALOG


def test_the_real_capture_frame():
    # SolaX H34A15IB610126, 2026-08-23, off-grid: PV 1222 W, battery charging
    # 672 W, grid idle. The unit's own Total Off-Grid Power read 411 W, so the
    # derived value reads high by the conversion loss — by design.
    assert math.isclose(_load(1222, 672, 0), 550.0)


def test_a_charging_pack_is_subtracted():
    assert math.isclose(_load(4000, 1500, 0), 2500.0)


def test_a_discharging_pack_is_added():
    assert math.isclose(_load(0, -800, 0), 800.0)


def test_grid_import_adds():
    assert math.isclose(_load(0, 0, 1500), 1500.0)


def test_grid_export_subtracts():
    assert math.isclose(_load(5000, 0, -3000), 2000.0)


def test_night_charging_from_the_grid():
    # The regime where a sign error in either term is loudest. Inputs here are
    # POST-normalise, so importing 2200 W is a POSITIVE gridPower — the raw
    # register's positive-on-export convention has already been applied by
    # grid_sign_normalize upstream.
    assert math.isclose(_load(0, 2000, 2200), 200.0)


def test_never_negative():
    assert math.isclose(_load(100, 0, -900), 0.0)


def test_a_missing_input_yields_none_not_a_partial_sum():
    # gridPower never lands, so the field is absent. A load assembled from two
    # of three terms is a confident wrong number, and this field gates
    # publication.
    spec = _spec()
    out = decode(spec, {1: {10: 1222, 11: 672}})
    assert out["loadPower"] is None


def test_validate_accepts_a_spec_using_the_builtin():
    # The catalogue membership check runs inside RegisterSpec.validate; an
    # unknown builtin there is what makes the whole spec unusable.
    assert _spec().validate() == []
