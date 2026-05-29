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
    for field, label in MAPPABLE_FIELDS:
        assert isinstance(label, str) and label.strip(), f"empty label for {field}"
