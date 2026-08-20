"""Register maps, keyed by the protocol number the device reports.

── Why this is a dispatch table and not a constant ───────────────────────
Anenji spans at least two register maps. `srne_anenji_12k` is hardware-proven
on the SRNE ASF-HF map; the SmartESS family clones EASUN/ISolar SMG II. And
inside SMG II the map is versioned: register 184 carries a protocol number,
and the meaning of the telemetry block changes with it.

Our bench unit reports **11**. The most complete published SMG II map
documents protocols **3 to 6** and calls register 202 "Total Grid Current" --
which would make an idle inverter draw 238 A. On protocol 11 it is AC
voltage, and reads 228.4 V. Neither source is wrong; they describe different
versions of the same product line.

So: only protocol numbers we have measured appear here, and an unrecognised
one publishes nothing rather than being decoded on a guess.

── Confidence is part of the map ─────────────────────────────────────────
The bench unit ran from a wall socket with no battery, no panels, and no
load, so those registers read zero. Zero is **undecidable**, not evidence.
Fields verified against live values on hardware are `CONFIRMED`; fields whose
address comes from a map that matched our hardware everywhere it could be
checked, but which we have not yet exercised, are `IDENTIFIED`.

Source for the addresses: `syssi/esphome-smg-ii`, cross-checked register for
register against our own capture -- including all 18 configuration registers,
which decode as a coherent 24 V lead-acid charge profile. See
`docs/inverter-research/2026-08-20-anenji-smg-ii-register-families.md` in the
main svitgrid repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .modbus_rtu import to_signed

# The collector answered 15-register reads happily and the vendor cloud never
# asked for more than 15 in one go, so 32 is a conservative ceiling.
MAX_BLOCK_REGISTERS = 32
# Registers closer than this are merged into one read: one extra round trip
# costs more than a few wasted words at 9600 baud.
MAX_BLOCK_GAP = 8


class Confidence(Enum):
    CONFIRMED = auto()
    """Verified against a live value on real hardware."""

    IDENTIFIED = auto()
    """Address known from a map that matched our hardware; not yet exercised."""


@dataclass(frozen=True)
class FieldSpec:
    field: str
    address: int
    scale: float = 1.0
    signed: bool = False
    unit: str = ""
    confidence: Confidence = Confidence.IDENTIFIED


@dataclass(frozen=True)
class RegisterMap:
    name: str
    protocol_numbers: tuple[int, ...]
    fields: tuple[FieldSpec, ...]

    def decode_block(self, base_address: int, words: list[int]) -> dict[str, float]:
        """Decode the fields this block covers. Others are simply absent.

        A partial block yields partial results rather than raising: a short
        read is a transport problem, and inventing zeros for the missing
        fields would publish a reading that looks complete.
        """
        end = base_address + len(words)
        values: dict[str, float] = {}
        for spec in self.fields:
            if not base_address <= spec.address < end:
                continue
            raw = words[spec.address - base_address]
            if spec.signed:
                raw = to_signed(raw)
            values[spec.field] = raw * spec.scale
        return values

    def read_plan(
        self,
        max_count: int = MAX_BLOCK_REGISTERS,
        max_gap: int = MAX_BLOCK_GAP,
    ) -> list[tuple[int, int]]:
        """Contiguous `(address, count)` reads covering every field."""
        addresses = sorted({spec.address for spec in self.fields})
        if not addresses:
            return []
        plan: list[tuple[int, int]] = []
        start = previous = addresses[0]
        for address in addresses[1:]:
            too_far = address - previous > max_gap
            too_big = address - start + 1 > max_count
            if too_far or too_big:
                plan.append((start, previous - start + 1))
                start = address
            previous = address
        plan.append((start, previous - start + 1))
        return plan


SMG_II_PROTOCOL_11 = RegisterMap(
    name="EASUN/ISolar SMG II, protocol 11",
    protocol_numbers=(11,),
    fields=(
        FieldSpec("runningState", 201),
        FieldSpec("gridVoltageL1", 202, 0.1, unit="V", confidence=Confidence.CONFIRMED),
        FieldSpec("gridFrequency", 203, 0.01, unit="Hz", confidence=Confidence.CONFIRMED),
        FieldSpec("gridPower", 204, unit="W", signed=True),
        FieldSpec("loadVoltageL1", 210, 0.1, unit="V", confidence=Confidence.CONFIRMED),
        FieldSpec("loadPower", 213, unit="W", signed=True),
        FieldSpec("batteryVoltage", 215, 0.1, unit="V"),
        FieldSpec("batteryCurrent", 216, 0.1, signed=True, unit="A"),
        FieldSpec("batteryPower", 217, unit="W", signed=True),
        # PV is published as one AVERAGE triple on this protocol, not per
        # string. There is no pv2Voltage to read.
        FieldSpec("pv1Voltage", 219, 0.1, unit="V"),
        FieldSpec("pv1Current", 220, 0.1, unit="A"),
        FieldSpec("pv1Power", 223, unit="W"),
        FieldSpec("inverterTemperature", 227, unit="°C", confidence=Confidence.CONFIRMED),
        FieldSpec("batterySoc", 229, unit="%"),
    ),
)

# Only protocol numbers measured on real hardware belong here.
PLATFORMS: dict[int, RegisterMap] = {
    protocol: SMG_II_PROTOCOL_11 for protocol in SMG_II_PROTOCOL_11.protocol_numbers
}
