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
