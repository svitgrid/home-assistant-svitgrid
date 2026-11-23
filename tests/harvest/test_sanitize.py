# tests/harvest/test_sanitize.py
from custom_components.svitgrid.harvest.decoder import sanitize
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


def _spec_with(reads):
    d = {
        "modelId": "m",
        "version": 1,
        "protocol": "solarman_v5",
        "port": 8899,
        "defaultSlaveId": 1,
        "flags": {},
        "reads": reads,
        "derivations": [],
        "writes": [],
    }
    return RegisterSpec.from_dict(d)


def test_soc_stays_absent_when_battery_is_read_without_an_soc_register():
    # The MUST PV1800 shape: a battery-voltage register and no SOC register.
    # Dart reports SOC absent (hasBatterySoc false); 0 % would be a confident
    # wrong value.
    spec = _spec_with([{"field": "batteryVoltage", "address": 100}])
    out = sanitize({"batteryVoltage": 52.0}, spec)
    assert out.get("batterySoc") is None


def test_daily_pv_stays_absent_without_a_daily_pv_register():
    # Dart's hasDailyPvEnergy is false when the map has no daily PV register.
    out = sanitize({"gridPower": 500.0}, _spec())
    assert out.get("dailyPvEnergy") is None


def test_daily_pv_zero_filled_when_a_per_string_register_exists():
    spec = _spec_with([{"field": "dailyPv1Energy", "address": 200}])
    out = sanitize({"gridPower": 500.0}, spec)
    assert out["dailyPvEnergy"] == 0.0
