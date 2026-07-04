"""Battery-power sign convention for HA-Solarman-sourced inverters.

The Home Assistant "Solarman" integration exposes battery power as
DISCHARGE-positive / CHARGE-negative — the inverse of Svitgrid's convention
(charge positive, discharge negative). We normalize at capture (so the local
store + branded panel show the right direction) and re-invert at upload (so the
server's existing `home_assistant_solarman` negation still yields correct cloud
data). See battery_sign.py for the full rationale.
"""

from __future__ import annotations

from custom_components.svitgrid.battery_sign import (
    flip_battery_sign,
    preset_is_discharge_positive,
)


def test_solarman_preset_is_discharge_positive():
    assert preset_is_discharge_positive("deye-sg05lp1-solarman-v1") is True
    assert preset_is_discharge_positive("deye-sg04lp3-solarman-v1") is True
    assert preset_is_discharge_positive("deye-sg02hp3-80k-solarman-v1") is True


def test_non_solarman_presets_are_not_discharge_positive():
    # Raw home_assistant presets (e.g. anenji) + manual (no preset) → no flip.
    assert preset_is_discharge_positive("anenji-esphome-v1") is False
    assert preset_is_discharge_positive("anenji-generic-v1") is False
    assert preset_is_discharge_positive(None) is False
    assert preset_is_discharge_positive("") is False


def test_flip_negates_battery_power():
    out = flip_battery_sign({"batteryPower": -800.0, "batterySoc": 49.0})
    assert out["batteryPower"] == 800.0
    assert out["batterySoc"] == 49.0  # other fields untouched


def test_flip_is_pure_does_not_mutate_input():
    src = {"batteryPower": 1177.0}
    out = flip_battery_sign(src)
    assert src["batteryPower"] == 1177.0  # input unchanged
    assert out["batteryPower"] == -1177.0


def test_flip_no_battery_power_is_noop():
    src = {"pvPower": 500.0}
    assert flip_battery_sign(src) == src


def test_flip_non_numeric_battery_power_is_noop():
    src = {"batteryPower": None}
    assert flip_battery_sign(src) == src
