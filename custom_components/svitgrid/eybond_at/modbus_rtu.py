"""Modbus RTU codec for the EyBond/SmartESS collector's payload.

Pure functions. No sockets, no clock, no Home Assistant imports.

The collector tunnels **bare** Modbus RTU: no wrapper, no length prefix, and
no transaction id. All 3,113 Modbus frames in the 2026-08-20 capture validated
against the RTU CRC16, which is what established the framing.

Words are returned RAW and unsigned. Signedness and scale are properties of a
FIELD, not of a frame -- in one captured block register 202 is an unsigned
voltage and register 213 is a signed power, and only the register map knows
which is which. A codec that guessed would corrupt one of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# FC3 caps at 125 registers, because 125 * 2 = 250 fits the single-byte count.
MAX_READ_COUNT = 125

# FC16 caps at 123, because the request also spends 7 bytes on the header and
# 2 on the CRC: 123 * 2 = 246, and 246 + 9 = 255.
MAX_WRITE_COUNT = 123

FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04

# Function code 6 is NOT part of this protocol. The constant survives only so
# the demultiplexer can still measure an FC06 frame's length if the vendor
# cloud ever sends one -- this stream carries the vendor's traffic as well as
# ours, and a frame we cannot length-parse desynchronises the whole connection
# (see demux.py). There is deliberately no builder and no response parser for
# it: the device answers an FC06 write with SILENCE, not an exception, and the
# collector then drops the TCP connection. Every write the integration
# attempted before 2026-08-23 failed exactly that way.
FC_WRITE_SINGLE = 0x06

# The only write this protocol defines. Confirmed by the vendor document
# (`SMG-RS232 Communication Protocol V1.0.1`), by three independent community
# codebases, and by a live capture against the bench unit.
FC_WRITE_MULTIPLE = 0x10

EXCEPTION_MASK = 0x80

_READ_FUNCTIONS = (FC_READ_HOLDING, FC_READ_INPUT)

# ── Exception codes, as THIS VENDOR defines them ─────────────────────────
#
# These diverge from stock Modbus and the divergence matters. In stock Modbus
# 0x01 is ILLEGAL FUNCTION; here it means the register is read-only. A reader
# who assumes the stock meaning is pointed straight back at the function code
# -- the exact wrong turn, since an unsupported function code on this device
# produces no reply at all.
EXC_READ_ONLY = 0x01
EXC_OUT_OF_RANGE = 0x03
EXC_WRONG_MODE = 0x07


class ModbusError(Exception):
    """A frame is malformed, truncated, or fails its CRC."""


class ModbusExceptionError(ModbusError):
    """The device returned a Modbus exception response."""

    def __init__(self, code: int) -> None:
        super().__init__(f"modbus exception 0x{code:02X}")
        self.code = code


def crc16(data: bytes) -> int:
    """Modbus RTU CRC16: polynomial 0xA001, seed 0xFFFF, transmitted little-endian."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _append_crc(pdu: bytes) -> bytes:
    return pdu + crc16(pdu).to_bytes(2, "little")


def _check_crc(frame: bytes) -> None:
    if len(frame) < 4:
        raise ModbusError(f"frame too short to carry a CRC: {frame.hex()}")
    expected = int.from_bytes(frame[-2:], "little")
    actual = crc16(frame[:-2])
    if actual != expected:
        raise ModbusError(f"CRC mismatch: frame says {expected:#06x}, computed {actual:#06x}")


def build_read(slave: int, address: int, count: int, function: int = FC_READ_HOLDING) -> bytes:
    """Build a read request. Reproduces the collector's own captured requests."""
    if not 0 <= slave <= 0xFF:
        raise ModbusError(f"slave id out of range: {slave}")
    if not 0 <= address <= 0xFFFF:
        raise ModbusError(f"address out of range: {address}")
    if not 1 <= count <= MAX_READ_COUNT:
        raise ModbusError(f"count out of range: {count}")
    if function not in _READ_FUNCTIONS:
        raise ModbusError(f"not a read function code: {function:#04x}")
    pdu = bytes([slave, function]) + address.to_bytes(2, "big") + count.to_bytes(2, "big")
    return _append_crc(pdu)


def parse_read_response(frame: bytes) -> list[int]:
    """Return the raw uint16 words of a read response.

    Raises `ModbusExceptionError` for an exception response, so a caller can
    tell "this address does not exist" from "this frame is broken".
    """
    if len(frame) < 5:
        raise ModbusError(f"frame too short: {frame.hex()}")
    _check_crc(frame)
    function = frame[1]
    if function & EXCEPTION_MASK:
        raise ModbusExceptionError(frame[2])
    if function not in _READ_FUNCTIONS:
        raise ModbusError(f"not a read response: function {function:#04x}")
    byte_count = frame[2]
    if byte_count % 2:
        raise ModbusError(f"odd byte count: {byte_count}")
    if len(frame) != 3 + byte_count + 2:
        raise ModbusError(f"byte count {byte_count} disagrees with frame length {len(frame)}")
    data = frame[3 : 3 + byte_count]
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def to_signed(word: int) -> int:
    """Reinterpret one raw uint16 as int16."""
    return word - 0x10000 if word & 0x8000 else word


def words_to_ascii(words: list[int]) -> str:
    """Decode an ASCII string packed two characters per register, big-endian.

    Trailing NULs are stripped: the captured serial number occupies 14 of the
    24 bytes register 186 returns.
    """
    raw = b"".join(word.to_bytes(2, "big") for word in words)
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def build_write_multiple(slave: int, address: int, values: Sequence[int]) -> bytes:
    """Build a write-multiple-holding-registers request (function code 16).

    The only write frame this protocol defines. Layout::

        slave, 0x10, addr_hi, addr_lo, qty_hi, qty_lo, byte_count,
        data_hi, data_lo, ..., crc_lo, crc_hi

    Every bound is CHECKED rather than masked. A truncated address writes a
    different register and a truncated value a different setpoint, both
    silently -- and on this device a setpoint is a battery charge voltage.

    Only a single-register write (``len(values) == 1``) has been confirmed
    against real hardware. The encoder is general because the function code
    is, but nothing in the integration writes more than one register at a
    time, and a multi-register write to this family remains unproven.
    """
    if not 0 <= slave <= 0xFF:
        raise ModbusError(f"slave id out of range: {slave}")
    if not 0 <= address <= 0xFFFF:
        raise ModbusError(f"address out of range: {address}")
    words = list(values)
    if not 1 <= len(words) <= MAX_WRITE_COUNT:
        raise ModbusError(f"register count out of range: {len(words)}")
    if address + len(words) - 1 > 0xFFFF:
        raise ModbusError(
            f"write of {len(words)} registers from {address} runs past the address space"
        )
    for value in words:
        if not 0 <= value <= 0xFFFF:
            raise ModbusError(f"value out of range: {value}")

    pdu = (
        bytes([slave, FC_WRITE_MULTIPLE])
        + address.to_bytes(2, "big")
        + len(words).to_bytes(2, "big")
        + bytes([len(words) * 2])
        + b"".join(word.to_bytes(2, "big") for word in words)
    )
    return _append_crc(pdu)


def parse_write_multiple_response(frame: bytes) -> tuple[int, int]:
    """Return the ``(address, quantity)`` an FC16 acknowledgement carried back.

    **The acknowledgement echoes the QUANTITY of registers written, not the
    value.** The captured hardware exchange wrote 0 to register 303 and the
    device answered ``00 01`` -- one register. Code that reads that trailing
    word as the value written will reject a perfectly good acknowledgement for
    every value except 1.

    The acknowledgement proves the device PARSED the request. It does not
    prove the setting took effect, so a caller that cares must read the
    register back (see `write_register_verified`).

    An exception response is raised as `ModbusExceptionError` so a caller can
    tell a refusal it can explain from a frame that is simply broken.
    """
    if len(frame) < 5:
        raise ModbusError(f"frame too short: {frame.hex()}")
    _check_crc(frame)
    function = frame[1]
    if function & EXCEPTION_MASK:
        raise ModbusExceptionError(frame[2])
    if function != FC_WRITE_MULTIPLE:
        raise ModbusError(
            f"not a write-multiple response: function {function:#04x} "
            f"(this protocol writes only with {FC_WRITE_MULTIPLE:#04x})"
        )
    if len(frame) != 8:
        raise ModbusError(
            f"write acknowledgement must be 8 bytes, got {len(frame)}: {frame.hex()}"
        )
    return ((frame[2] << 8) | frame[3], (frame[4] << 8) | frame[5])


@dataclass(frozen=True)
class WriteExceptionDescription:
    """A device refusal, split into something to branch on and something to say."""

    code: int

    # Stable, machine-readable. Callers branch on this; it is not prose and
    # must not be reworded to suit a UI.
    reason: str

    # For a human. States what is actually blocking the write.
    message: str


def describe_write_exception(code: int) -> WriteExceptionDescription:
    """Explain a write refusal in the terms THIS VENDOR uses.

    Three refusals are documented for this family, and they call for three
    different next steps -- which is the whole reason they are not collapsed
    into one failure:

    ``0x01`` the register is read-only; nothing the user can do.

    ``0x03`` the value is out of the permitted range -- **or** it breaks a
    cross-register rule. The vendor documents that register 341 must exceed
    343, so an individually-valid percentage is still refused when it would
    invert that pair. Reporting this as "your value is malformed" sends the
    user to change the one field that was already fine.

    ``0x07`` not modifiable in the current working mode. Field-reported: one
    user's writes began working only after an inverter restart. Without
    naming the mode, the user retries the identical write forever.

    An undocumented code is reported as such rather than guessed at.
    """
    if code == EXC_READ_ONLY:
        return WriteExceptionDescription(
            code=code,
            reason="read_only",
            message="the inverter reports this register as read-only",
        )
    if code == EXC_OUT_OF_RANGE:
        return WriteExceptionDescription(
            code=code,
            reason="out_of_range",
            message=(
                "the inverter rejected this value as out of range. That can be the "
                "value itself, or a rule spanning two registers -- register 341 must "
                "stay above register 343, so a value that is fine on its own is still "
                "refused when it would invert that pair"
            ),
        )
    if code == EXC_WRONG_MODE:
        return WriteExceptionDescription(
            code=code,
            reason="wrong_mode",
            message=(
                "the inverter will not change this register in its current working "
                "mode. Retrying the same value will not help until the mode changes; "
                "some units have only accepted writes after a restart"
            ),
        )
    return WriteExceptionDescription(
        code=code,
        reason="device_error",
        message=f"the inverter refused the write with undocumented exception 0x{code:02X}",
    )
