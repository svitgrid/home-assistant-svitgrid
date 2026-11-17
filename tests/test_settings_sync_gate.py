"""Tests for settings-sync gate functions: hash, upload decision, model ranges."""

from custom_components.svitgrid.settings_read_plan import battery_mode_register_for_model
from custom_components.svitgrid.settings_sync import (
    config_range_for_model,
    registers_hash,
    settings_upload_hash,
    should_upload,
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
    assert config_range_for_model("deye_sg04lp3") == (115, 77)
    assert config_range_for_model("deye_sg03lp1") == (217, 63)
    assert config_range_for_model("victron_multiplus_ii_gx_6k5") is None


def test_battery_mode_register_for_model():
    assert battery_mode_register_for_model("deye_sg04lp3") == 111
    assert battery_mode_register_for_model("deye_sg03lp1") == 213
    assert battery_mode_register_for_model("victron_multiplus_ii_gx_6k5") is None


def test_upload_hash_changes_with_the_mode_word_alone():
    regs = [1, 2, 3]
    assert settings_upload_hash(regs, 0) != settings_upload_hash(regs, 1)
    assert settings_upload_hash(regs, 0) != settings_upload_hash(regs, None)


def test_upload_hash_without_mode_is_the_plain_batch_hash():
    regs = [1, 2, 3]
    assert settings_upload_hash(regs, None) == registers_hash(regs)
    assert settings_upload_hash(regs, 7) == registers_hash(regs + [7])
