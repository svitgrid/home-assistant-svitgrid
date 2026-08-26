"""The SMG II write path speaks FC16, and nothing else.

── Why this file exists ──────────────────────────────────────────────────
The integration shipped its write path on **FC06 (write single register)**, a
function code this protocol does not define. The device does not refuse it --
it does not answer it at all, and the collector then drops the TCP connection.
Every write ever attempted through the shipped path failed that way.

The protocol defines exactly two frames: FC03 to read, FC16 (0x10) to write.
That is the vendor document (`SMG-RS232 Communication Protocol V1.0.1`), three
independent community codebases (`ha-eybond-local` drivers/smg.py;
`syssi/esphome-smg-ii`, which sets `use_write_multiple: true` on all 168
writable entities; makstt232's `set_smg_param`), and -- decisively -- a live
capture against the bench unit through this repository's own `EybondAtHub`.

── The vectors ───────────────────────────────────────────────────────────
`CAPTURED_REQUEST` and `CAPTURED_RESPONSE` below are that capture, byte for
byte. They are the whole point of this file. A frame invented to match our own
encoder proves only that the encoder agrees with itself; it cannot catch the
bug that shipped, because an FC06 encoder and an FC06 decoder agree perfectly.

── The trap the response shape sets ──────────────────────────────────────
FC16 echoes the address and the **QUANTITY of registers written** -- not the
value. In the capture, `00 01` is "one register", and the value written was
0. Those are different numbers that happen to look interchangeable at a
glance, and a parser that reads the last data word as the value will reject a
perfectly good acknowledgement whenever the value is not 1.
"""

import pytest

from custom_components.svitgrid.eybond_at.modbus_rtu import (
    FC_WRITE_MULTIPLE,
    ModbusError,
    ModbusExceptionError,
    build_read,
    build_write_multiple,
    crc16,
    describe_write_exception,
    parse_write_multiple_response,
)

# Captured on the bench unit, 2026-08-23, through EybondAtHub. Write register
# 303 (buzzer mode) = 0. The read-back confirmed the value changed; it was
# then restored to its original.
#
#   slave 01 | fc 10 | addr 012f (=303) | qty 0001 | bytes 02 | data 0000 | crc b10f
CAPTURED_REQUEST = bytes.fromhex("0110012f0001020000b10f")
#   slave 01 | fc 10 | addr 012f (=303) | qty 0001 | crc 31fc
CAPTURED_RESPONSE = bytes.fromhex("0110012f000131fc")


def _exception_frame(slave: int, function: int, code: int) -> bytes:
    body = bytes([slave, function | 0x80, code])
    return body + crc16(body).to_bytes(2, "little")


# ── the encoder reproduces the captured request exactly ───────────────────


def test_build_write_multiple_reproduces_the_captured_request():
    """The one vector that proves we speak what the hardware answered.

    Byte-for-byte against a frame the device acknowledged -- CRC included, so
    this also pins the byte order of the trailing checksum.
    """
    assert build_write_multiple(slave=1, address=303, values=[0]) == CAPTURED_REQUEST


def test_the_write_frame_carries_function_code_16_not_6():
    """The shipped bug, stated directly.

    FC06 is not in this protocol. A device that receives one goes silent, so
    this assertion is the difference between a write that works and a dropped
    connection.
    """
    frame = build_write_multiple(slave=1, address=324, values=[560])
    assert frame[1] == FC_WRITE_MULTIPLE == 0x10
    assert frame[1] != 0x06


def test_the_write_frame_carries_quantity_and_byte_count():
    # addr 0144 (=324), qty 0001, byte count 02, data 0230 (=560).
    frame = build_write_multiple(slave=1, address=324, values=[560])
    assert frame[:9] == bytes([0x01, 0x10, 0x01, 0x44, 0x00, 0x01, 0x02, 0x02, 0x30])
    assert len(frame) == 11


def test_a_multi_register_write_sizes_quantity_and_byte_count_together():
    frame = build_write_multiple(slave=1, address=341, values=[50, 60, 70])
    assert frame[4:7] == bytes([0x00, 0x03, 0x06])  # qty 3, byte count 6
    assert len(frame) == 7 + 6 + 2


def test_build_write_multiple_accepts_the_full_uint16_range():
    assert len(build_write_multiple(slave=1, address=320, values=[0])) == 11
    assert len(build_write_multiple(slave=1, address=320, values=[65535])) == 11


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slave": 256, "address": 303, "values": [0]},
        {"slave": -1, "address": 303, "values": [0]},
        {"slave": 1, "address": 0x10000, "values": [0]},
        {"slave": 1, "address": -1, "values": [0]},
        {"slave": 1, "address": 303, "values": [0x10000]},
        {"slave": 1, "address": 303, "values": [-1]},
        {"slave": 1, "address": 303, "values": []},
    ],
)
def test_build_write_multiple_refuses_out_of_range_rather_than_truncating(kwargs):
    """Every bound is CHECKED, never masked.

    A truncated address writes a different register and a truncated value a
    different setpoint, both silently -- and on this device a setpoint is a
    battery charge voltage.
    """
    with pytest.raises(ModbusError):
        build_write_multiple(**kwargs)


# ── the parser accepts the captured acknowledgement ───────────────────────


def test_parse_write_multiple_response_reads_the_captured_acknowledgement():
    address, quantity = parse_write_multiple_response(CAPTURED_RESPONSE)
    assert address == 303
    assert quantity == 1


def test_the_acknowledgement_carries_quantity_not_the_value_written():
    """The trap, pinned.

    The captured write set register 303 to **0** and the device answered
    `00 01`. Anything treating that trailing word as the value written would
    read 1, mismatch, and reject an acknowledgement that was entirely correct.
    """
    _, quantity = parse_write_multiple_response(CAPTURED_RESPONSE)
    written_value = int.from_bytes(CAPTURED_REQUEST[7:9], "big")
    assert written_value == 0
    assert quantity == 1
    assert quantity != written_value


def test_parse_write_multiple_response_rejects_a_bad_crc():
    frame = bytearray(CAPTURED_RESPONSE)
    frame[-1] ^= 0xFF
    with pytest.raises(ModbusError):
        parse_write_multiple_response(bytes(frame))


def test_parse_write_multiple_response_rejects_an_fc06_reply():
    """An FC06 reply is not evidence of anything on this device.

    Nothing here has ever produced one. Accepting it would quietly re-open the
    path this fix closes.
    """
    body = bytes([0x01, 0x06, 0x01, 0x2F, 0x00, 0x00])
    with pytest.raises(ModbusError):
        parse_write_multiple_response(body + crc16(body).to_bytes(2, "little"))


def test_parse_write_multiple_response_rejects_a_read_response():
    with pytest.raises(ModbusError):
        parse_write_multiple_response(build_read(slave=1, address=303, count=1))


def test_parse_write_multiple_response_rejects_a_wrong_length_frame():
    body = CAPTURED_RESPONSE[:-2] + b"\x00"
    with pytest.raises(ModbusError):
        parse_write_multiple_response(body + crc16(body).to_bytes(2, "little"))


# ── exception replies ─────────────────────────────────────────────────────


def test_parse_write_multiple_response_surfaces_a_modbus_exception():
    frame = _exception_frame(1, FC_WRITE_MULTIPLE, 0x07)
    with pytest.raises(ModbusExceptionError) as excinfo:
        parse_write_multiple_response(frame)
    assert excinfo.value.code == 0x07


def test_the_exception_function_code_is_fc16_with_the_high_bit():
    assert _exception_frame(1, FC_WRITE_MULTIPLE, 0x01)[1] == 0x90


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (0x01, "read_only"),
        (0x03, "out_of_range"),
        (0x07, "wrong_mode"),
    ],
)
def test_each_documented_exception_code_has_its_own_reason(code, reason):
    """Three refusals a user can act on differently, so three outcomes.

    "the inverter will not change this while it is running" and "that value is
    not allowed" call for opposite next steps. Collapsing them into one
    failure tells the user nothing they can use.
    """
    assert describe_write_exception(code).reason == reason


def test_an_undocumented_exception_code_is_not_guessed_at():
    described = describe_write_exception(0x42)
    assert described.reason == "device_error"
    assert "0x42" in described.message


def test_the_wrong_mode_refusal_says_the_mode_is_the_obstacle():
    """0x07 is field-reported: one user's writes began working only after an
    inverter restart. The message has to point at the mode, or the user
    retries the identical write forever."""
    message = describe_write_exception(0x07).message.lower()
    assert "mode" in message


def test_the_out_of_range_refusal_does_not_blame_the_value_alone():
    """0x03 can mean a CROSS-REGISTER rule, not a malformed value.

    The vendor documents that register 341 must exceed 343, so an
    individually-valid percentage is still refused when it would invert that
    pair. Telling the user their value is malformed sends them to change the
    one thing that was already fine.
    """
    message = describe_write_exception(0x03).message.lower()
    assert "341" in message and "343" in message


def test_the_read_only_refusal_is_not_reported_as_a_bad_function_code():
    """0x01 is ILLEGAL FUNCTION in stock Modbus, but this vendor uses it for
    "this register is read-only". Reporting the stock meaning would point the
    next reader straight back at the function code -- the exact wrong turn,
    since an unsupported code on this device produces SILENCE, not 0x01."""
    message = describe_write_exception(0x01).message.lower()
    assert "read-only" in message or "read only" in message
    assert "function code" not in message


# ── the FC06 builder is gone, not merely unused ───────────────────────────


def test_the_fc06_write_helpers_no_longer_exist():
    """A dead path that still looks usable is a trap for the next reader.

    `build_write_single` produced frames this device ignores. Leaving it
    importable invites exactly the mistake that shipped, so it is removed
    rather than deprecated.
    """
    from custom_components.svitgrid.eybond_at import modbus_rtu

    assert not hasattr(modbus_rtu, "build_write_single")
    assert not hasattr(modbus_rtu, "parse_write_response")
