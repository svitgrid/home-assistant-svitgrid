"""Poll cycle: identify once, then read the map's blocks and decode them."""

import pytest

from custom_components.svitgrid.eybond_at.identity import (
    REG_DEVICE_TYPE,
    REG_FIRMWARE,
    REG_PROTOCOL,
    REG_SERIAL,
    UnknownPlatform,
)
from custom_components.svitgrid.eybond_at.link import TransactionFailed
from custom_components.svitgrid.eybond_at.reader import EybondAtReader
from custom_components.svitgrid.eybond_at.register_map import Confidence

# Registers 201..229, verbatim from the 2026-08-20 capture where they overlap,
# zero-filled where the bench unit has no hardware attached.
TELEMETRY = [
    0x0004, 0x08EC, 0x1387, 0x0000, 0x08FA, 0x0000, 0x1387, 0xFFFE,
    0x0000, 0x08FA, 0x0002, 0x1386, 0xFFFE, 0x002D, 0x0000, 0x0000,
    0x0000, 0x0112, 0x0000, 0x0000, 0x0000, 0x0001, 0x0000, 0x0001,
    0x001B, 0x001B, 0x001B, 0x0005, 0x0000,
]  # fmt: skip
SERIAL_WORDS = [0x3939, 0x3433, 0x3236, 0x3034, 0x3130, 0x3731, 0x3036] + [0] * 5
FIRMWARE_WORDS = [0x3738, 0x3033, 0x5F41, 0x3632, 0x3630, 0x3132, 0x3676, 0x3100]


class FakeLink:
    def __init__(self, protocol: int = 11):
        self.protocol = protocol
        self.reads: list[tuple[int, int]] = []
        self.fail_at: set[int] = set()

    async def read_registers(self, address: int, count: int, timeout_s: float = 5.0):
        self.reads.append((address, count))
        if address in self.fail_at:
            raise TransactionFailed(f"synthetic failure at {address}")
        if address == REG_DEVICE_TYPE:
            return [0x7803]
        if address == REG_PROTOCOL:
            return [self.protocol]
        if address == REG_SERIAL:
            return SERIAL_WORDS[:count]
        if address == REG_FIRMWARE:
            return FIRMWARE_WORDS[:count]
        if 201 <= address <= 229:
            offset = address - 201
            return TELEMETRY[offset : offset + count]
        raise TransactionFailed(f"unexpected read at {address}")


class TestReading:
    async def test_decodes_the_confirmed_ac_fields(self):
        reader = EybondAtReader(FakeLink())
        reading = await reader.read()
        assert reading.values["gridVoltageL1"] == pytest.approx(228.4)
        assert reading.values["gridFrequency"] == pytest.approx(49.99)
        assert reading.values["loadVoltageL1"] == pytest.approx(229.8)
        assert reading.values["inverterTemperature"] == 27

    async def test_carries_the_device_identity(self):
        reading = await EybondAtReader(FakeLink()).read()
        assert reading.identity.serial == "99432604107106"
        assert reading.identity.protocol_number == 11

    async def test_reports_which_fields_are_only_identified(self):
        # The caller must be able to tell a measured value from an unexercised
        # one without consulting a document.
        reading = await EybondAtReader(FakeLink()).read()
        assert reading.confidence["gridVoltageL1"] is Confidence.CONFIRMED
        assert reading.confidence["batterySoc"] is Confidence.IDENTIFIED

    async def test_a_reading_with_every_block_read_is_complete(self):
        reading = await EybondAtReader(FakeLink()).read()
        assert reading.complete is True
        assert reading.missing_blocks == ()


class TestIdentityCaching:
    async def test_identifies_once_across_several_polls(self):
        # Four identity reads at 9600 baud cost about a second. Repeating them
        # every poll would halve the useful poll rate.
        link = FakeLink()
        reader = EybondAtReader(link)
        await reader.read()
        identity_reads = sum(1 for a, _ in link.reads if a == REG_PROTOCOL)
        await reader.read()
        await reader.read()
        assert sum(1 for a, _ in link.reads if a == REG_PROTOCOL) == identity_reads

    async def test_reidentifies_after_invalidate(self):
        # The collector may reconnect as a DIFFERENT device -- a customer can
        # swap an inverter without telling us.
        link = FakeLink()
        reader = EybondAtReader(link)
        await reader.read()
        reader.invalidate()
        await reader.read()
        assert sum(1 for a, _ in link.reads if a == REG_PROTOCOL) == 2


class TestRefusal:
    async def test_refuses_to_read_an_unknown_protocol(self):
        # Publishing nothing is recoverable; publishing a wrong map is not.
        reader = EybondAtReader(FakeLink(protocol=4))
        with pytest.raises(UnknownPlatform):
            await reader.read()

    async def test_does_not_poll_telemetry_when_the_platform_is_unknown(self):
        link = FakeLink(protocol=4)
        reader = EybondAtReader(link)
        with pytest.raises(UnknownPlatform):
            await reader.read()
        assert not any(201 <= a <= 229 for a, _ in link.reads)


class TestPartialReads:
    async def test_a_failed_block_marks_the_reading_incomplete(self):
        """A short reading must never look like a complete one.

        Absent fields and zero fields are very different things, and the
        difference is invisible downstream unless it is carried explicitly.
        """
        link = FakeLink()
        link.fail_at = {201}
        reader = EybondAtReader(link)
        await reader.read()  # identity succeeds and is cached
        reader.invalidate()
        reading = await reader.read()
        assert reading.complete is False
        assert reading.missing_blocks == ((201, 29),)
        assert "gridVoltageL1" not in reading.values

    async def test_a_failed_identity_read_propagates(self):
        link = FakeLink()
        link.fail_at = {REG_PROTOCOL}
        with pytest.raises(TransactionFailed):
            await EybondAtReader(link).read()
