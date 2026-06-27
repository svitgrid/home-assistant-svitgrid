from custom_components.svitgrid.harvest.register_spec import (
    RegisterSpec, BUILTIN_CATALOG,
)

DEYE = {
    "modelId": "deye_sg04lp3", "version": 1, "source": "generated",
    "verified": True, "protocol": "solarman_v5", "port": 8899,
    "defaultSlaveId": 1,
    "flags": {"batteryPositiveIsDischarge": True},
    "reads": [
        {"field": "batterySoc", "address": 588},
        {"field": "batteryPower", "address": 590, "signed": True},
        {"field": "dailyPvEnergy", "address": 529, "scale": 0.1},
    ],
    "derivations": [
        {"field": "batteryPower", "op": "builtin",
         "builtin": "battery_sign_normalize", "inputs": ["batteryPower"]},
        {"field": "totalPvPower", "op": "sum", "inputs": ["pv1Power", "pv2Power"]},
    ],
    "writes": [],
}

def test_from_dict_parses_reads_and_flags():
    spec = RegisterSpec.from_dict(DEYE)
    assert spec.model_id == "deye_sg04lp3"
    assert spec.flags.battery_positive_is_discharge is True
    bp = next(r for r in spec.reads if r.field == "batteryPower")
    assert bp.signed is True and bp.scale == 1.0  # default
    soc = next(r for r in spec.reads if r.field == "batterySoc")
    assert soc.words == 1 and soc.unit_id == 1 and soc.function_code == "FC03"
    daily = next(r for r in spec.reads if r.field == "dailyPvEnergy")
    assert daily.scale == 0.1

def test_validate_rejects_unknown_builtin():
    d = {**DEYE, "derivations": [
        {"field": "x", "op": "builtin", "builtin": "nope", "inputs": ["batterySoc"]},
    ]}
    problems = RegisterSpec.from_dict(d).validate()
    assert any("nope" in p for p in problems)

def test_validate_rejects_dangling_input():
    d = {**DEYE, "derivations": [
        {"field": "x", "op": "sum", "inputs": ["batterySoc", "missing"]},
    ]}
    problems = RegisterSpec.from_dict(d).validate()
    assert any("missing" in p for p in problems)

def test_builtin_catalog_has_seven():
    assert BUILTIN_CATALOG == frozenset({
        "pv_power_from_vi", "battery_sign_normalize", "battery_temp_clamp",
        "phase_voltage_grid_or_load", "phase_load_ct_or_inverter",
        "grid_relay_bit", "daily_grid_unavailable",
    })
