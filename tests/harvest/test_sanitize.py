# tests/harvest/test_sanitize.py
from custom_components.svitgrid.harvest.decoder import sanitize, spec_measures_grid
from custom_components.svitgrid.harvest.register_spec import RegisterSpec


def _spec():
    return RegisterSpec.from_dict(
        {
            "modelId": "m",
            "version": 1,
            "protocol": "solarman_v5",
            "port": 8899,
            "defaultSlaveId": 1,
            "flags": {},
            "reads": [],
            "derivations": [],
            "writes": [],
        }
    )


def test_battery_soc_clamped_high():
    assert sanitize({"batterySoc": 120.0}, _spec())["batterySoc"] == 100.0


def test_battery_soc_clamped_low():
    assert sanitize({"batterySoc": -5.0}, _spec())["batterySoc"] == 0.0


def test_battery_soc_in_range_untouched():
    assert sanitize({"batterySoc": 73.0}, _spec())["batterySoc"] == 73.0


def test_present_none_untouched():
    out = sanitize({"batterySoc": None, "gridPower": 500.0}, _spec())
    assert out["batterySoc"] is None and out["gridPower"] == 500.0


def test_present_none_standard_field_not_injected_to_zero():
    # a DEFINED-but-failed read (present, value None) must stay None, not become 0.0
    out = sanitize({"batterySoc": None, "gridPower": 500.0}, _spec())
    assert out["batterySoc"] is None


def test_structurally_absent_standard_field_becomes_zero():
    # a field with NO key at all (spec defines no read, e.g. grid-tie battery) -> 0.0
    out = sanitize({"gridPower": 500.0}, _spec())
    assert out["batterySoc"] == 0.0


def test_pure_does_not_mutate_input():
    src = {"batterySoc": 120.0}
    sanitize(src, _spec())
    assert src["batterySoc"] == 120.0


def test_structurally_absent_grid_power_is_zero_filled_for_dart_parity():
    # The golden vectors hold sanitize() to the Dart reader, which zero-fills.
    # The upload path, not sanitize(), keeps that 0 out of the payload.
    assert sanitize({"batterySoc": 50.0}, _spec())["gridPower"] == 0.0


def test_spec_measures_grid_is_false_without_a_grid_read_or_derivation():
    assert spec_measures_grid(_spec()) is False


def test_spec_measures_grid_is_true_for_a_grid_read():
    spec = RegisterSpec.from_dict(
        {
            "modelId": "m",
            "version": 1,
            "protocol": "solarman_v5",
            "port": 8899,
            "defaultSlaveId": 1,
            "flags": {},
            "reads": [{"field": "gridPower", "address": 625, "signed": True}],
            "derivations": [],
            "writes": [],
        }
    )
    assert spec_measures_grid(spec) is True


def test_spec_measures_grid_is_true_for_a_grid_derivation():
    spec = RegisterSpec.from_dict(
        {
            "modelId": "m",
            "version": 1,
            "protocol": "solarman_v5",
            "port": 8899,
            "defaultSlaveId": 1,
            "flags": {},
            "reads": [{"field": "gridPowerL1", "address": 1, "signed": True}],
            "derivations": [
                {
                    "field": "gridPower",
                    "op": "builtin",
                    "builtin": "grid_sign_normalize",
                    "inputs": ["gridPowerL1"],
                }
            ],
            "writes": [],
        }
    )
    assert spec_measures_grid(spec) is True
