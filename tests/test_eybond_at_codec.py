"""AT-text codec for the Anenji/SmartESS collector.

Every fixture in this file is a VERBATIM frame captured from real hardware on
2026-08-20 -- collector I20000282044487591, firmware 8.50.18.3, AT version 1.14.
The hex is kept beside each one so a future reader can verify it against the
capture rather than trusting the decoded form.

This matters more than usual here. The previous implementation of this protocol
was written against a framing transcribed from a third-party project, every test
asserted the codec against itself, and the whole thing was wrong. Fixtures come
off the wire now.
"""

import pytest

from custom_components.svitgrid.eybond_at.at_codec import (
    AtProtocolError,
    AtResponse,
    build_query,
    build_write,
    is_at_line,
    parse_response,
)

# --- captured frames -------------------------------------------------------
# cloud -> collector
CAP_QUERY_DTUPN = bytes.fromhex("41542b445455504e3f0d0a")
CAP_WRITE_SYST = bytes.fromhex("41542b535953543d32303236303832303132323430350d0a")
# collector -> cloud
CAP_RESP_DTUPN = bytes.fromhex("41542b445455504e3a4932303030303238323034343438373539310d0a")
CAP_RESP_ATVER = bytes.fromhex("41542b41545645523a312e31340d0a")
CAP_RESP_SYST_W000 = bytes.fromhex("41542b535953543a573030300d0a")
CAP_RESP_LEDCTR_W001 = bytes.fromhex("41542b4c45444354523a573030310d0a")
CAP_RESP_HTBT_EMPTY = bytes.fromhex("41542b485442543a0d0a")
CAP_RESP_UART = b"AT+UART:9600,8,1,NONE\r\n"
CAP_RESP_CLDSRVHOST1 = b"AT+CLDSRVHOST1:dtu_ess.eybond.com,18899,TCP\r\n"
CAP_RESP_WFSS_NEGATIVE = b"AT+WFSS:-49\r\n"
CAP_RESP_DEVLIST_R001 = b"AT+DEVLIST:R001\r\n"
# a bare Modbus RTU frame off the same socket -- must NOT parse as AT
CAP_MODBUS_RTU = bytes.fromhex("010302eb64f75f")


class TestBuild:
    def test_query_matches_the_captured_cloud_frame(self):
        assert build_query("DTUPN") == CAP_QUERY_DTUPN

    def test_write_matches_the_captured_cloud_frame(self):
        assert build_write("SYST", "20260820122405") == CAP_WRITE_SYST

    def test_query_rejects_a_command_that_is_not_ascii(self):
        with pytest.raises(AtProtocolError):
            build_query("SYSTÉ")

    def test_query_rejects_an_empty_command(self):
        with pytest.raises(AtProtocolError):
            build_query("")

    def test_write_rejects_a_value_containing_the_terminator(self):
        # A value carrying CRLF would split into two frames on the wire and
        # desynchronise every later response.
        with pytest.raises(AtProtocolError):
            build_write("SYST", "2026\r\nAT+EVIL?")


class TestParse:
    def test_parses_the_collector_part_number(self):
        r = parse_response(CAP_RESP_DTUPN)
        assert r == AtResponse(command="DTUPN", value="I20000282044487591")

    def test_parses_a_dotted_version(self):
        assert parse_response(CAP_RESP_ATVER).value == "1.14"

    def test_parses_an_empty_value(self):
        # AT+HTBT: is the heartbeat reply. Empty is legal, not a parse failure.
        r = parse_response(CAP_RESP_HTBT_EMPTY)
        assert r.command == "HTBT"
        assert r.value == ""

    def test_keeps_commas_in_a_multi_field_value(self):
        assert parse_response(CAP_RESP_UART).value == "9600,8,1,NONE"

    def test_keeps_the_full_host_triple(self):
        assert parse_response(CAP_RESP_CLDSRVHOST1).value == "dtu_ess.eybond.com,18899,TCP"

    def test_splits_on_the_first_colon_only(self):
        """SYNTHETIC frame -- no captured value contains a second colon.

        Kept, and labelled, because the grammar makes the case reachable:
        `_FORBIDDEN_IN_COMMAND` bars a colon from the command, and nothing bars
        one from a value. A greedy split would silently truncate such a value
        from the left.

        This test exists because mutation testing showed the captured
        `CLDSRVHOST1` fixture cannot tell `partition` from `rpartition` -- it
        carries only one colon, so the assertion above was vacuous for this
        property.
        """
        r = parse_response(b"AT+CLDSRVHOST1:http://dtu.example.com:18899\r\n")
        assert r.command == "CLDSRVHOST1"
        assert r.value == "http://dtu.example.com:18899"

    def test_keeps_a_negative_value(self):
        assert parse_response(CAP_RESP_WFSS_NEGATIVE).value == "-49"

    def test_rejects_a_frame_with_no_terminator(self):
        with pytest.raises(AtProtocolError):
            parse_response(b"AT+DTUPN:I2000")

    def test_rejects_a_frame_with_no_at_prefix(self):
        with pytest.raises(AtProtocolError):
            parse_response(b"DTUPN:I2000\r\n")

    def test_rejects_a_modbus_frame(self):
        with pytest.raises(AtProtocolError):
            parse_response(CAP_MODBUS_RTU)


class TestReturnCodes:
    def test_w000_is_a_successful_write(self):
        r = parse_response(CAP_RESP_SYST_W000)
        assert r.is_write_ack is True
        assert r.is_unsupported is False

    def test_w001_is_also_a_write_ack(self):
        # Captured from AT+LEDCTR=COM,ON. Both W000 and W001 follow a write.
        assert parse_response(CAP_RESP_LEDCTR_W001).is_write_ack is True

    def test_r001_marks_an_unsupported_query(self):
        r = parse_response(CAP_RESP_DEVLIST_R001)
        assert r.is_unsupported is True
        assert r.is_write_ack is False

    def test_a_data_value_is_neither(self):
        r = parse_response(CAP_RESP_DTUPN)
        assert r.is_unsupported is False
        assert r.is_write_ack is False


class TestFrameClassification:
    """The collector multiplexes AT lines and bare Modbus RTU on ONE socket.

    Telling them apart is the demultiplexer's whole job, so the predicate has
    to be exact rather than approximate.
    """

    def test_recognises_a_captured_at_line(self):
        assert is_at_line(CAP_RESP_DTUPN) is True

    def test_recognises_a_captured_query(self):
        assert is_at_line(CAP_QUERY_DTUPN) is True

    def test_rejects_a_captured_modbus_frame(self):
        assert is_at_line(CAP_MODBUS_RTU) is False

    def test_rejects_a_modbus_frame_that_happens_to_start_with_at_bytes(self):
        # 0x41 0x54 0x2b is "AT+". A Modbus response whose payload begins with
        # those bytes must not be mistaken for an AT line -- the terminator is
        # what decides.
        assert is_at_line(bytes.fromhex("010341542b0000")) is False

    def test_rejects_an_empty_frame(self):
        assert is_at_line(b"") is False
