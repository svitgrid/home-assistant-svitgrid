"""Tests for assemble_payload — the extracted assembly function that both
the HA-entity path (build_reading_payload) and the direct-harvest engine share."""

from custom_components.svitgrid.readings_publisher import assemble_payload


def test_assembles_renames_and_aggregates():
    payload = assemble_payload(inverter_id="inv-1", fields={
        "batterySoc": 85.0, "pv1Power": 2000.0, "pv2Power": 1800.0, "gridPower": 500.0,
    })
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
