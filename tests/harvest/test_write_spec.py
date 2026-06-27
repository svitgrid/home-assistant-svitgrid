"""Tests for write-spec parsing (SP-C Task 5).

Tests run RED before implementation, GREEN after.
"""
import pytest

from custom_components.svitgrid.harvest.register_spec import (
    FieldWrite,
    RegisterSpec,
    SlotSpec,
    WriteCommand,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_WRITE_JSON = {
    "command": "set_work_mode",
    "fields": [
        {
            "payloadField": "workMode",
            "address": 142,
            "encoding": "full_word",
            "valueScale": 1.0,
        }
    ],
}

BIT_WRITE_JSON = {
    "command": "set_gen_force",
    "fields": [
        {
            "payloadField": "genForce",
            "address": 326,
            "encoding": "bit:13",
            "onValue": 1,
            "offValue": 0,
            "clearMask": 8191,
        }
    ],
}

SLOT_WRITE_JSON = {
    "command": "set_tou_slot",
    "fields": [
        {
            "payloadField": "startTime",
            "base": 148,
            "viaNextSlot": True,
        }
    ],
    "slot": {
        "indexField": "slotIndex",
        "count": 6,
        "stride": 2,
        "endViaNextSlotStart": True,
        "fields": [
            {"payloadField": "socMin", "base": 103},
        ],
    },
}

BASE_SPEC: dict = {
    "modelId": "deye_sg04lp3",
    "version": 1,
    "source": "generated",
    "verified": True,
    "protocol": "solarman_v5",
    "port": 8899,
    "defaultSlaveId": 1,
    "flags": {},
    "reads": [],
    "derivations": [],
    "writes": [],
}


# ---------------------------------------------------------------------------
# FieldWrite.from_dict
# ---------------------------------------------------------------------------

class TestFieldWriteFromDict:
    def test_simple_full_word(self):
        fw = FieldWrite.from_dict(SIMPLE_WRITE_JSON["fields"][0])
        assert fw.payload_field == "workMode"
        assert fw.address == 142
        assert fw.encoding == "full_word"
        assert fw.value_scale == 1.0
        assert fw.on_value is None
        assert fw.off_value is None
        assert fw.clear_mask is None
        assert fw.via_next_slot is False
        assert fw.limits is None

    def test_bit_encoding_with_mask(self):
        fw = FieldWrite.from_dict(BIT_WRITE_JSON["fields"][0])
        assert fw.payload_field == "genForce"
        assert fw.address == 326
        assert fw.encoding == "bit:13"
        assert fw.on_value == 1
        assert fw.off_value == 0
        assert fw.clear_mask == 8191

    def test_via_next_slot_and_base(self):
        fw = FieldWrite.from_dict(SLOT_WRITE_JSON["fields"][0])
        assert fw.payload_field == "startTime"
        assert fw.base == 148
        assert fw.address is None
        assert fw.via_next_slot is True

    def test_defaults(self):
        fw = FieldWrite.from_dict({"payloadField": "x"})
        assert fw.encoding == "full_word"
        assert fw.value_scale == 1.0
        assert fw.via_next_slot is False

    def test_frozen(self):
        fw = FieldWrite.from_dict({"payloadField": "x"})
        with pytest.raises((AttributeError, TypeError)):
            fw.payload_field = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SlotSpec.from_dict
# ---------------------------------------------------------------------------

class TestSlotSpecFromDict:
    def test_parses_slot(self):
        ss = SlotSpec.from_dict(SLOT_WRITE_JSON["slot"])
        assert ss.index_field == "slotIndex"
        assert ss.count == 6
        assert ss.stride == 2
        assert ss.end_via_next_slot is True
        assert len(ss.fields) == 1
        assert ss.fields[0].payload_field == "socMin"

    def test_fields_is_tuple(self):
        ss = SlotSpec.from_dict(SLOT_WRITE_JSON["slot"])
        assert isinstance(ss.fields, tuple)

    def test_frozen(self):
        ss = SlotSpec.from_dict(SLOT_WRITE_JSON["slot"])
        with pytest.raises((AttributeError, TypeError)):
            ss.count = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# WriteCommand.from_dict
# ---------------------------------------------------------------------------

class TestWriteCommandFromDict:
    def test_simple_command(self):
        wc = WriteCommand.from_dict(SIMPLE_WRITE_JSON)
        assert wc.command == "set_work_mode"
        assert len(wc.fields) == 1
        assert wc.slot is None

    def test_bit_command(self):
        wc = WriteCommand.from_dict(BIT_WRITE_JSON)
        assert wc.command == "set_gen_force"
        assert wc.fields[0].encoding == "bit:13"

    def test_slot_command(self):
        wc = WriteCommand.from_dict(SLOT_WRITE_JSON)
        assert wc.command == "set_tou_slot"
        assert wc.slot is not None
        assert isinstance(wc.slot, SlotSpec)
        assert wc.slot.count == 6

    def test_fields_and_slot_fields_are_tuples(self):
        wc = WriteCommand.from_dict(SLOT_WRITE_JSON)
        assert isinstance(wc.fields, tuple)
        assert isinstance(wc.slot.fields, tuple)

    def test_no_slot_when_absent(self):
        wc = WriteCommand.from_dict(SIMPLE_WRITE_JSON)
        assert wc.slot is None


# ---------------------------------------------------------------------------
# RegisterSpec.writes
# ---------------------------------------------------------------------------

class TestRegisterSpecWrites:
    def test_writes_empty_by_default(self):
        spec = RegisterSpec.from_dict(BASE_SPEC)
        assert spec.writes == ()

    def test_writes_parsed_as_tuple_of_write_commands(self):
        d = {**BASE_SPEC, "writes": [SIMPLE_WRITE_JSON, BIT_WRITE_JSON]}
        spec = RegisterSpec.from_dict(d)
        assert isinstance(spec.writes, tuple)
        assert len(spec.writes) == 2
        assert all(isinstance(w, WriteCommand) for w in spec.writes)

    def test_slot_command_roundtrip(self):
        d = {**BASE_SPEC, "writes": [SLOT_WRITE_JSON]}
        spec = RegisterSpec.from_dict(d)
        wc = spec.writes[0]
        assert wc.slot.index_field == "slotIndex"
        assert wc.slot.fields[0].payload_field == "socMin"

    def test_writes_is_immutable_tuple(self):
        d = {**BASE_SPEC, "writes": [SIMPLE_WRITE_JSON]}
        spec = RegisterSpec.from_dict(d)
        with pytest.raises((AttributeError, TypeError)):
            spec.writes = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# validate() — write checks
# ---------------------------------------------------------------------------

class TestValidateWrites:
    def _spec(self, writes):
        return RegisterSpec.from_dict({**BASE_SPEC, "writes": writes})

    def test_valid_full_word_passes(self):
        spec = self._spec([SIMPLE_WRITE_JSON])
        assert spec.validate() == []

    def test_valid_bit_encoding_passes(self):
        spec = self._spec([BIT_WRITE_JSON])
        assert spec.validate() == []

    def test_valid_slot_command_passes(self):
        spec = self._spec([SLOT_WRITE_JSON])
        assert spec.validate() == []

    def test_bad_encoding_rejected(self):
        bad = {
            "command": "do_thing",
            "fields": [{"payloadField": "x", "address": 1, "encoding": "nibble"}],
        }
        problems = self._spec([bad]).validate()
        assert any("encoding" in p for p in problems)

    def test_bit_encoding_valid_pattern(self):
        good = {
            "command": "do_thing",
            "fields": [{"payloadField": "x", "address": 1, "encoding": "bit:0"}],
        }
        assert self._spec([good]).validate() == []

    def test_slot_missing_index_field_rejected(self):
        bad_slot = {
            "command": "set_tou_slot",
            "fields": [{"payloadField": "startTime", "base": 148}],
            "slot": {
                "indexField": "",   # empty = missing
                "count": 6,
                "stride": 2,
                "endViaNextSlotStart": False,
                "fields": [{"payloadField": "socMin", "base": 103}],
            },
        }
        problems = self._spec([bad_slot]).validate()
        assert any("index_field" in p for p in problems)

    def test_slot_count_zero_rejected(self):
        bad_slot = {
            "command": "set_tou_slot",
            "fields": [{"payloadField": "startTime", "base": 148}],
            "slot": {
                "indexField": "slotIndex",
                "count": 0,
                "stride": 2,
                "endViaNextSlotStart": False,
                "fields": [{"payloadField": "socMin", "base": 103}],
            },
        }
        problems = self._spec([bad_slot]).validate()
        assert any("count" in p for p in problems)

    def test_slot_empty_fields_rejected(self):
        bad_slot = {
            "command": "set_tou_slot",
            "fields": [{"payloadField": "startTime", "base": 148}],
            "slot": {
                "indexField": "slotIndex",
                "count": 6,
                "stride": 2,
                "endViaNextSlotStart": False,
                "fields": [],
            },
        }
        problems = self._spec([bad_slot]).validate()
        assert any("fields" in p for p in problems)
