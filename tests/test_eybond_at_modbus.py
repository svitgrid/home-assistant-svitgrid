"""Modbus RTU codec and the shared-socket frame demultiplexer.

Fixtures are VERBATIM frames lifted out of the 2026-08-20 capture of collector
`I20000282044487591`, selected by CRC validity. All 3,113 captured Modbus
frames validated, which is itself the evidence that the payload is bare RTU
with no wrapper.

The demultiplexer exists because ONE socket carries AT-text lines and bare
Modbus RTU at the same time, and RTU has neither a length prefix nor a
delimiter. Over a serial line, frames are separated by inter-frame silence.
Over TCP that silence does not survive, so the length has to be derived from
the function code and the byte count.
"""

import pytest

from custom_components.svitgrid.eybond_at.demux import (
    Direction,
    Frame,
    FrameKind,
    split_frames,
    take_frame,
)
from custom_components.svitgrid.eybond_at.modbus_rtu import (
    ModbusError,
    ModbusExceptionError,
    build_read,
    build_write_single,
    crc16,
    parse_read_response,
    parse_write_response,
    to_signed,
    words_to_ascii,
)

# --- captured requests (cloud -> collector) --------------------------------
REQ_TYPE = bytes.fromhex("010300ab0001f5ea")  # register 171, count 1
REQ_SERIAL = bytes.fromhex("010300ba000c642a")  # register 186, count 12
REQ_AC = bytes.fromhex("010300c9000fd5f0")  # register 201, count 15
REQ_SETTINGS = bytes.fromhex("0103014000084424")  # register 320, count 8

# --- captured responses (collector -> cloud) -------------------------------
RESP_TYPE = bytes.fromhex("0103027803da45")
RESP_SERIAL = bytes.fromhex("0103183939343332363034313037313036000000000000000000009cfb")
RESP_AC = bytes.fromhex("01031e000408ec1387000008fa00001387fffe000008fa00021386fffe002d0000e779")

# --- captured AT line, for the demultiplexer -------------------------------
AT_HTBT = bytes.fromhex("41542b485442543f0d0a")  # AT+HTBT?


class TestCrc:
    def test_every_captured_request_validates(self):
        for frame in (REQ_TYPE, REQ_SERIAL, REQ_AC, REQ_SETTINGS):
            assert crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")

    def test_every_captured_response_validates(self):
        for frame in (RESP_TYPE, RESP_SERIAL, RESP_AC):
            assert crc16(frame[:-2]) == int.from_bytes(frame[-2:], "little")

    def test_a_flipped_bit_breaks_the_crc(self):
        corrupted = bytearray(RESP_AC)
        corrupted[5] ^= 0x01
        assert crc16(bytes(corrupted[:-2])) != int.from_bytes(RESP_AC[-2:], "little")


class TestBuildRead:
    def test_reproduces_the_captured_identity_request(self):
        assert build_read(slave=1, address=0x00AB, count=1) == REQ_TYPE

    def test_reproduces_the_captured_serial_request(self):
        assert build_read(slave=1, address=0x00BA, count=12) == REQ_SERIAL

    def test_reproduces_the_captured_telemetry_request(self):
        assert build_read(slave=1, address=0x00C9, count=15) == REQ_AC

    def test_reproduces_the_captured_settings_request(self):
        assert build_read(slave=1, address=0x0140, count=8) == REQ_SETTINGS

    def test_rejects_a_count_above_the_modbus_limit(self):
        # FC3 caps at 125 registers; the collector's own cap is lower still.
        with pytest.raises(ModbusError):
            build_read(slave=1, address=0x0064, count=126)

    def test_rejects_a_zero_count(self):
        with pytest.raises(ModbusError):
            build_read(slave=1, address=0x0064, count=0)

    def test_rejects_an_address_outside_uint16(self):
        with pytest.raises(ModbusError):
            build_read(slave=1, address=0x10000, count=1)


class TestParseResponse:
    def test_reads_the_platform_code(self):
        # Register 171 -- the value that decides which register map applies.
        assert parse_read_response(RESP_TYPE) == [0x7803]

    def test_reads_the_full_telemetry_block(self):
        words = parse_read_response(RESP_AC)
        assert len(words) == 15
        # Offsets from register 201. Named per the EASUN SMG II map, which our
        # hardware confirmed.
        assert words[0] == 4  # 201 operation mode id
        assert words[1] == 2284  # 202 ac voltage -> 228.4 V
        assert words[2] == 4999  # 203 ac frequency -> 49.99 Hz
        assert words[4] == 2298  # 205 inverter voltage -> 229.8 V
        assert words[9] == 2298  # 210 output voltage
        assert words[11] == 4998  # 212 output frequency -> 49.98 Hz

    def test_leaves_words_unsigned(self):
        # Signedness is a property of the FIELD, not the frame. The codec must
        # not guess -- register 213 is signed, register 202 is not, and both
        # arrive as raw uint16 here.
        assert parse_read_response(RESP_AC)[12] == 0xFFFE

    def test_rejects_a_frame_whose_crc_is_wrong(self):
        corrupted = bytearray(RESP_AC)
        corrupted[5] ^= 0x01
        with pytest.raises(ModbusError):
            parse_read_response(bytes(corrupted))

    def test_rejects_a_truncated_frame(self):
        with pytest.raises(ModbusError):
            parse_read_response(RESP_AC[:10])

    def test_rejects_a_byte_count_that_disagrees_with_the_frame(self):
        """The CRC is RECOMPUTED, so the frame is internally consistent.

        Without that, `_check_crc` fires first and this test proves nothing
        about byte-count handling -- mutation testing found exactly that: the
        length check could be deleted and this test still passed.
        """
        pdu = bytearray(RESP_TYPE[:-2])
        pdu[2] = 40  # claims 40 data bytes in a 7-byte frame
        forged = bytes(pdu) + crc16(bytes(pdu)).to_bytes(2, "little")
        with pytest.raises(ModbusError):
            parse_read_response(forged)

    def test_raises_a_typed_error_for_an_exception_response(self):
        # SYNTHETIC: exception frames were observed during the register sweep
        # but not logged. Built to spec -- function code | 0x80, then the code.
        pdu = bytes([0x01, 0x83, 0x02])
        frame = pdu + crc16(pdu).to_bytes(2, "little")
        with pytest.raises(ModbusExceptionError) as err:
            parse_read_response(frame)
        assert err.value.code == 0x02  # illegal data address


class TestHelpers:
    def test_decodes_the_captured_serial_number(self):
        words = parse_read_response(RESP_SERIAL)
        assert words_to_ascii(words) == "99432604107106"

    def test_converts_a_negative_word(self):
        assert to_signed(0xFFFE) == -2

    def test_leaves_a_positive_word_alone(self):
        assert to_signed(2284) == 2284


class TestDemux:
    """One socket, two protocols, no delimiters on the Modbus half."""

    def test_takes_a_captured_at_line(self):
        frame, rest = take_frame(AT_HTBT, Direction.RESPONSE)
        assert frame == Frame(kind=FrameKind.AT, raw=AT_HTBT)
        assert rest == b""

    def test_takes_a_captured_modbus_response(self):
        frame, rest = take_frame(RESP_AC, Direction.RESPONSE)
        assert frame == Frame(kind=FrameKind.MODBUS, raw=RESP_AC)
        assert rest == b""

    def test_derives_response_length_from_the_byte_count(self):
        # Two responses of different lengths, back to back in one buffer. Only
        # the byte-count field can tell where the first one ends.
        buf = RESP_TYPE + RESP_AC
        frames, rest = split_frames(buf, Direction.RESPONSE)
        assert [f.raw for f in frames] == [RESP_TYPE, RESP_AC]
        assert rest == b""

    def test_derives_request_length_from_the_function_code(self):
        buf = REQ_TYPE + REQ_AC + REQ_SETTINGS
        frames, rest = split_frames(buf, Direction.REQUEST)
        assert [f.raw for f in frames] == [REQ_TYPE, REQ_AC, REQ_SETTINGS]
        assert rest == b""

    def test_separates_an_at_line_from_a_modbus_frame_in_one_buffer(self):
        # This is the case the whole module exists for.
        frames, rest = split_frames(AT_HTBT + RESP_TYPE, Direction.RESPONSE)
        assert [f.kind for f in frames] == [FrameKind.AT, FrameKind.MODBUS]
        assert rest == b""

    def test_returns_a_partial_modbus_frame_as_remainder(self):
        frame, rest = take_frame(RESP_AC[:20], Direction.RESPONSE)
        assert frame is None
        assert rest == RESP_AC[:20]

    def test_returns_a_partial_at_line_as_remainder(self):
        frame, rest = take_frame(AT_HTBT[:5], Direction.RESPONSE)
        assert frame is None
        assert rest == AT_HTBT[:5]

    def test_resumes_across_a_split_that_lands_mid_frame(self):
        # TCP may deliver a frame in any two pieces. Feeding the halves in turn
        # must yield exactly the same frame.
        buf = RESP_TYPE + RESP_AC
        for cut in (1, 4, 7, 12, 30):
            first, rest = split_frames(buf[:cut], Direction.RESPONSE)
            second, tail = split_frames(rest + buf[cut:], Direction.RESPONSE)
            assert [f.raw for f in first + second] == [RESP_TYPE, RESP_AC]
            assert tail == b""

    def test_raises_on_a_corrupt_modbus_frame(self):
        # A bad CRC means the stream is desynchronised. There is no transaction
        # id to resynchronise on, so this must be loud.
        corrupted = bytearray(RESP_TYPE)
        corrupted[3] ^= 0xFF
        with pytest.raises(ModbusError):
            take_frame(bytes(corrupted), Direction.RESPONSE)

    def test_raises_on_an_unknown_function_code(self):
        pdu = bytes([0x01, 0x63, 0x00, 0x00])
        frame = pdu + crc16(pdu).to_bytes(2, "little")
        with pytest.raises(ModbusError):
            take_frame(frame, Direction.RESPONSE)

    def test_takes_an_exception_response_as_a_five_byte_frame(self):
        pdu = bytes([0x01, 0x83, 0x02])
        exc = pdu + crc16(pdu).to_bytes(2, "little")
        frame, rest = take_frame(exc + RESP_TYPE, Direction.RESPONSE)
        assert frame is not None
        assert frame.raw == exc
        assert rest == RESP_TYPE

    def test_empty_buffer_yields_nothing(self):
        frame, rest = take_frame(b"", Direction.RESPONSE)
        assert frame is None
        assert rest == b""


class TestErrorsAreDiagnosable:
    """A framing error naming only the code is a dead end in the field.

    The same message covers a genuinely unsupported function AND a stream
    that has desynchronised, and those need opposite fixes. Seen live on
    2026-08-21: "unknown response function code: 0x17", sixteen times
    overnight, with no way to tell which it was.
    """

    def test_an_unknown_function_code_reports_the_bytes(self):
        frame = bytes.fromhex("011700112233445566")
        with pytest.raises(ModbusError) as err:
            take_frame(frame, Direction.RESPONSE)
        assert "0x17" in str(err.value)
        assert "011700112233" in str(err.value)

    def test_a_crc_mismatch_reports_the_bytes(self):
        corrupted = bytearray(RESP_TYPE)
        corrupted[3] ^= 0xFF
        with pytest.raises(ModbusError) as err:
            take_frame(bytes(corrupted), Direction.RESPONSE)
        assert "CRC mismatch" in str(err.value)
        assert corrupted.hex()[:8] in str(err.value)

    def test_the_context_is_bounded(self):
        # A long buffer must not dump a screenful into the log.
        frame = bytes([0x01, 0x17]) + bytes(500)
        with pytest.raises(ModbusError) as err:
            take_frame(frame, Direction.RESPONSE)
        message = str(err.value)
        assert "..." in message
        assert "502 buffered" in message
        assert len(message) < 320

    def test_a_whole_foreign_frame_survives_the_bound(self):
        """The bound existed to cap the log; it also truncated the evidence.

        A collector that opens a connection and sends something we do not
        recognise gives us exactly one look at it. The first capture in the
        field was a 36-byte frame cut off at 24, which left the trailer -- the
        part that identifies the framing -- unread.
        """
        foreign = bytes.fromhex("a5170010450000010000000200") + bytes(22) + bytes([0x15])
        assert len(foreign) == 36
        with pytest.raises(ModbusError) as err:
            take_frame(foreign, Direction.RESPONSE)
        message = str(err.value)
        assert foreign.hex() in message, "the whole frame must reach the log"
        assert "..." not in message


# ── write-single codec ────────────────────────────────────────────────────
#
# Function code 6. The collector tunnels bare RTU with no transaction id, so a
# write correlates by ORDER like every other frame, and its echo is the only
# acknowledgement the wire carries.
#
# No write has ever been confirmed against this hardware. These tests prove the
# CODEC, not the capability.


def test_build_write_single_frames_what_the_device_expects():
    # Register 303 (buzzer mode) = 3.
    frame = build_write_single(slave=1, address=303, value=3)
    assert frame[:6] == bytes([0x01, 0x06, 0x01, 0x2F, 0x00, 0x03])
    assert len(frame) == 8
    crc = crc16(frame[:6])
    assert frame[6] == crc & 0xFF
    assert frame[7] == (crc >> 8) & 0xFF


def test_build_write_single_accepts_the_full_uint16_range():
    assert len(build_write_single(slave=1, address=320, value=0)) == 8
    assert len(build_write_single(slave=1, address=320, value=65535)) == 8


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slave": 1, "address": 320, "value": 65536},
        {"slave": 1, "address": 320, "value": -1},
        {"slave": 1, "address": 0x10000, "value": 1},
        {"slave": 256, "address": 1, "value": 1},
    ],
)
def test_build_write_single_refuses_out_of_range_rather_than_truncating(kwargs):
    # A truncated address writes a DIFFERENT register and a truncated value a
    # different setpoint, both silently. On this device a setpoint is a battery
    # charge voltage.
    with pytest.raises(ModbusError):
        build_write_single(**kwargs)


def test_parse_write_response_returns_the_echo():
    echo = build_write_single(slave=1, address=303, value=3)
    address, value = parse_write_response(echo)
    assert (address, value) == (303, 3)


def test_parse_write_response_rejects_a_bad_crc():
    echo = bytearray(build_write_single(slave=1, address=303, value=3))
    echo[7] ^= 0xFF
    with pytest.raises(ModbusError):
        parse_write_response(bytes(echo))


def test_parse_write_response_surfaces_a_modbus_exception():
    # 0x86 = FC 6 with the exception bit, 0x02 = illegal data address. Exactly
    # what a read-only register answers, so it must be distinguishable from a
    # broken frame.
    body = bytes([0x01, 0x86, 0x02])
    crc = crc16(body)
    frame = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    with pytest.raises(ModbusExceptionError):
        parse_write_response(frame)


def test_parse_write_response_rejects_a_read_response():
    with pytest.raises(ModbusError):
        parse_write_response(build_read(slave=1, address=303, count=1))
