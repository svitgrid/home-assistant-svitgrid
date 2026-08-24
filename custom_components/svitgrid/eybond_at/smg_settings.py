"""The EASUN SMG II configuration block, as a catalogue.

Ported from `packages/inverter_protocol/lib/src/protocol/eybond_at/smg_settings.dart`
in the Flutter repo -- same addresses, same scales, same constraints. See that
file's docstring and `docs/inverter-research/2026-08-20-anenji-smg-ii-register-families.md`
(main svitgrid repo) for the provenance of every number here: serial
99432604107106, cross-checked against `syssi/esphome-smg-ii`.

## Ranges are measured, not assumed

Bounds come from the photographed nameplate (ANJ-4.2KW-24V-W-Pro: 24 VDC, AC
charger DC output 27 V at max 120 A) and from the 24 V lead-acid profile the
unit actually holds. A system voltage nobody has measured publishes NOTHING,
for the same reason an unrecognised protocol number decodes nothing: the 48 V
setpoint scale is unresolved, and a 2x error on a charge voltage is not a
display bug.

## The constraints that matter are between fields

Every value in a damaging configuration can be individually in range. A float
above a bulk never leaves absorption; a bulk above the overvoltage trip
charges straight into it; a low-DC cut-off above the back-to-utility SOC cuts
off before it ever switches. `validate_smg_settings` is where that lives.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class SmgRisk(str, Enum):
    """How much damage a wrong value here can do."""

    ROUTINE = "routine"
    """Wrong is annoying: a buzzer, a backlight, an output frequency."""

    PROTECTIVE = "protective"
    """Wrong can flatten or cook a battery pack."""


@dataclass(frozen=True)
class SmgSetting:
    """One writable configuration register."""

    key: str
    address: int

    # Raw register units per display unit. 0.1 means the register holds
    # decivolts and the user sees volts.
    scale: float
    unit: str
    decimals: int

    # Inclusive bounds, in RAW register units.
    raw_min: int
    raw_max: int

    risk: SmgRisk = SmgRisk.ROUTINE

    # True when raw_min/raw_max were scaled from another pack voltage rather
    # than measured. A derived bound is a claim about hardware nobody has
    # read, so it is checked against the device before any write (see
    # SmgSettingsExecutor).
    bounds_derived: bool = False

    def to_display(self, raw: int) -> float:
        return raw * self.scale

    def to_raw(self, display: float) -> int:
        """Rounds rather than truncates: 28.2 V arrives as 28.199999999999996
        in a float, and truncating would write 281 for a setpoint the user
        typed as 28.2."""
        return round(display / self.scale)

    def contains(self, raw: int) -> bool:
        return self.raw_min <= raw <= self.raw_max


# Settings whose meaning and bounds do not depend on pack voltage: currents in
# amps, SOC thresholds in percent, equalisation timers, the AC output, and the
# buzzer. Correct for every pack on this map.
_PACK_INDEPENDENT: tuple[SmgSetting, ...] = (
    # Nameplate: AC charger DC output 27 V, max 120 A (default 30 A).
    SmgSetting(
        key="maxChargeCurrent", address=332, scale=0.1, unit="A", decimals=1,
        raw_min=0, raw_max=1200,  # 600 = 60.0 A
    ),
    SmgSetting(
        key="maxMainsChargeCurrent", address=333, scale=0.1, unit="A", decimals=1,
        raw_min=0, raw_max=1200,  # 300 = 30.0 A
    ),

    # -- protection ----------------------------------------------------------
    SmgSetting(
        key="socBackToUtility", address=341, scale=1.0, unit="%", decimals=0,
        raw_min=0, raw_max=100, risk=SmgRisk.PROTECTIVE,  # 20
    ),
    SmgSetting(
        key="socBackToBattery", address=342, scale=1.0, unit="%", decimals=0,
        raw_min=0, raw_max=100, risk=SmgRisk.PROTECTIVE,  # 30
    ),
    SmgSetting(
        key="socLowDcCutoff", address=343, scale=1.0, unit="%", decimals=0,
        raw_min=0, raw_max=100, risk=SmgRisk.PROTECTIVE,  # 15
    ),

    # -- equalization ----------------------------------------------------------
    SmgSetting(
        key="equalizationEnabled", address=313, scale=1.0, unit="", decimals=0,
        raw_min=0, raw_max=1,  # 0 = disabled
    ),
    SmgSetting(
        key="equalizationMinutes", address=335, scale=1.0, unit="min", decimals=0,
        raw_min=5, raw_max=900,  # 60
    ),
    SmgSetting(
        key="equalizationTimeoutMinutes", address=336, scale=1.0, unit="min", decimals=0,
        raw_min=5, raw_max=900,  # 120
    ),
    SmgSetting(
        key="equalizationIntervalDays", address=337, scale=1.0, unit="d", decimals=0,
        raw_min=0, raw_max=90,  # 30
    ),

    # -- output ----------------------------------------------------------
    SmgSetting(
        key="outputVoltage", address=320, scale=0.1, unit="V", decimals=1,
        raw_min=2000, raw_max=2400,  # 2300 = 230.0 V
    ),
    SmgSetting(
        key="outputFrequency", address=321, scale=0.01, unit="Hz", decimals=2,
        raw_min=5000, raw_max=6000,  # 5000 = 50.00 Hz
    ),

    # -- device ----------------------------------------------------------
    SmgSetting(
        key="buzzerMode", address=303, scale=1.0, unit="", decimals=0,
        raw_min=0, raw_max=3,  # 3 = faults only
    ),
)

# The six DC voltage setpoints, measured on the 24 V bench unit. Bench values
# in comments are what the unit held.
_PACK_DEPENDENT_24V: tuple[SmgSetting, ...] = (
    # -- charge profile ----------------------------------------------------------
    SmgSetting(
        key="maxChargeVoltage", address=324, scale=0.1, unit="V", decimals=1,
        raw_min=240, raw_max=320, risk=SmgRisk.PROTECTIVE,  # 282 = 28.2 V
    ),
    SmgSetting(
        key="floatChargeVoltage", address=325, scale=0.1, unit="V", decimals=1,
        raw_min=240, raw_max=300, risk=SmgRisk.PROTECTIVE,  # 270 = 27.0 V
    ),

    # -- protection ----------------------------------------------------------
    SmgSetting(
        key="batteryOverVoltage", address=323, scale=0.1, unit="V", decimals=1,
        raw_min=260, raw_max=340, risk=SmgRisk.PROTECTIVE,  # 320 = 32.0 V
    ),
    SmgSetting(
        key="lowVoltageCutoffOnMains", address=327, scale=0.1, unit="V", decimals=1,
        raw_min=190, raw_max=260, risk=SmgRisk.PROTECTIVE,  # 230 = 23.0 V
    ),
    SmgSetting(
        key="lowVoltageCutoffOffGrid", address=329, scale=0.1, unit="V", decimals=1,
        raw_min=180, raw_max=250, risk=SmgRisk.PROTECTIVE,  # 210 = 21.0 V
    ),

    # -- equalization ----------------------------------------------------------
    SmgSetting(
        key="equalizationVoltage", address=334, scale=0.1, unit="V", decimals=1,
        raw_min=240, raw_max=320, risk=SmgRisk.PROTECTIVE,  # 292 = 29.2 V
    ),
)

# Register 184 reports the protocol number. 11 is the EASUN/ISolar SMG II map,
# which is what the bench unit reads and what this catalogue describes.
smg_ii_protocol_number = 11

# Addresses of the six pack-dependent DC setpoints, for the doubling below.
_PACK_DEPENDENT_ADDRESSES = tuple(s.address for s in _PACK_DEPENDENT_24V)


def _scaled_from_24v(multiplier: int) -> tuple[SmgSetting, ...]:
    """The 24 V table restated for another pack, by scaling the bounds.

    The SCALE is not touched: register 324 holds 282 for 28.2 V on the bench
    unit, so this map is true volts at 0.1 V/LSB and stays that way. Only the
    accepted RANGE moves with the pack. (The 12 V-equivalent encoding belongs
    to the SRNE/EyBond map, a different protocol number on the same brand --
    do not carry that finding here.)
    """
    return tuple(
        SmgSetting(
            key=s.key,
            address=s.address,
            scale=s.scale,
            unit=s.unit,
            decimals=s.decimals,
            raw_min=s.raw_min * multiplier,
            raw_max=s.raw_max * multiplier,
            risk=s.risk,
            bounds_derived=True,
        )
        for s in _PACK_DEPENDENT_24V
    )


def _pack_dependent_for(nominal_pack_voltage: int) -> tuple[SmgSetting, ...]:
    if nominal_pack_voltage == 24:
        return _PACK_DEPENDENT_24V
    if nominal_pack_voltage == 48:
        return _scaled_from_24v(2)
    return ()


def smg_settings_for(*, protocol_number: int, nominal_pack_voltage: int) -> list[SmgSetting]:
    """The writable catalogue for a device, or an empty list when we cannot say.

    Gated on the PROTOCOL NUMBER, not the model or the pack: register 184 is
    what selects the register map at runtime, and picking a map by brand or
    model name is what produced a wrong map for this family once already.

    A pack voltage with no bounds table still gets every pack-independent
    setting, because those are correct regardless of what the battery is.
    """
    if protocol_number != smg_ii_protocol_number:
        return []
    pack_dependent = _pack_dependent_for(nominal_pack_voltage)
    return [*_PACK_INDEPENDENT, *pack_dependent]


@dataclass(frozen=True)
class SmgConstraintViolation:
    """A combination the device would accept but a battery would not survive."""

    # The registers involved, so the UI can mark both.
    addresses: tuple[int, int]
    key: str

    def __str__(self) -> str:  # pragma: no cover - debug aid only
        return f"SmgConstraintViolation({self.key}, registers {self.addresses})"


def _strictly_below(a: int, b: int) -> bool:
    return a < b


@dataclass(frozen=True)
class _Constraint:
    key: str
    low: int
    high: int
    # True when the values are acceptable.
    ok: Callable[[int, int], bool]


_CONSTRAINTS: tuple[_Constraint, ...] = (
    # Float must sit below bulk, or the pack never leaves absorption.
    _Constraint("floatBelowBulk", 325, 324, _strictly_below),
    # Bulk must sit below the overvoltage trip, or charging trips protection.
    _Constraint("bulkBelowOverVoltage", 324, 323, _strictly_below),
    # Equalization is a deliberate overcharge: above bulk, below the trip.
    _Constraint("bulkBelowEqualization", 324, 334, _strictly_below),
    _Constraint("equalizationBelowOverVoltage", 334, 323, _strictly_below),
    # Off-grid may run the pack lower than on mains, never higher.
    _Constraint("offGridCutoffBelowMainsCutoff", 329, 327, _strictly_below),
    # Cut off below the point we would have switched to utility, not above it.
    _Constraint("lowDcCutoffBelowBackToUtility", 343, 341, _strictly_below),
    # Return to battery only above the point we left it.
    _Constraint("backToUtilityBelowBackToBattery", 341, 342, _strictly_below),
)


def validate_smg_settings(values: dict[int, int]) -> list[SmgConstraintViolation]:
    """Every cross-field constraint `values` violates.

    `values` is keyed by register address and holds RAW values. A constraint
    whose fields are not both present is skipped rather than failed, so a
    partial apply is validated on what it actually carries.

    Returns ALL violations rather than stopping at the first: these fields are
    interdependent, the write path is slow, and fixing them one at a time is a
    poor way to spend it.
    """
    violations: list[SmgConstraintViolation] = []
    for c in _CONSTRAINTS:
        low = values.get(c.low)
        high = values.get(c.high)
        if low is None or high is None:
            continue
        if not c.ok(low, high):
            violations.append(SmgConstraintViolation(addresses=(c.low, c.high), key=c.key))
    return violations


def constraint_address_pairs_for_testing() -> list[tuple[int, int]]:
    """The `(low, high)` register-address pair behind every constraint.

    Exists only for `test_smg_settings.py`, which asserts none of these pairs
    crosses the pack-dependent (the six DC voltage setpoints) / pack-independent
    boundary. That partition is why `SmgSettingsExecutor.apply` can safely strip
    unconfirmed pack-dependent addresses before calling `validate_smg_settings`:
    a pack-independent write can never combine with a stripped value to hide a
    real hazard, because no constraint here expresses such a combination. If a
    future constraint ever spans both groups, that stripping would make it
    silently unevaluatable while the group is unconfirmed -- exactly when it
    matters most -- and this test is what is meant to catch that before it
    ships.
    """
    return [(c.low, c.high) for c in _CONSTRAINTS]
