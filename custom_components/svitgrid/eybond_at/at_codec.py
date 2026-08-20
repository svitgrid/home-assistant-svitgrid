"""AT-text line codec for the EyBond/SmartESS collector.

Pure functions. No sockets, no clock, no Home Assistant imports.

── The wire, as measured ─────────────────────────────────────────────────
Captured 2026-08-20 from collector `I20000282044487591` (type
`WFBLE.DTU.Module-x02`, firmware `8.50.18.3`, AT version `1.14`) by relaying
its session to the vendor cloud and recording both directions::

    query      AT+{CMD}?\\r\\n
    write      AT+{CMD}={VALUE}\\r\\n
    response   AT+{CMD}:{VALUE}\\r\\n

The collector answers; it never initiates. Over 869 observed frames the
request and response counts paired exactly (67/67 AT, 84/84 Modbus), so the
line is strictly serialized with no pipelining.

── Why this file exists at all ───────────────────────────────────────────
An earlier implementation of "the EyBond protocol" built an 8-byte binary
header with a transaction id, in C, Dart and Python, from a framing
transcribed out of a third-party project. The hardware speaks neither that
header nor any transaction id. See
`docs/inverter-registers-deye-vs-anenji.md` in the main svitgrid repo.

Two consequences are load-bearing here:

1. **There is no transaction id.** A response is matched to its request by
   the command name, and by nothing else. Never hold two requests in flight.
2. **One socket carries two protocols.** AT lines and bare Modbus RTU share
   the connection, so `is_at_line` is the demultiplexer's first gate. It
   requires BOTH the prefix and the terminator, because a Modbus payload can
   legitimately contain the bytes `41 54 2b`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PREFIX = b"AT+"
TERMINATOR = b"\r\n"

# A write is acknowledged with a W-code. Both W000 and W001 were observed
# following writes that appeared to take effect (`AT+SYST=` and `AT+UART=`
# returned W000; `AT+LEDCTR=` consistently returned W001). Whether the two
# differ in meaning is NOT established, so this predicate claims only that a
# write was acknowledged -- never that it succeeded.
_WRITE_ACK = re.compile(r"^W\d{3}$")

# Observed once, in reply to queries the collector does not implement
# (`AT+DEVLIST?`, `AT+PROTOCOL?`, `AT+WAP?`, `AT+WSSSID?` all returned R001).
_UNSUPPORTED = "R001"

# Anything that would terminate or re-frame a line if embedded in a value.
_FORBIDDEN_IN_VALUE = ("\r", "\n")
# `?` `=` and `:` are the three syntactic separators, so a command carrying
# one of them would produce a frame that parses back as a different command.
_FORBIDDEN_IN_COMMAND = ("\r", "\n", "?", "=", ":")


class AtProtocolError(Exception):
    """A frame is not valid AT-text, or a value cannot be safely encoded."""


@dataclass(frozen=True)
class AtResponse:
    """One `AT+{CMD}:{VALUE}` line from the collector."""

    command: str
    value: str

    @property
    def is_write_ack(self) -> bool:
        """The collector acknowledged a write. Not a claim that it succeeded."""
        return bool(_WRITE_ACK.match(self.value))

    @property
    def is_unsupported(self) -> bool:
        """The collector does not implement this command."""
        return self.value == _UNSUPPORTED


def _validate_command(command: str) -> None:
    if not command:
        raise AtProtocolError("command is empty")
    if not command.isascii():
        raise AtProtocolError(f"command is not ASCII: {command!r}")
    for bad in _FORBIDDEN_IN_COMMAND:
        if bad in command:
            raise AtProtocolError(f"command contains {bad!r}: {command!r}")


def build_query(command: str) -> bytes:
    """Build `AT+{COMMAND}?\\r\\n`."""
    _validate_command(command)
    return PREFIX + command.encode("ascii") + b"?" + TERMINATOR


def build_write(command: str, value: str) -> bytes:
    """Build `AT+{COMMAND}={VALUE}\\r\\n`."""
    _validate_command(command)
    if not value.isascii():
        raise AtProtocolError(f"value is not ASCII: {value!r}")
    for bad in _FORBIDDEN_IN_VALUE:
        if bad in value:
            # A value carrying CRLF would split into two frames on the wire and
            # desynchronise every later response.
            raise AtProtocolError(f"value contains {bad!r}: {value!r}")
    return PREFIX + command.encode("ascii") + b"=" + value.encode("ascii") + TERMINATOR


def is_at_line(frame: bytes) -> bool:
    """True when `frame` is a complete AT-text line.

    Both conditions are required. Checking only the prefix would misclassify a
    Modbus response whose payload happens to begin with `41 54 2b`.
    """
    return frame.startswith(PREFIX) and frame.endswith(TERMINATOR)


def parse_response(frame: bytes) -> AtResponse:
    """Parse one `AT+{CMD}:{VALUE}\\r\\n` line.

    An empty value is legal: the heartbeat reply is literally `AT+HTBT:`.
    """
    if not is_at_line(frame):
        raise AtProtocolError(f"not an AT line: {frame!r}")
    body = frame[len(PREFIX) : -len(TERMINATOR)]
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError as err:
        raise AtProtocolError(f"AT line is not ASCII: {frame!r}") from err
    if ":" not in text:
        raise AtProtocolError(f"AT line carries no value separator: {frame!r}")
    # Split on the FIRST colon only. `AT+CLDSRVHOST1:` carries a host,port,proto
    # triple, and a greedy split would lose the tail.
    command, _, value = text.partition(":")
    if not command:
        raise AtProtocolError(f"AT line has an empty command: {frame!r}")
    return AtResponse(command=command, value=value)
