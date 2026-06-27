# tests/harvest/test_sanitize.py
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.decoder import sanitize

def _spec():
    return RegisterSpec.from_dict({
        "modelId": "m", "version": 1, "protocol": "solarman_v5", "port": 8899,
        "defaultSlaveId": 1, "flags": {}, "reads": [], "derivations": [], "writes": [],
    })

def test_battery_soc_clamped_high():
    assert sanitize({"batterySoc": 120.0}, _spec())["batterySoc"] == 100.0

def test_battery_soc_clamped_low():
    assert sanitize({"batterySoc": -5.0}, _spec())["batterySoc"] == 0.0

def test_battery_soc_in_range_untouched():
    assert sanitize({"batterySoc": 73.0}, _spec())["batterySoc"] == 73.0

def test_none_and_absent_untouched():
    out = sanitize({"batterySoc": None, "gridPower": 500.0}, _spec())
    assert out["batterySoc"] is None and out["gridPower"] == 500.0

def test_pure_does_not_mutate_input():
    src = {"batterySoc": 120.0}
    sanitize(src, _spec())
    assert src["batterySoc"] == 120.0
