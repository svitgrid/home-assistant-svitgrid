# tests/harvest/test_decoder.py
import math

from custom_components.svitgrid.harvest.decoder import decode
from custom_components.svitgrid.harvest.register_spec import RegisterSpec


def _spec(**over):
    base = {
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
    base.update(over)
    return RegisterSpec.from_dict(base)


def test_scaled_unsigned_read():
    spec = _spec(reads=[{"field": "batteryVoltage", "address": 587, "scale": 0.01}])
    assert math.isclose(decode(spec, {1: {587: 5230}})["batteryVoltage"], 52.30)


def test_signed_negative_read():
    spec = _spec(reads=[{"field": "gridPower", "address": 625, "signed": True}])
    assert decode(spec, {1: {625: 64536}})["gridPower"] == -1000.0


def test_unsigned_sentinel_0xffff_is_zero():
    spec = _spec(reads=[{"field": "ev", "address": 260}])
    assert decode(spec, {1: {260: 0xFFFF}})["ev"] == 0.0


def test_signed_sentinel_0x7fff_is_zero():
    spec = _spec(reads=[{"field": "x", "address": 1, "signed": True}])
    assert decode(spec, {1: {1: 0x7FFF}})["x"] == 0.0


def test_offset_after_scale():
    spec = _spec(reads=[{"field": "t", "address": 586, "scale": 0.1, "offset": -100.0}])
    assert math.isclose(decode(spec, {1: {586: 1290}})["t"], 29.0)


def test_32bit_unsigned():
    spec = _spec(reads=[{"field": "e", "address": 100, "words": 2, "scale": 0.01}])
    # hi=1, lo=0x86A0 → 0x000186A0 = 100000 → *0.01 = 1000.0
    assert math.isclose(decode(spec, {1: {100: 0x0001, 101: 0x86A0}})["e"], 1000.0)


def test_32bit_signed_negative():
    spec = _spec(reads=[{"field": "p", "address": 200, "words": 2, "signed": True}])
    # 0xFFFFFFFF → -1
    assert decode(spec, {1: {200: 0xFFFF, 201: 0xFFFF}})["p"] == -1.0


def test_32bit_missing_low_word_is_none():
    spec = _spec(reads=[{"field": "p", "address": 200, "words": 2}])
    assert decode(spec, {1: {200: 0x0001}})["p"] is None


def test_sum_and_product():
    spec = _spec(
        reads=[
            {"field": "v", "address": 1, "scale": 0.1},
            {"field": "i", "address": 2, "scale": 0.1},
            {"field": "a", "address": 3},
            {"field": "b", "address": 4},
        ],
        derivations=[
            {"field": "p", "op": "product", "inputs": ["v", "i"]},
            {"field": "s", "op": "sum", "inputs": ["a", "b"]},
        ],
    )
    out = decode(spec, {1: {1: 3000, 2: 50, 3: 100, 4: 200}})
    assert math.isclose(out["p"], 1500.0) and out["s"] == 300.0


def test_battery_sign_normalize_flips_and_clamps():
    spec = _spec(
        flags={"batteryPositiveIsDischarge": True},
        reads=[{"field": "batteryPower", "address": 590, "signed": True}],
        derivations=[
            {
                "field": "batteryPower",
                "op": "builtin",
                "builtin": "battery_sign_normalize",
                "inputs": ["batteryPower"],
            }
        ],
    )
    assert decode(spec, {1: {590: 1500}})["batteryPower"] == -1500.0


def test_battery_temp_clamp_out_of_range_is_none():
    spec = _spec(
        reads=[{"field": "t", "address": 586, "scale": 0.1, "offset": -100.0}],
        derivations=[
            {"field": "t", "op": "builtin", "builtin": "battery_temp_clamp", "inputs": ["t"]}
        ],
    )
    # raw 500 → -50°C → out of [-20,80] → None
    out = decode(spec, {1: {586: 500}})
    assert "t" in out and out["t"] is None


def test_per_unit_id_read():
    spec = _spec(protocol="modbus_tcp", reads=[{"field": "soc", "address": 843, "unitId": 100}])
    assert decode(spec, {100: {843: 87}})["soc"] == 87.0


# ---------------------------------------------------------------------------
# grid_sign_normalize (issue: absent from the Python catalog until 2026-08-17)
# Mirrors reference_decoder.dart:119-131.
# ---------------------------------------------------------------------------


def _grid_spec(*, inputs, flags, reads):
    return _spec(
        flags=flags,
        reads=reads,
        derivations=[
            {
                "field": "gridPower",
                "op": "builtin",
                "builtin": "grid_sign_normalize",
                "inputs": list(inputs),
            }
        ],
    )


def test_grid_sign_normalize_single_input_is_identity_without_flag():
    """One real total register, gridPositiveIsExport unset → value passes through."""
    spec = _grid_spec(
        inputs=["gridPower"],
        flags={},
        reads=[{"field": "gridPower", "address": 570, "signed": True}],
    )
    assert decode(spec, {1: {570: 1234}})["gridPower"] == 1234.0


def test_grid_sign_normalize_negates_when_grid_positive_is_export():
    """SRNE/KSTAR/Megarevo: raw positive = export, so the sign must flip."""
    spec = _grid_spec(
        inputs=["gridPower"],
        flags={"gridPositiveIsExport": True},
        reads=[{"field": "gridPower", "address": 570, "signed": True}],
    )
    assert decode(spec, {1: {570: 1234}})["gridPower"] == -1234.0


def test_grid_sign_normalize_sums_per_phase_legs():
    """kstar_hybrid_3p_12k / megarevo / srne_anenji: no total register, sum the legs."""
    spec = _grid_spec(
        inputs=["gridPowerL1", "gridPowerL2", "gridPowerL3"],
        flags={"gridPositiveIsExport": True},
        reads=[
            {"field": "gridPowerL1", "address": 1, "signed": True},
            {"field": "gridPowerL2", "address": 2, "signed": True},
            {"field": "gridPowerL3", "address": 3, "signed": True},
        ],
    )
    out = decode(spec, {1: {1: 100, 2: 200, 3: 300}})
    assert out["gridPower"] == -600.0


def test_grid_sign_normalize_missing_inputs_count_as_zero():
    """Dart folds `out[f] ?? 0` — a missing leg contributes 0, never None."""
    spec = _grid_spec(
        inputs=["gridPowerL1", "gridPowerL2"],
        flags={},
        reads=[
            {"field": "gridPowerL1", "address": 1, "signed": True},
            {"field": "gridPowerL2", "address": 2, "signed": True},
        ],
    )
    out = decode(spec, {1: {1: 100}})  # L2 absent from raw
    assert out["gridPower"] == 100.0


def test_grid_sign_normalize_collapses_negative_zero():
    """Dart's `-gp + 0.0` exists to kill IEEE-754 -0.0; Python must match."""
    spec = _grid_spec(
        inputs=["gridPower"],
        flags={"gridPositiveIsExport": True},
        reads=[{"field": "gridPower", "address": 570, "signed": True}],
    )
    out = decode(spec, {1: {570: 0}})
    assert out["gridPower"] == 0.0
    assert math.copysign(1.0, out["gridPower"]) == 1.0  # +0.0, not -0.0


# ---------------------------------------------------------------------------
# battery_power_from_vi — V×I, THEN sign-normalise, THEN the 50 kW clamp.
# Replaces battery_sign_normalize; never chains with it.
# Mirrors reference_decoder.dart:106-118.
# ---------------------------------------------------------------------------


def _bp_spec(flags):
    return _spec(
        flags=flags,
        reads=[
            {"field": "batteryVoltage", "address": 256, "scale": 0.1},
            {"field": "batteryCurrent", "address": 257, "scale": 0.1, "signed": True},
        ],
        derivations=[
            {
                "field": "batteryPower",
                "op": "builtin",
                "builtin": "battery_power_from_vi",
                "inputs": ["batteryVoltage", "batteryCurrent"],
            }
        ],
    )


def test_battery_power_from_vi_multiplies_voltage_by_current():
    spec = _bp_spec({})
    # 51.2 V × 20.0 A = 1024 W
    out = decode(spec, {1: {256: 512, 257: 200}})
    assert math.isclose(out["batteryPower"], 1024.0)


def test_battery_power_from_vi_negates_when_positive_is_discharge():
    """SRNE: batteryPositiveIsDischarge → V×I must be negated, unlike pv_power_from_vi."""
    spec = _bp_spec({"batteryPositiveIsDischarge": True})
    out = decode(spec, {1: {256: 512, 257: 200}})
    assert math.isclose(out["batteryPower"], -1024.0)


def test_battery_power_from_vi_clamps_above_50kw_to_zero():
    """|bp| > 50000 → 0.0 (sanity clamp, same as battery_sign_normalize)."""
    spec = _bp_spec({})
    # 600.0 V × 100.0 A = 60000 W → clamped
    out = decode(spec, {1: {256: 6000, 257: 1000}})
    assert out["batteryPower"] == 0.0


def test_battery_power_from_vi_clamp_applies_after_negation():
    """The clamp is on the magnitude AFTER the sign flip — a negative -60 kW clamps too."""
    spec = _bp_spec({"batteryPositiveIsDischarge": True})
    out = decode(spec, {1: {256: 6000, 257: 1000}})
    assert out["batteryPower"] == 0.0


def test_battery_power_from_vi_missing_input_is_zero_not_none():
    """Dart uses `?? 0`, so an absent register yields 0.0 W, not None."""
    spec = _bp_spec({})
    out = decode(spec, {1: {256: 512}})  # current absent
    assert out["batteryPower"] == 0.0


def test_pv_power_from_vi_still_has_no_sign_step_or_clamp():
    """Guard against copying the battery branch onto pv_power_from_vi (or vice versa)."""
    spec = _spec(
        flags={"batteryPositiveIsDischarge": True},
        reads=[
            {"field": "pv1Voltage", "address": 1, "scale": 0.1},
            {"field": "pv1Current", "address": 2, "scale": 0.1},
        ],
        derivations=[
            {
                "field": "pv1Power",
                "op": "builtin",
                "builtin": "pv_power_from_vi",
                "inputs": ["pv1Voltage", "pv1Current"],
            }
        ],
    )
    # 600.0 V × 100.0 A = 60000 W — NOT clamped, NOT negated.
    out = decode(spec, {1: {1: 6000, 2: 1000}})
    assert math.isclose(out["pv1Power"], 60000.0)


# ---------------------------------------------------------------------------
# lowWordFirst — Megarevo's six daily counters. Mirrors reference_decoder.dart:46-47.
# ---------------------------------------------------------------------------


def test_32bit_low_word_first_swaps_the_halves():
    spec = _spec(
        reads=[{"field": "e", "address": 100, "words": 2, "scale": 0.001, "lowWordFirst": True}]
    )
    # lowWordFirst: address 100 holds the LOW word, 101 the HIGH word.
    # hi=0x0001, lo=0x86A0 → 100000 → ×0.001 = 100.0
    assert math.isclose(decode(spec, {1: {100: 0x86A0, 101: 0x0001}})["e"], 100.0)


def test_32bit_default_is_high_word_first():
    spec = _spec(reads=[{"field": "e", "address": 100, "words": 2, "scale": 0.001}])
    assert math.isclose(decode(spec, {1: {100: 0x0001, 101: 0x86A0}})["e"], 100.0)


def test_32bit_low_word_first_sign_extends_after_assembly():
    spec = _spec(
        reads=[{"field": "p", "address": 200, "words": 2, "signed": True, "lowWordFirst": True}]
    )
    # 0xFFFFFFFF is symmetric; use a value that is only negative when swapped:
    # swapped → hi=0xFFFF lo=0x0001 → 0xFFFF0001 → -65535
    assert decode(spec, {1: {200: 0x0001, 201: 0xFFFF}})["p"] == -65535.0


def test_32bit_low_word_first_missing_word_is_none():
    """The presence guard stays order-INDEPENDENT: both words required either way."""
    spec = _spec(reads=[{"field": "p", "address": 200, "words": 2, "lowWordFirst": True}])
    assert decode(spec, {1: {200: 0x0001}})["p"] is None
    assert decode(spec, {1: {201: 0x0001}})["p"] is None
