# tests/harvest/test_decoder.py
import math

from custom_components.svitgrid.harvest.decoder import decode
from custom_components.svitgrid.harvest.register_spec import RegisterSpec


def _spec(**over):
    base = {
        "modelId": "m", "version": 1, "protocol": "solarman_v5", "port": 8899,
        "defaultSlaveId": 1, "flags": {}, "reads": [], "derivations": [], "writes": [],
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
        reads=[{"field": "v", "address": 1, "scale": 0.1},
               {"field": "i", "address": 2, "scale": 0.1},
               {"field": "a", "address": 3}, {"field": "b", "address": 4}],
        derivations=[
            {"field": "p", "op": "product", "inputs": ["v", "i"]},
            {"field": "s", "op": "sum", "inputs": ["a", "b"]},
        ])
    out = decode(spec, {1: {1: 3000, 2: 50, 3: 100, 4: 200}})
    assert math.isclose(out["p"], 1500.0) and out["s"] == 300.0

def test_battery_sign_normalize_flips_and_clamps():
    spec = _spec(
        flags={"batteryPositiveIsDischarge": True},
        reads=[{"field": "batteryPower", "address": 590, "signed": True}],
        derivations=[{"field": "batteryPower", "op": "builtin",
                      "builtin": "battery_sign_normalize", "inputs": ["batteryPower"]}])
    assert decode(spec, {1: {590: 1500}})["batteryPower"] == -1500.0

def test_battery_temp_clamp_out_of_range_is_none():
    spec = _spec(
        reads=[{"field": "t", "address": 586, "scale": 0.1, "offset": -100.0}],
        derivations=[{"field": "t", "op": "builtin",
                      "builtin": "battery_temp_clamp", "inputs": ["t"]}])
    # raw 500 → -50°C → out of [-20,80] → None
    out = decode(spec, {1: {586: 500}})
    assert "t" in out and out["t"] is None

def test_per_unit_id_read():
    spec = _spec(protocol="modbus_tcp",
                 reads=[{"field": "soc", "address": 843, "unitId": 100}])
    assert decode(spec, {100: {843: 87}})["soc"] == 87.0
