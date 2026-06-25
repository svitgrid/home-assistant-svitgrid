"""Guards for the canonical-field constants."""
from __future__ import annotations

from custom_components.svitgrid.const import ALL_FIELDS, MAPPABLE_FIELDS


def test_mappable_fields_cover_all_fields_exactly():
    """MAPPABLE_FIELDS is the single source of truth — it must cover every
    canonical field, with no extras and no duplicates."""
    keys = [field for field, _label in MAPPABLE_FIELDS]
    assert set(keys) == ALL_FIELDS
    assert len(keys) == len(set(keys)), "duplicate field in MAPPABLE_FIELDS"


def test_mappable_fields_have_nonempty_labels():
    """Every mappable field has a human-readable, non-empty label."""
    for field, label in MAPPABLE_FIELDS:
        assert isinstance(label, str) and label.strip(), f"empty label for {field}"
    labels = [label for _field, label in MAPPABLE_FIELDS]
    assert len(labels) == len(set(labels)), "duplicate label in MAPPABLE_FIELDS"


def test_core_payload_fields_are_the_five_non_pv_required():
    from custom_components.svitgrid.const import CORE_PAYLOAD_FIELDS

    assert CORE_PAYLOAD_FIELDS == frozenset(
        {"batterySoc", "batteryPower", "batteryVoltage", "gridPower", "loadPower"}
    )
    # pvPower is NOT in the set — the gate defaults it to 0 for no-solar systems.
    assert "pvPower" not in CORE_PAYLOAD_FIELDS
    assert "pv1Power" not in CORE_PAYLOAD_FIELDS


def test_per_phase_power_fields_are_mappable():
    """Per-phase load and grid power (L1..L3) must be mappable so 3-phase
    households get per-phase data in the app. The API folds these scalars
    into its canonical phaseLoads / phaseGridPowers arrays at ingest (the
    same path as the existing gridVoltageL1..L3 → phaseVoltages fold)."""
    keys = {field for field, _label in MAPPABLE_FIELDS}
    for field in (
        "loadPowerL1",
        "loadPowerL2",
        "loadPowerL3",
        "gridPowerL1",
        "gridPowerL2",
        "gridPowerL3",
    ):
        assert field in keys, f"{field} missing from MAPPABLE_FIELDS"
        assert field in ALL_FIELDS, f"{field} missing from ALL_FIELDS"


def test_set_cloud_endpoint_is_an_internal_command():
    """set_cloud_endpoint is bypass-signed: the URL allow-list is the
    trust boundary, not the admin keystore. Must be in INTERNAL_COMMANDS
    so command_poller's Arm-2 signature-verify path skips it."""
    from custom_components.svitgrid.const import (
        INTERNAL_COMMANDS,
        SET_CLOUD_ENDPOINT_COMMAND,
    )
    assert SET_CLOUD_ENDPOINT_COMMAND == "set_cloud_endpoint"
    assert SET_CLOUD_ENDPOINT_COMMAND in INTERNAL_COMMANDS


def test_default_api_base_is_in_allow_list():
    """The default api_base must be one of the allow-listed URLs — else
    a fresh install hits an endpoint the broker auth doesn't trust."""
    from custom_components.svitgrid.cloud_endpoint_handler import (
        is_allowed_api_base,
    )
    from custom_components.svitgrid.const import DEFAULT_API_BASE
    assert is_allowed_api_base(DEFAULT_API_BASE)
