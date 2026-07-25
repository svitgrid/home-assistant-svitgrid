"""Tests for settings-sync gate functions: hash, upload decision, model ranges."""
import pytest
from custom_components.svitgrid.settings_sync import (
    registers_hash,
    should_upload,
    config_range_for_model,
)


def _reference_fnv(regs):
    """Reference FNV-1a implementation for test vectors."""
    h = 2166136261
    for v in regs:
        h = ((h ^ (v & 0xFF)) * 16777619) & 0xFFFFFFFF
        h = ((h ^ ((v >> 8) & 0xFF)) * 16777619) & 0xFFFFFFFF
    return h


def test_hash_matches_known_vector():
    """Hash [1, 258] (bytes 01 00 02 01) matches FNV reference."""
    # vector cross-checked against the server FNV loop: regs [1, 258]
    # bytes folded: 01 00 02 01
    assert registers_hash([1, 258]) == _reference_fnv([1, 0x0102])


def test_should_upload_bootstrap_change_heartbeat():
    """Test four clauses: bootstrap, changed, fresh+unchanged, heartbeat."""
    assert should_upload(1, 0, 0, 100)            # bootstrap
    assert should_upload(2, 1, 50, 100)           # changed
    assert not should_upload(1, 1, 50, 100)       # fresh + unchanged
    assert should_upload(1, 1, 50, 50 + 1801)     # heartbeat


def test_range_for_models():
    """Model → (start, count) range lookup."""
    assert config_range_for_model('deye_sg04lp3') == (115, 63)
    assert config_range_for_model('deye_sg03lp1') == (217, 63)
    assert config_range_for_model('victron_multiplus_ii_gx_6k5') is None
