"""Tests for settings-sync gate functions: hash, upload decision, model ranges."""
import pytest
from custom_components.svitgrid.settings_sync import (
    registers_hash,
    should_upload,
    config_range_for_model,
)


def test_hash_matches_known_vector():
    """Hash [1, 258] matches correct FNV-1a reference value."""
    # Hardcoded value computed independently via pure integer arithmetic:
    # python3 -c "h=2166136261; v=1; h=((h^(v&0xFF))*16777619)&0xFFFFFFFF;
    # h=((h^((v>>8)&0xFF))*16777619)&0xFFFFFFFF; v=258;
    # h=((h^(v&0xFF))*16777619)&0xFFFFFFFF; h=((h^((v>>8)&0xFF))*16777619)&0xFFFFFFFF;
    # print(h)"
    assert registers_hash([1, 258]) == 1215212649


def test_hash_empty_list():
    """Empty register list returns FNV offset basis."""
    assert registers_hash([]) == 2166136261


def test_should_upload_four_clauses():
    """Test four clauses: bootstrap, changed, clock-skew, heartbeat."""
    # Bootstrap: never uploaded (last_uploaded == 0)
    assert should_upload(1, 1, 0, 100)

    # Changed: hash differs
    assert should_upload(2, 1, 50, 100)

    # Clock skew (fail-safe): now < last_uploaded
    assert should_upload(1, 1, 1000, 500)

    # Heartbeat: now >= last_uploaded + heartbeat_s
    assert should_upload(1, 1, 50, 50 + 1801)

    # None apply: no upload
    assert not should_upload(1, 1, 50, 100)


def test_should_upload_cached_hash_zero_alone_insufficient():
    """cached_hash==0 alone does NOT force upload if last_uploaded > 0 and unchanged."""
    # Same hash (0), last uploaded > 0, now fresh (no heartbeat elapsed)
    # Should NOT upload because last_uploaded > 0 (not bootstrap)
    # and no other clause applies
    assert not should_upload(0, 0, 50, 100)


def test_range_for_models():
    """Model → (start, count) range lookup."""
    assert config_range_for_model('deye_sg04lp3') == (115, 63)
    assert config_range_for_model('deye_sg03lp1') == (217, 63)
    assert config_range_for_model('victron_multiplus_ii_gx_6k5') is None
