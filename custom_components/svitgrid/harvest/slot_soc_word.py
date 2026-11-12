"""A Deye TOU slot's SOC register is ONE 16-bit word holding TWO settings.

Batt is the low byte and Gen the high byte (LCD photo of inverter
``RInTOQcxtMoAZUgVxmp9``, 2026-11-12: 14175 = 0x375F = Gen 55, Batt 95).

A word is valid only when both bytes are 0-100. Anything else (for example
30728) is not a packed pair, so callers keep the old behavior: treat it as
implausible and refuse to write it.

Python twin of ``services/api/src/tou/slot-soc-word.ts`` and
``packages/inverter_protocol/lib/src/registers/slot_soc_word.dart`` in the
svitgrid monorepo.
"""

from __future__ import annotations


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def split_slot_soc_word(word: object) -> tuple[int, int] | None:
    """Return ``(batt, gen)`` for *word*, or None when it is not a valid pair."""
    if not _is_int(word) or not 0 <= word <= 0xFFFF:  # type: ignore[operator]
        return None
    batt = word & 0xFF  # type: ignore[operator]
    gen = word >> 8  # type: ignore[operator]
    if batt > 100 or gen > 100:
        return None
    return batt, gen


def is_valid_slot_soc_word(word: object) -> bool:
    """Return True when *word* holds a valid Batt/Gen pair."""
    return split_slot_soc_word(word) is not None


def _check_byte(name: str, value: object) -> None:
    if not _is_int(value) or not 0 <= value <= 100:  # type: ignore[operator]
        raise ValueError(f"{name} must be an integer 0-100, got {value}")


def compose_slot_soc_word(live: int, *, batt: int | None = None, gen: int | None = None) -> int:
    """Return the word to write: change one byte, keep the other from *live*.

    Never clamps a raw word. Raises ``ValueError`` on an invalid *live* word,
    rather than guessing which byte is real, and on a byte outside 0-100. A
    None *batt* or *gen* keeps that byte.
    """
    if not is_valid_slot_soc_word(live):
        raise ValueError(f"live slot SOC word {live} is not a valid Batt/Gen pair")
    word = live
    if batt is not None:
        _check_byte("batt", batt)
        word = (word & 0xFF00) | batt
    if gen is not None:
        _check_byte("gen", gen)
        word = (gen << 8) | (word & 0x00FF)
    return word
