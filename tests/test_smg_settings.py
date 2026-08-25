"""Conformance tests for the SMG II settings catalogue.

The fixture (`tests/fixtures/smg-ii-settings.json`, copied verbatim from
`packages/inverter_protocol/smg-ii-settings.json` in the Flutter repo) is the
source of truth. This test asserts the Python catalogue matches it entry for
entry -- every field, both pack voltages, and the constraint pairs -- so the
Python, Dart and (eventually) C copies cannot drift apart silently.

Mirrors `packages/inverter_protocol/test/protocol/eybond_at/smg_settings_test.dart`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.svitgrid.eybond_at.smg_settings import (
    SmgRisk,
    smg_ii_protocol_number,
    smg_settings_for,
    validate_smg_settings,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "smg-ii-settings.json"


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _by_key(settings, key):
    for s in settings:
        if s.key == key:
            return s
    raise KeyError(key)


class TestConformsToFixture:
    """The catalogue must match the shared fixture entry for entry."""

    def test_protocol_number_matches(self, fixture):
        assert smg_ii_protocol_number == fixture["protocolNumber"]

    @pytest.mark.parametrize("pack_voltage_str", ["24", "48"])
    def test_pack_dependent_entries_match(self, fixture, pack_voltage_str):
        pack_voltage = int(pack_voltage_str)
        settings = smg_settings_for(
            protocol_number=smg_ii_protocol_number, nominal_pack_voltage=pack_voltage
        )
        expected_entries = fixture["packVoltages"][pack_voltage_str]
        for expected in expected_entries:
            actual = _by_key(settings, expected["key"])
            assert actual.address == expected["address"], expected["key"]
            assert actual.scale == pytest.approx(expected["scale"]), expected["key"]
            assert actual.unit == expected["unit"], expected["key"]
            assert actual.decimals == expected["decimals"], expected["key"]
            assert actual.raw_min == expected["rawMin"], expected["key"]
            assert actual.raw_max == expected["rawMax"], expected["key"]
            assert actual.risk.value == expected["risk"], expected["key"]
            assert actual.bounds_derived == expected["boundsDerived"], expected["key"]

    def test_pack_independent_entries_match(self, fixture):
        settings = smg_settings_for(
            protocol_number=smg_ii_protocol_number, nominal_pack_voltage=24
        )
        for expected in fixture["packIndependent"]:
            actual = _by_key(settings, expected["key"])
            assert actual.address == expected["address"], expected["key"]
            assert actual.scale == pytest.approx(expected["scale"]), expected["key"]
            assert actual.unit == expected["unit"], expected["key"]
            assert actual.decimals == expected["decimals"], expected["key"]
            assert actual.raw_min == expected["rawMin"], expected["key"]
            assert actual.raw_max == expected["rawMax"], expected["key"]
            assert actual.risk.value == expected["risk"], expected["key"]
            assert actual.bounds_derived == expected["boundsDerived"], expected["key"]

    def test_constraint_pairs_match(self, fixture):
        from custom_components.svitgrid.eybond_at.smg_settings import (
            constraint_address_pairs_for_testing,
        )

        actual_pairs = constraint_address_pairs_for_testing()
        expected_pairs = [(c["low"], c["high"]) for c in fixture["constraints"]]
        assert actual_pairs == expected_pairs

    def test_every_entry_count_matches(self, fixture):
        settings24 = smg_settings_for(
            protocol_number=smg_ii_protocol_number, nominal_pack_voltage=24
        )
        expected_count = len(fixture["packIndependent"]) + len(fixture["packVoltages"]["24"])
        assert len(settings24) == expected_count


class TestCatalogue:
    def test_publishes_the_measured_24v_profile(self):
        settings = smg_settings_for(protocol_number=smg_ii_protocol_number, nominal_pack_voltage=24)
        assert settings
        assert any(s.address == 324 for s in settings)

    def test_every_setting_round_trips_raw_to_display_and_back(self):
        for s in smg_settings_for(protocol_number=smg_ii_protocol_number, nominal_pack_voltage=24):
            display_min = s.to_display(s.raw_min)
            assert s.to_raw(display_min) == s.raw_min, f"{s.key} min round trip"
            display_max = s.to_display(s.raw_max)
            assert s.to_raw(display_max) == s.raw_max, f"{s.key} max round trip"

    def test_every_raw_value_in_range_round_trips_not_just_the_endpoints(self):
        """The endpoints above are truncation-safe by accident, so they prove
        nothing about `to_raw`'s rounding.

        `to_display(324) / 0.1` is 323.99999999999994 -- a setpoint the user
        reads as 32.4 V comes back as 32.3 V if `to_raw` truncates. 18 values
        across the 24 V and 48 V voltage ranges do this, every one of them a
        protective DC setpoint, and every one 0.1 V LOW rather than loudly
        wrong. Walking the whole range is what catches it.
        """
        for pack_voltage in (24, 48):
            settings = smg_settings_for(
                protocol_number=smg_ii_protocol_number, nominal_pack_voltage=pack_voltage
            )
            for s in settings:
                for raw in range(s.raw_min, s.raw_max + 1):
                    assert s.to_raw(s.to_display(raw)) == raw, (
                        f"{pack_voltage} V {s.key}: {raw} -> "
                        f"{s.to_display(raw)}{s.unit} -> {s.to_raw(s.to_display(raw))}"
                    )

    def test_to_raw_rounds_a_value_the_user_typed_rather_than_truncating(self):
        """The user's own numbers, not just round-trips of ours: 32.4 V typed
        into the app must reach register 323 as 324, not 323."""
        settings = smg_settings_for(
            protocol_number=smg_ii_protocol_number, nominal_pack_voltage=24
        )

        def by_address(a):
            return next(s for s in settings if s.address == a)

        assert by_address(323).to_raw(32.4) == 324
        assert by_address(329).to_raw(18.2) == 182

    def test_bench_values_sit_inside_their_published_range(self):
        bench = {
            324: 282, 325: 270, 332: 600, 333: 300,
            323: 320, 327: 230, 329: 210,
            341: 20, 342: 30, 343: 15,
            334: 292, 335: 60, 336: 120, 337: 30,
            320: 2300, 321: 5000, 313: 0, 303: 3,
        }
        for s in smg_settings_for(protocol_number=smg_ii_protocol_number, nominal_pack_voltage=24):
            if s.address not in bench:
                continue
            value = bench[s.address]
            assert s.raw_min <= value <= s.raw_max, (
                f"{s.key} bench value {value} outside {s.raw_min}..{s.raw_max}"
            )

    def test_marks_the_settings_that_can_damage_a_pack(self):
        settings = smg_settings_for(protocol_number=smg_ii_protocol_number, nominal_pack_voltage=24)

        def by_address(a):
            return next(s for s in settings if s.address == a)

        assert by_address(323).risk is SmgRisk.PROTECTIVE
        assert by_address(327).risk is SmgRisk.PROTECTIVE
        assert by_address(329).risk is SmgRisk.PROTECTIVE
        assert by_address(303).risk is SmgRisk.ROUTINE


class TestCatalogueGatedOnProtocolNumber:
    def test_unrecognised_protocol_publishes_nothing(self):
        assert smg_settings_for(protocol_number=3, nominal_pack_voltage=24) == []
        assert smg_settings_for(protocol_number=0, nominal_pack_voltage=48) == []

    def test_protocol_11_at_24v_publishes_the_full_measured_catalogue(self):
        settings = smg_settings_for(protocol_number=smg_ii_protocol_number, nominal_pack_voltage=24)
        assert len(settings) == 18
        assert all(not s.bounds_derived for s in settings), "24 V bounds were measured on the bench unit"

    def test_an_unmeasured_pack_still_gets_every_pack_independent_setting(self):
        settings = smg_settings_for(protocol_number=smg_ii_protocol_number, nominal_pack_voltage=12)
        keys = {s.key for s in settings}
        assert keys == {
            "maxChargeCurrent", "maxMainsChargeCurrent",
            "socBackToUtility", "socBackToBattery", "socLowDcCutoff",
            "equalizationEnabled", "equalizationMinutes",
            "equalizationTimeoutMinutes", "equalizationIntervalDays",
            "outputVoltage", "outputFrequency", "buzzerMode",
        }
        assert len(settings) == 12
        assert not any(s.address == 324 for s in settings), "maxChargeVoltage is pack-dependent"


class TestFortyEightVoltBoundsAreDerived:
    def test_every_dc_setpoint_doubles_its_bounds_and_is_marked_derived(self):
        at24 = {
            s.address: s
            for s in smg_settings_for(protocol_number=smg_ii_protocol_number, nominal_pack_voltage=24)
        }
        at48 = {
            s.address: s
            for s in smg_settings_for(protocol_number=smg_ii_protocol_number, nominal_pack_voltage=48)
        }
        for address in (324, 325, 323, 327, 329, 334):
            a = at24[address]
            b = at48[address]
            assert b.raw_min == a.raw_min * 2, f"raw_min for {address}"
            assert b.raw_max == a.raw_max * 2, f"raw_max for {address}"
            assert b.scale == a.scale, "scale is measured, not derived"
            assert b.key == a.key
            assert b.bounds_derived is True, f"{address} at 48 V is derived"
            assert a.bounds_derived is False, f"{address} at 24 V is measured"

    def test_48v_publishes_all_eighteen_settings(self):
        assert len(
            smg_settings_for(protocol_number=smg_ii_protocol_number, nominal_pack_voltage=48)
        ) == 18

    def test_cross_field_constraints_still_hold_at_48v_bounds(self):
        violations = validate_smg_settings({
            325: 580,  # float 58.0 V
            324: 564,  # bulk  56.4 V -> float above bulk
        })
        assert any(v.key == "floatBelowBulk" for v in violations)


class TestValidateSmgSettings:
    @staticmethod
    def valid():
        return {
            324: 282, 325: 270, 323: 320,
            327: 230, 329: 210,
            341: 20, 342: 30, 343: 15,
            334: 292,
        }

    def test_accepts_the_profile_the_bench_unit_actually_holds(self):
        assert validate_smg_settings(self.valid()) == []

    def test_rejects_a_float_above_the_bulk_charge_voltage(self):
        v = self.valid()
        v[325] = 290
        errors = validate_smg_settings(v)
        assert errors
        assert set(errors[0].addresses) >= {324, 325}

    def test_rejects_a_float_exactly_equal_to_the_bulk_charge_voltage(self):
        """`floatBelowBulk` says STRICTLY below. Equal is the boundary the
        constraint's own comment describes -- a pack held at the bulk voltage
        never leaves absorption, which is the damage, not a near miss."""
        v = self.valid()
        v[325] = v[324]
        errors = validate_smg_settings(v)
        assert errors, "float equal to bulk must be refused, not tolerated"
        assert errors[0].key == "floatBelowBulk"

    def test_rejects_a_bulk_above_the_overvoltage_protection(self):
        v = self.valid()
        v[324] = 325
        assert validate_smg_settings(v)

    def test_rejects_an_off_grid_cutoff_above_the_on_mains_cutoff(self):
        v = self.valid()
        v[329] = 240
        assert validate_smg_settings(v)

    def test_rejects_a_low_dc_cutoff_above_the_back_to_utility_soc(self):
        v = self.valid()
        v[343] = 25
        assert validate_smg_settings(v)

    def test_rejects_a_back_to_battery_soc_below_back_to_utility(self):
        v = self.valid()
        v[342] = 15
        assert validate_smg_settings(v)

    def test_rejects_an_equalization_voltage_below_the_bulk_voltage(self):
        v = self.valid()
        v[334] = 275
        assert validate_smg_settings(v)

    def test_reports_every_violated_constraint_not_just_the_first(self):
        v = self.valid()
        v[325] = 290
        v[329] = 240
        assert len(validate_smg_settings(v)) >= 2

    def test_ignores_constraints_whose_fields_are_absent(self):
        assert validate_smg_settings({324: 282}) == []


class TestCrossFieldConstraintsNeverSpanTheExecutorStrippingBoundary:
    def test_no_constraint_pairs_a_pack_dependent_address_with_a_pack_independent_one(self):
        from custom_components.svitgrid.eybond_at.smg_settings import (
            constraint_address_pairs_for_testing,
        )

        settings = smg_settings_for(protocol_number=smg_ii_protocol_number, nominal_pack_voltage=48)
        pack_dependent = {s.address for s in settings if s.bounds_derived}
        pack_independent = {s.address for s in settings if not s.bounds_derived}

        for low, high in constraint_address_pairs_for_testing():
            crosses = (low in pack_dependent and high in pack_independent) or (
                low in pack_independent and high in pack_dependent
            )
            assert not crosses, (
                f"constraint on registers {low}<->{high} spans the pack-dependent/"
                "pack-independent boundary -- see smg_settings.py docstring"
            )
