# tests/harvest/test_battery_bus_current_2.py
#
# `battery_bus_current_2_sum` — the add-on half of svitgrid#553/#558.
#
# On the four LV 3-phase models that share DeyeSG04LP3Registers, reg 591 carries
# ONE current branch of a shared DC bus and reg 594 carries the other. An
# install that reads 591 alone reports about HALF the battery current the pack
# is really doing, with voltage, power and SOC all correct.
#
# This file exists because of the failure mode CLAUDE.md names for
# BUILTIN_CATALOG, and it is worse here than a wrong number: a builtin present
# in Dart's kBuiltinCatalog and absent here makes spec_health refuse the spec
# WHOLE, so the inverter pairs fine and then reports NOTHING — not a wrong
# current, no fields at all.
#
# The gate is the invariant the reading itself must satisfy, P = V x I: take the
# second current only when adding it moves V x I CLOSER to the power the
# inverter reports. A one-branch unit reads 594 = 0 and is left untouched; a 594
# holding something unrelated is pushed away from P and refused.
import math

from custom_components.svitgrid.harvest.decoder import decode
from custom_components.svitgrid.harvest.register_spec import (
    BUILTIN_CATALOG,
    RegisterSpec,
)


def _spec():
    """The shape the Dart exporter emits for deye_sg05lp3 and its three siblings."""
    return RegisterSpec.from_dict(
        {
            "modelId": "deye_sg05lp3",
            "version": 1,
            "protocol": "solarman_v5",
            "port": 8899,
            "defaultSlaveId": 1,
            "flags": {"batteryPositiveIsDischarge": True},
            "reads": [
                {"field": "batteryVoltage", "address": 587, "scale": 0.01},
                {"field": "batteryPower", "address": 590, "signed": True},
                {"field": "batteryCurrent", "address": 591, "signed": True, "scale": 0.01},
                {
                    "field": "batteryBusCurrent2",
                    "address": 594,
                    "signed": True,
                    "scale": 0.01,
                },
            ],
            "derivations": [
                {
                    "field": "batteryPower",
                    "op": "builtin",
                    "inputs": ["batteryPower"],
                    "builtin": "battery_sign_normalize",
                },
                {
                    "field": "batteryCurrent",
                    "op": "builtin",
                    "inputs": [
                        "batteryVoltage",
                        "batteryCurrent",
                        "batteryBusCurrent2",
                        "batteryPower",
                    ],
                    "builtin": "battery_bus_current_2_sum",
                },
            ],
            "writes": [],
        }
    )


def _decode(regs):
    return decode(_spec(), {1: dict(regs)})


# The four frames the firmware half (#553) and the Dart reader half (#558)
# replay, read from kus6MEagbUOV5UwcDDrT (deye_sg05lp3) on 2026-09-06.
TWO_BRANCH_DISCHARGE_1430W = {587: 5312, 590: 1430, 591: 1304, 594: 1298}
TWO_BRANCH_DISCHARGE_523W = {587: 5314, 590: 523, 591: 501, 594: 487}
ONE_BRANCH_CHARGE_2018W = {587: 5417, 590: 65536 - 2018, 591: 65536 - 3725, 594: 0}
ONE_BRANCH_STRAY_594 = {587: 5417, 590: 65536 - 2018, 591: 65536 - 3725, 594: 3725}


def test_builtin_is_in_the_catalogue():
    # Must stay identical to Dart's kBuiltinCatalog
    # (packages/inverter_protocol/lib/src/spec/builtin_catalog.dart).
    assert "battery_bus_current_2_sum" in BUILTIN_CATALOG


def test_two_branches_sum():
    out = _decode(TWO_BRANCH_DISCHARGE_1430W)
    assert math.isclose(out["batteryVoltage"], 53.12, abs_tol=0.01)
    assert math.isclose(out["batteryPower"], -1430.0, abs_tol=0.5)
    # RAW convention (positive = discharge on Deye): 13.04 + 12.98.
    assert math.isclose(out["batteryCurrent"], 26.02, abs_tol=0.01)


def test_two_branches_at_a_second_load():
    # 5.01 + 4.87 = 9.88. Reg 591 alone would read 5.01, i.e. half.
    out = _decode(TWO_BRANCH_DISCHARGE_523W)
    assert math.isclose(out["batteryCurrent"], 9.88, abs_tol=0.01)


def test_one_branch_is_left_alone():
    out = _decode(ONE_BRANCH_CHARGE_2018W)
    assert math.isclose(out["batteryPower"], 2018.0, abs_tol=0.5)
    assert math.isclose(out["batteryCurrent"], -37.25, abs_tol=0.01)


def test_a_stray_594_is_refused():
    out = _decode(ONE_BRANCH_STRAY_594)
    assert math.isclose(out["batteryCurrent"], -37.25, abs_tol=0.01)


def test_the_battery_power_register_is_untouched():
    # Reg 590 is already the BUS total on this platform. Summing the second
    # current branch must not change it, or the pack reads double the power.
    out = _decode(TWO_BRANCH_DISCHARGE_1430W)
    assert math.isclose(out["batteryPower"], -1430.0, abs_tol=0.5)


def test_the_sum_is_clamped_at_500_amps():
    # Both branches at 300 A against a power register that rewards the sum
    # (50 V x 600 A = 30 kW, exactly reg 590). 600 A is past the clamp, so the
    # first branch stands.
    out = _decode({587: 5000, 590: 30000, 591: 30000, 594: 30000})
    assert math.isclose(out["batteryCurrent"], 300.0, abs_tol=0.01)


def test_below_the_power_floor_nothing_is_decided():
    out = _decode({587: 5312, 590: 50, 591: 1304, 594: 1298})
    assert math.isclose(out["batteryCurrent"], 13.04, abs_tol=0.01)
