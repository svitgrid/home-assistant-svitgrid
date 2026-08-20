"""Frame demultiplexer for the collector's single shared socket.

Pure functions. No sockets, no clock, no Home Assistant imports.

── The problem ───────────────────────────────────────────────────────────
One TCP connection carries two protocols at once:

  * AT-text lines, delimited by CRLF, and
  * bare Modbus RTU, which has **no length prefix and no delimiter**.

On a serial line, RTU frames are separated by inter-frame silence. TCP does
not preserve that silence, and it may split or coalesce anything. So a frame
boundary has to be *derived*:

  * an AT line ends at the first CRLF;
  * a read RESPONSE is 3 + byte_count + 2, and the byte count is the third
    byte, so at least three bytes must arrive before the length is knowable;
  * an exception response is always 5 bytes;
  * a read REQUEST is always 8 bytes.

Direction therefore has to be told to the parser. It cannot be inferred: an
8-byte request and an 8-byte response are indistinguishable by shape alone.

── Why a bad CRC is fatal ────────────────────────────────────────────────
This protocol has no transaction id, so a desynchronised stream cannot be
resynchronised by matching an id. Skipping a byte and hoping would silently
reinterpret the rest of the connection. A CRC failure raises, and the caller
drops the connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from .at_codec import PREFIX, TERMINATOR
from .modbus_rtu import (
    EXCEPTION_MASK,
    FC_READ_HOLDING,
    FC_READ_INPUT,
    FC_WRITE_MULTIPLE,
    FC_WRITE_SINGLE,
    ModbusError,
    crc16,
)

EXCEPTION_FRAME_LEN = 5
REQUEST_FRAME_LEN = 8
_ECHO_RESPONSE_LEN = 8
_READ_FUNCTIONS = (FC_READ_HOLDING, FC_READ_INPUT)
_WRITE_FUNCTIONS = (FC_WRITE_SINGLE, FC_WRITE_MULTIPLE)


class Direction(Enum):
    """Which way the bytes are travelling.

    `REQUEST` is anything sent TO the collector -- the vendor cloud's polling,
    and our own injected reads. `RESPONSE` is what the collector sends back.
    """

    REQUEST = auto()
    RESPONSE = auto()


class FrameKind(Enum):
    AT = auto()
    MODBUS = auto()


@dataclass(frozen=True)
class Frame:
    kind: FrameKind
    raw: bytes


def _modbus_frame_length(buf: bytes, direction: Direction) -> int | None:
    """Total frame length, or None when more bytes are needed to know it."""
    if len(buf) < 2:
        return None
    function = buf[1]

    if function & EXCEPTION_MASK:
        return EXCEPTION_FRAME_LEN

    if direction is Direction.REQUEST:
        if function in _READ_FUNCTIONS or function == FC_WRITE_SINGLE:
            return REQUEST_FRAME_LEN
        if function == FC_WRITE_MULTIPLE:
            if len(buf) < 7:
                return None
            return 7 + buf[6] + 2
        raise ModbusError(f"unknown request function code: {function:#04x}")

    if function in _READ_FUNCTIONS:
        if len(buf) < 3:
            return None
        return 3 + buf[2] + 2
    if function in _WRITE_FUNCTIONS:
        return _ECHO_RESPONSE_LEN
    raise ModbusError(f"unknown response function code: {function:#04x}")


def take_frame(buf: bytes, direction: Direction) -> tuple[Frame | None, bytes]:
    """Take one frame off the front of `buf`.

    Returns `(frame, remainder)`. `frame` is None when `buf` does not yet hold
    a complete frame, and `remainder` is then `buf` unchanged.
    """
    if not buf:
        return None, buf

    if buf.startswith(PREFIX):
        end = buf.find(TERMINATOR)
        if end == -1:
            return None, buf
        cut = end + len(TERMINATOR)
        return Frame(kind=FrameKind.AT, raw=buf[:cut]), buf[cut:]

    # A partial AT line still looks like a prefix of "AT+".
    if PREFIX.startswith(buf[: len(PREFIX)]):
        return None, buf

    length = _modbus_frame_length(buf, direction)
    if length is None or len(buf) < length:
        return None, buf

    frame = buf[:length]
    expected = int.from_bytes(frame[-2:], "little")
    actual = crc16(frame[:-2])
    if actual != expected:
        raise ModbusError(
            f"CRC mismatch on a {length}-byte frame: "
            f"frame says {expected:#06x}, computed {actual:#06x}"
        )
    return Frame(kind=FrameKind.MODBUS, raw=frame), buf[length:]


def split_frames(buf: bytes, direction: Direction) -> tuple[list[Frame], bytes]:
    """Take as many complete frames as `buf` holds.

    Returns `(frames, remainder)`. Feed the remainder back in front of the next
    read.
    """
    frames: list[Frame] = []
    rest = buf
    while True:
        frame, rest = take_frame(rest, direction)
        if frame is None:
            return frames, rest
        frames.append(frame)
