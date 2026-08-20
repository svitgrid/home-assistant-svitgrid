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

# FC3 caps at 125 registers, because 125 * 2 = 250 fits the single-byte count.
MAX_READ_COUNT = 125

FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_SINGLE = 0x06
FC_WRITE_MULTIPLE = 0x10
EXCEPTION_MASK = 0x80

_READ_FUNCTIONS = (FC_READ_HOLDING, FC_READ_INPUT)


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
