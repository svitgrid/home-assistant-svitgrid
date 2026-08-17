"""Tests for assemble_payload — the extracted assembly function that both
the HA-entity path (build_reading_payload) and the direct-harvest engine share."""

from custom_components.svitgrid.readings_publisher import assemble_payload


def test_assembles_renames_and_aggregates():
    payload = assemble_payload(
        inverter_id="inv-1",
        fields={
            "batterySoc": 85.0,
            "pv1Power": 2000.0,
            "pv2Power": 1800.0,
            "gridPower": 500.0,
        },
    )
    assert payload["inverterId"] == "inv-1"
    assert payload["source"] == "edge"
    assert "timestamp" in payload
    assert payload["batterySoc"] == 85.0
    # per-string renamed to API names; aggregate present
    assert payload["pvPower1"] == 2000.0 and payload["pvPower2"] == 1800.0
    assert "pv1Power" not in payload and "pv2Power" not in payload
    assert payload["pvPower"] == 3800.0


def test_no_pv_means_no_pvpower_key():
    payload = assemble_payload(inverter_id="i", fields={"batterySoc": 50.0})
    assert "pvPower" not in payload


def test_assembles_six_pv_strings():
    payload = assemble_payload(
        inverter_id="i",
        fields={
            "pv1Power": 1000.0,
            "pv2Power": 1000.0,
            "pv3Power": 1000.0,
            "pv4Power": 1000.0,
            "pv5Power": 1000.0,
            "pv6Power": 1000.0,
        },
    )
    assert payload["pvPower"] == 6000.0  # all six summed
    assert payload["pvPower5"] == 1000.0 and payload["pvPower6"] == 1000.0
    assert "pv5Power" not in payload and "pv6Power" not in payload


# ---------------------------------------------------------------------------
# Issue #127 — some specs (Huawei SUN2000 x4, swatten_sih_th_10k) read a
# single combined `totalPvPower` register instead of per-MPPT power fields.
# With no pv1Power..pv6Power ever present, the old code left `has_any_pv`
# False forever and `pvPower` unset — gate_payload then defaulted it to 0.0,
# reporting 0 W solar forever even though totalPvPower decoded correctly.
# ---------------------------------------------------------------------------


def test_total_pv_power_only_falls_back_to_pvpower():
    """Regression case for #127: no per-string field, only totalPvPower."""
    payload = assemble_payload(
        inverter_id="i",
        fields={"totalPvPower": 4200.0, "batterySoc": 50.0},
    )
    assert payload["pvPower"] == 4200.0
    assert payload["pvPower"] != 0.0


def test_per_string_and_total_pv_power_both_present_prefers_summed():
    """When both exist, the finer-grained per-string sum wins (matches the
    mobile-harvester and edge paths), not the coarser combined register."""
    payload = assemble_payload(
        inverter_id="i",
        fields={
            "pv1Power": 1000.0,
            "pv2Power": 500.0,
            "totalPvPower": 999.0,  # deliberately different from the sum
        },
    )
    assert payload["pvPower"] == 1500.0


def test_total_pv_power_present_but_none_does_not_become_zero():
    """The decoder distinguishes "field not read" (absent) from "read but no
    data this tick" (present with value None) — see decoder.py's
    _STANDARD_ZERO_FIELDS comment. A None totalPvPower must not be coerced
    to 0.0 by the fallback, and must not be treated as a resolved value."""
    payload = assemble_payload(
        inverter_id="i",
        fields={"totalPvPower": None, "batterySoc": 50.0},
    )
    assert "pvPower" not in payload
