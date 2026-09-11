"""`mask` on a read, and the fail-closed rule for read knobs this add-on lacks.

svitgrid#568. LuxPower/EG4 input register 5 packs SOC in the LOW byte and SOH
in the high byte, so a 70 % pack on a healthy BMS reads 0x6446 = 25670. The
published spec says `"mask": 255`; before this, `ReadDef.from_dict` dropped
the key, `decode` returned 25670, and `_apply_reader_clamps` clamped it to a
permanent, plausible 100 %.

The second half is the class, not the instance: a read-level knob this add-on
does not implement used to be discarded silently, so the spec decoded a WRONG
number instead of failing. Unknown builtins already refuse the spec whole;
unknown read knobs now do the same — dark and loud beats wrong and quiet.
"""

from __future__ import annotations

import pytest

from custom_components.svitgrid.harvest.decoder import decode, sanitize
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.spec_health import build_spec


def _doc(reads: list[dict]) -> dict:
    return {
        "modelId": "eg4_18kpv",
        "version": 1,
        "protocol": "solarman_v5",
        "port": 8899,
        "defaultSlaveId": 1,
        "flags": {"usesInputRegisters": True},
        "reads": reads,
        "derivations": [],
        "writes": [],
    }


_SOC = {"field": "batterySoc", "address": 5, "functionCode": "FC04", "mask": 255}


def test_mask_extracts_the_low_byte_soc_not_the_packed_word() -> None:
    spec = RegisterSpec.from_dict(_doc([_SOC]))
    out = decode(spec, {1: {5: 0x6446}})  # SOH 100 (0x64) | SOC 70 (0x46)
    assert out["batterySoc"] == 70.0


def test_masked_soc_survives_the_reader_clamp_as_70_not_100() -> None:
    spec = RegisterSpec.from_dict(_doc([_SOC]))
    assert sanitize(decode(spec, {1: {5: 25670}}), spec)["batterySoc"] == 70.0


def test_dead_register_sentinel_is_checked_on_the_unmasked_word() -> None:
    # Dart (registers/base.dart) compares the sentinel BEFORE masking, so a
    # dead 0xFFFF register reads 0 — not 0xFFFF & 0xFF = 255.
    spec = RegisterSpec.from_dict(_doc([_SOC]))
    assert decode(spec, {1: {5: 0xFFFF}})["batterySoc"] == 0.0


def test_mask_applies_before_signedness() -> None:
    # The masked value's sign, as in Dart: 0x80FF & 0x00FF = 255, not negative.
    spec = RegisterSpec.from_dict(
        _doc([{"field": "x", "address": 9, "signed": True, "mask": 0x00FF}])
    )
    assert decode(spec, {1: {9: 0x80FF}})["x"] == 255.0


def test_mask_on_a_32_bit_read_is_refused() -> None:
    # Mirrors Dart RegisterSpec.validate: the 32-bit path bypasses convert, so
    # a mask there would be ignored without a word.
    spec = RegisterSpec.from_dict(_doc([{"field": "e", "address": 100, "words": 2, "mask": 255}]))
    assert any("mask" in p for p in spec.validate())


@pytest.mark.parametrize("knob", ["shift", "bitOffset", "command"])
def test_unknown_read_knob_makes_the_spec_not_executable(knob: str) -> None:
    spec = RegisterSpec.from_dict(_doc([{"field": "batterySoc", "address": 5, knob: 8}]))
    problems = spec.validate()
    assert any(knob in p and "batterySoc" in p for p in problems), problems


def test_build_spec_refuses_a_spec_with_an_unknown_read_knob() -> None:
    doc = _doc([{"field": "batterySoc", "address": 5, "shift": 8}])
    assert build_spec(doc, model_id="eg4_18kpv") is None


def test_build_spec_accepts_every_knob_the_decoder_implements() -> None:
    doc = _doc(
        [
            _SOC,
            {
                "field": "e",
                "address": 40,
                "words": 2,
                "lowWordFirst": True,
                "signed": True,
                "scale": 0.1,
                "offset": 0.0,
                "unitId": 1,
                "sentinel": 65535,
                "functionCode": "FC04",
            },
        ]
    )
    assert build_spec(doc, model_id="eg4_18kpv") is not None
