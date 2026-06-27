"""Tests for compute_register_writes (SP-C Task 6).

Tests written RED-first (TDD), covering every encoding rule from the brief:
  full_word + limits, on/off, bit:0 set/clear, bit:13, clearMask:0x03,
  full_word + clear_mask, slot base+idx, via_next_slot (mid + wrap),
  slot-index-out-of-range.
"""
from __future__ import annotations

import pytest

from custom_components.svitgrid.harvest.register_spec import FieldWrite, SlotSpec, WriteCommand
from custom_components.svitgrid.harvest.write_compute import compute_register_writes

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fw(**kw) -> FieldWrite:
    """Build a FieldWrite with convenient kwargs (camelCase not needed here)."""
    return FieldWrite(**kw)


def _cmd(fields=(), slot=None) -> WriteCommand:
    return WriteCommand(command="test_cmd", fields=tuple(fields), slot=slot)


# ---------------------------------------------------------------------------
# full_word encoding
# ---------------------------------------------------------------------------

class TestFullWordEncoding:
    """full_word: scale, limits, on/off, clear_mask."""

    def test_scale_applied(self):
        fw = _fw(payload_field="sellPower", address=340, value_scale=10.0)
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"sellPower": 5000}, {})
        assert result == [(1, 340, 500)]

    def test_scale_one_passthrough(self):
        fw = _fw(payload_field="workMode", address=142)
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"workMode": 3}, {})
        assert result == [(1, 142, 3)]

    def test_limits_clamp_max(self):
        fw = _fw(payload_field="sellPower", address=340, limits={"min": 0, "max": 15000})
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"sellPower": 20000}, {})
        assert result == [(1, 340, 15000)]

    def test_limits_clamp_min(self):
        fw = _fw(payload_field="sellPower", address=340, limits={"min": 0, "max": 15000})
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"sellPower": -100}, {})
        assert result == [(1, 340, 0)]

    def test_limits_within_range_unchanged(self):
        fw = _fw(payload_field="sellPower", address=340, limits={"min": 0, "max": 15000})
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"sellPower": 7500}, {})
        assert result == [(1, 340, 7500)]

    def test_on_value_true(self):
        fw = _fw(payload_field="enabled", address=100, on_value=2, off_value=0)
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"enabled": True}, {})
        assert result == [(1, 100, 2)]

    def test_on_value_false(self):
        fw = _fw(payload_field="enabled", address=100, on_value=2, off_value=0)
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"enabled": False}, {})
        assert result == [(1, 100, 0)]

    def test_on_value_off_value_none_defaults_zero(self):
        """off_value=None should default to 0 for False payload."""
        fw = _fw(payload_field="enabled", address=100, on_value=1)
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"enabled": False}, {})
        assert result == [(1, 100, 0)]

    def test_full_word_with_clear_mask(self):
        """full_word + clear_mask: (prior & ~clear_mask) | value."""
        fw = _fw(payload_field="val", address=200, clear_mask=0x03)
        cmd = _cmd([fw])
        # prior=0xF (0b1111), clear_mask=0x03 → base = 0xF & ~0x03 = 0xC (12)
        # v=5, result = 12 | 5 = 13 (0b1101)
        prior = {200: 0b1111}  # 0xF
        result = compute_register_writes(cmd, {"val": 5}, prior)
        assert result == [(1, 200, 13)]

    def test_multiple_fields_deterministic_order(self):
        fw1 = _fw(payload_field="a", address=10)
        fw2 = _fw(payload_field="b", address=20)
        cmd = _cmd([fw1, fw2])
        result = compute_register_writes(cmd, {"a": 1, "b": 2}, {})
        assert result == [(1, 10, 1), (1, 20, 2)]

    def test_unit_id_forwarded(self):
        fw = _fw(payload_field="x", address=50)
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"x": 7}, {}, unit_id=100)
        assert result == [(100, 50, 7)]

    def test_rounding_applied(self):
        fw = _fw(payload_field="p", address=10, value_scale=3.0)
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"p": 10}, {})
        # round(10 / 3.0) = round(3.333) = 3
        assert result == [(1, 10, 3)]


# ---------------------------------------------------------------------------
# bit:N encoding
# ---------------------------------------------------------------------------

class TestBitEncoding:
    """bit:N: RMW on prior register, clear_mask, truthy detection."""

    def test_bit0_set(self):
        fw = _fw(payload_field="flag", address=326, encoding="bit:0")
        cmd = _cmd([fw])
        prior = {326: 0b1010}
        result = compute_register_writes(cmd, {"flag": True}, prior)
        assert result == [(1, 326, 0b1011)]

    def test_bit0_clear(self):
        fw = _fw(payload_field="flag", address=326, encoding="bit:0")
        cmd = _cmd([fw])
        prior = {326: 0b1011}
        result = compute_register_writes(cmd, {"flag": False}, prior)
        assert result == [(1, 326, 0b1010)]

    def test_bit0_preserves_other_bits(self):
        fw = _fw(payload_field="flag", address=326, encoding="bit:0")
        cmd = _cmd([fw])
        prior = {326: 0b11111110}
        result = compute_register_writes(cmd, {"flag": True}, prior)
        assert result == [(1, 326, 0b11111111)]

    def test_bit13_set(self):
        """gen_force: bit:13, prior=0, set → 0x2000."""
        fw = _fw(payload_field="genForce", address=326, encoding="bit:13",
                 on_value=1, off_value=0, clear_mask=0x1FFF)
        cmd = _cmd([fw])
        prior = {326: 0x0000}
        result = compute_register_writes(cmd, {"genForce": 1}, prior)
        # base = 0x0000 & ~0x1FFF = 0x0000; set bit 13 → 0x2000
        assert result == [(1, 326, 0x2000)]

    def test_bit13_clear(self):
        fw = _fw(payload_field="genForce", address=326, encoding="bit:13",
                 on_value=1, off_value=0, clear_mask=0x1FFF)
        cmd = _cmd([fw])
        prior = {326: 0xFFFF}
        result = compute_register_writes(cmd, {"genForce": 0}, prior)
        # base = 0xFFFF & ~0x1FFF = 0xE000; clear bit 13 → 0xE000 & ~0x2000 = 0xC000
        assert result == [(1, 326, 0xC000)]

    def test_clear_mask_0x03_bit1_set(self):
        """clearMask:0x03 on bit:1 with prior=0b111."""
        fw = _fw(payload_field="flag", address=50, encoding="bit:1", clear_mask=0x03)
        cmd = _cmd([fw])
        prior = {50: 0b0111}  # 7
        result = compute_register_writes(cmd, {"flag": True}, prior)
        # base = 0b0111 & ~0x03 = 0b0100 (4); set bit 1 → 4 | 2 = 6
        assert result == [(1, 50, 0b0110)]

    def test_clear_mask_0x03_bit1_clear(self):
        fw = _fw(payload_field="flag", address=50, encoding="bit:1", clear_mask=0x03)
        cmd = _cmd([fw])
        prior = {50: 0b0111}
        result = compute_register_writes(cmd, {"flag": False}, prior)
        # base = 0b0111 & ~0x03 = 0b0100 (4); clear bit 1 → 4 & ~2 = 4
        assert result == [(1, 50, 0b0100)]

    def test_prior_missing_defaults_zero(self):
        """If prior doesn't have the address, default to 0."""
        fw = _fw(payload_field="flag", address=99, encoding="bit:5")
        cmd = _cmd([fw])
        result = compute_register_writes(cmd, {"flag": True}, {})
        assert result == [(1, 99, 1 << 5)]

    def test_int_payload_on_value_match(self):
        """int payload == on_value → set bit."""
        fw = _fw(payload_field="genForce", address=326, encoding="bit:13",
                 on_value=1, off_value=0)
        cmd = _cmd([fw])
        prior = {326: 0x0000}
        result = compute_register_writes(cmd, {"genForce": 1}, prior)
        assert result == [(1, 326, 0x2000)]

    def test_int_payload_on_value_no_match(self):
        """int payload != on_value → clear bit."""
        fw = _fw(payload_field="genForce", address=326, encoding="bit:13",
                 on_value=1, off_value=0)
        cmd = _cmd([fw])
        prior = {326: 0x2000}
        result = compute_register_writes(cmd, {"genForce": 0}, prior)
        # clear bit 13: 0x2000 & ~0x2000 = 0x0000
        assert result == [(1, 326, 0x0000)]


# ---------------------------------------------------------------------------
# Slot encoding
# ---------------------------------------------------------------------------

class TestSlotEncoding:
    """Slot: index_field, stride, via_next_slot, wrap at last slot, OOB raises."""

    def _slot_cmd(self, via_next=False, count=6, stride=2, base=103) -> WriteCommand:
        field = _fw(payload_field="socMin", base=base, via_next_slot=via_next)
        slot = SlotSpec(
            index_field="slotIndex",
            count=count,
            stride=stride,
            end_via_next_slot=via_next,
            fields=(field,),
        )
        return _cmd(slot=slot)

    def test_slot_base_plus_idx_zero(self):
        cmd = self._slot_cmd()
        result = compute_register_writes(cmd, {"slotIndex": 0, "socMin": 10}, {})
        # addr = 103 + 0*2 = 103
        assert result == [(1, 103, 10)]

    def test_slot_base_plus_idx_two(self):
        cmd = self._slot_cmd()
        result = compute_register_writes(cmd, {"slotIndex": 2, "socMin": 20}, {})
        # addr = 103 + 2*2 = 107
        assert result == [(1, 107, 20)]

    def test_slot_last_index(self):
        cmd = self._slot_cmd()
        result = compute_register_writes(cmd, {"slotIndex": 5, "socMin": 30}, {})
        # addr = 103 + 5*2 = 113
        assert result == [(1, 113, 30)]

    def test_via_next_slot_mid(self):
        """via_next_slot at index 2 (count=6): (2+1)%6 = 3."""
        cmd = self._slot_cmd(via_next=True)
        result = compute_register_writes(cmd, {"slotIndex": 2, "socMin": 40}, {})
        # addr = 103 + ((2+1)%6)*2 = 103 + 3*2 = 109
        assert result == [(1, 109, 40)]

    def test_via_next_slot_wraps_at_last(self):
        """via_next_slot at index 5 (count=6): (5+1)%6 = 0 → base."""
        cmd = self._slot_cmd(via_next=True)
        result = compute_register_writes(cmd, {"slotIndex": 5, "socMin": 50}, {})
        # addr = 103 + ((5+1)%6)*2 = 103 + 0*2 = 103
        assert result == [(1, 103, 50)]

    def test_slot_index_out_of_range_high(self):
        cmd = self._slot_cmd()
        with pytest.raises(ValueError, match="slot index"):
            compute_register_writes(cmd, {"slotIndex": 6, "socMin": 0}, {})

    def test_slot_index_out_of_range_negative(self):
        cmd = self._slot_cmd()
        with pytest.raises(ValueError, match="slot index"):
            compute_register_writes(cmd, {"slotIndex": -1, "socMin": 0}, {})

    def test_slot_multiple_fields(self):
        """Slot with two fields — both emitted in declaration order."""
        f1 = _fw(payload_field="startTime", base=148)
        f2 = _fw(payload_field="socMin", base=103)
        slot = SlotSpec(
            index_field="slotIndex",
            count=6,
            stride=2,
            end_via_next_slot=False,
            fields=(f1, f2),
        )
        cmd = _cmd(slot=slot)
        result = compute_register_writes(cmd, {"slotIndex": 1, "startTime": 360, "socMin": 20}, {})
        # f1: addr = 148 + 1*2 = 150; f2: addr = 103 + 1*2 = 105
        assert result == [(1, 150, 360), (1, 105, 20)]

    def test_slot_value_scale(self):
        """slot field with value_scale."""
        field = _fw(payload_field="power", base=200, value_scale=10.0)
        slot = SlotSpec(
            index_field="slotIndex",
            count=4,
            stride=1,
            end_via_next_slot=False,
            fields=(field,),
        )
        cmd = _cmd(slot=slot)
        result = compute_register_writes(cmd, {"slotIndex": 3, "power": 5000}, {})
        # addr = 200 + 3*1 = 203; value = round(5000/10.0) = 500
        assert result == [(1, 203, 500)]

    def test_slot_ignores_top_level_fields(self):
        """When slot is set, only slot fields are emitted — not cmd.fields."""
        top_fw = _fw(payload_field="ignored", address=999)
        field = _fw(payload_field="socMin", base=103)
        slot = SlotSpec(
            index_field="slotIndex",
            count=6,
            stride=2,
            end_via_next_slot=False,
            fields=(field,),
        )
        cmd = WriteCommand(command="test_cmd", fields=(top_fw,), slot=slot)
        result = compute_register_writes(cmd, {"slotIndex": 0, "socMin": 5, "ignored": 42}, {})
        assert len(result) == 1
        assert result[0][1] == 103  # top-level address 999 NOT emitted


# ---------------------------------------------------------------------------
# Always-emit rule
# ---------------------------------------------------------------------------

class TestAlwaysEmit:
    """Every field is always emitted — no idempotent skip."""

    def test_always_emits_even_if_prior_matches(self):
        """Even if the register already holds the target value, we still emit."""
        fw = _fw(payload_field="workMode", address=142)
        cmd = _cmd([fw])
        prior = {142: 3}
        result = compute_register_writes(cmd, {"workMode": 3}, prior)
        assert result == [(1, 142, 3)]
