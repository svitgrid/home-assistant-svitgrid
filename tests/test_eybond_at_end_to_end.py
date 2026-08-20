"""End-to-end: announce, dial in, identify, poll, decode -- over a real socket.

`FakeAnenji` is a Modbus slave whose register table holds the values actually
captured from the bench unit on 2026-08-20 (collector `I20000282044487591`,
inverter serial `99432604107106`, firmware `7803_A6260126v1`). It speaks the
same bare RTU the collector does, framed the same way.

This is the closest thing to the hardware that exists without the hardware,
and it exercises every layer at once: transport, demultiplexer, scheduler,
Modbus codec, identity, register map, reader.

It is NOT a substitute for pointing the add-on at the real unit. The
simulator answers exactly what we told it to, so it cannot discover anything
the capture did not already contain.
"""

import asyncio
import contextlib

import pytest

from custom_components.svitgrid.eybond_at.demux import Direction, split_frames
from custom_components.svitgrid.eybond_at.hub import EybondAtHub, HubConfig
from custom_components.svitgrid.eybond_at.identity import UnknownPlatform
from custom_components.svitgrid.eybond_at.modbus_rtu import crc16
from custom_components.svitgrid.eybond_at.reader import EybondAtReader
from custom_components.svitgrid.eybond_at.register_map import Confidence

pytestmark = pytest.mark.usefixtures("socket_enabled")

# Captured register values. Everything absent from this table reads 0, exactly
# as the bench unit does with no battery, no panels, and no load attached.
CAPTURED_REGISTERS: dict[int, int] = {
    171: 0x7803,  # device type
    184: 11,  # protocol number
    # 186..197 serial "99432604107106", ASCII two chars per register
    186: 0x3939, 187: 0x3433, 188: 0x3236, 189: 0x3034, 190: 0x3130,
    191: 0x3731, 192: 0x3036,
    # 201..229 telemetry
    201: 0x0004, 202: 0x08EC, 203: 0x1387, 205: 0x08FA, 207: 0x1387,
    208: 0xFFFE, 210: 0x08FA, 211: 0x0002, 212: 0x1386, 213: 0xFFFE,
    214: 0x002D, 219: 0x0112, 225: 0x0001, 226: 0x001B, 227: 0x001B,
    228: 0x001B, 229: 0x0005,
    # 626..633 firmware "7803_A6260126v1"
    626: 0x3738, 627: 0x3033, 628: 0x5F41, 629: 0x3632, 630: 0x3630,
    631: 0x3132, 632: 0x3676, 633: 0x3100,
}  # fmt: skip


class FakeAnenji:
    """A Modbus slave answering from the captured table, over TCP."""

    def __init__(self, registers: dict[int, int] | None = None, slave: int = 1):
        self.registers = dict(CAPTURED_REGISTERS if registers is None else registers)
        self.slave = slave
        self.requests: list[tuple[int, int]] = []
        self._task = None
        self._writer = None
        self._buf = b""

    async def connect(self, port: int) -> None:
        reader, self._writer = await asyncio.open_connection("127.0.0.1", port)
        self._task = asyncio.create_task(self._serve(reader))

    async def _serve(self, reader) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                self._buf += data
                frames, self._buf = split_frames(self._buf, Direction.REQUEST)
                for frame in frames:
                    await self._answer(frame.raw)
        except (asyncio.CancelledError, ConnectionError):
            return

    async def _answer(self, request: bytes) -> None:
        address = int.from_bytes(request[2:4], "big")
        count = int.from_bytes(request[4:6], "big")
        self.requests.append((address, count))
        words = [self.registers.get(address + i, 0) for i in range(count)]
        body = bytes([self.slave, 0x03, count * 2]) + b"".join(w.to_bytes(2, "big") for w in words)
        self._writer.write(body + crc16(body).to_bytes(2, "little"))
        await self._writer.drain()

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._writer:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()


async def wait_for(predicate, limit_s=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def connected_link(inverter: FakeAnenji) -> EybondAtHub:
    link = EybondAtHub(
        HubConfig(
            listen_host="127.0.0.1",
            listen_port=0,
            announce_target="127.0.0.1",
            announce_interval_s=0.05,
            tick_interval_s=0.02,
        ),
        ip_provider=lambda: "127.0.0.1",
    )
    await link.start()
    await inverter.connect(link.listen_port)
    assert await wait_for(lambda: link.collector_count == 1)
    return link


class TestEndToEnd:
    async def test_reads_a_full_reading_through_the_whole_stack(self):
        inverter = FakeAnenji()
        link = await connected_link(inverter)
        try:
            reading = await EybondAtReader(link.sessions[0]).read()

            assert reading.identity.protocol_number == 11
            assert reading.identity.device_type == 0x7803
            assert reading.identity.serial == "99432604107106"
            assert reading.identity.firmware == "7803_A6260126v1"

            assert reading.values["gridVoltageL1"] == pytest.approx(228.4)
            assert reading.values["gridFrequency"] == pytest.approx(49.99)
            assert reading.values["loadVoltageL1"] == pytest.approx(229.8)
            assert reading.values["loadPower"] == -2
            assert reading.values["inverterTemperature"] == 27
            assert reading.complete is True
        finally:
            await inverter.close()
            await link.stop()

    async def test_the_bench_units_absent_hardware_reads_zero_not_missing(self):
        """Zero here is the unit's honest answer, not a decode failure.

        The distinction matters: `complete` is True, so downstream knows these
        are real readings from a device with nothing attached, rather than
        blocks we failed to read.
        """
        inverter = FakeAnenji()
        link = await connected_link(inverter)
        try:
            reading = await EybondAtReader(link.sessions[0]).read()
            assert reading.values["batterySoc"] == 5
            assert reading.values["pv1Power"] == 0
            assert reading.complete is True
            assert reading.confidence["batterySoc"] is Confidence.IDENTIFIED
        finally:
            await inverter.close()
            await link.stop()

    async def test_polls_the_map_in_one_block(self):
        # 201..229 is 29 registers, inside the collector's read limit, so the
        # whole telemetry map costs ONE round trip at 9600 baud.
        inverter = FakeAnenji()
        link = await connected_link(inverter)
        try:
            await EybondAtReader(link.sessions[0]).read()
            telemetry = [r for r in inverter.requests if 201 <= r[0] <= 229]
            assert telemetry == [(201, 29)]
        finally:
            await inverter.close()
            await link.stop()

    async def test_refuses_a_device_reporting_an_unmeasured_protocol(self):
        registers = dict(CAPTURED_REGISTERS)
        registers[184] = 4  # a protocol we have never captured
        inverter = FakeAnenji(registers)
        link = await connected_link(inverter)
        try:
            with pytest.raises(UnknownPlatform):
                await EybondAtReader(link.sessions[0]).read()
            # And it cost exactly one identity pass, no telemetry.
            assert not any(201 <= a <= 229 for a, _ in inverter.requests)
        finally:
            await inverter.close()
            await link.stop()

    async def test_survives_a_reconnect_and_reidentifies(self):
        """A reconnect produces a NEW session, and the reader follows it.

        Carrying a reader across sessions would decode the new connection with
        the old one's cached identity -- and it may be a different inverter.
        """
        inverter = FakeAnenji()
        link = await connected_link(inverter)
        try:
            first = await EybondAtReader(link.sessions[0]).read()
            assert first.complete is True

            await inverter.close()
            assert await wait_for(lambda: link.collector_count == 0)

            second = FakeAnenji()
            await second.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_count == 1, limit_s=2.0)
            assert await wait_for(lambda: link.sessions[0].serial is not None)
            again = await EybondAtReader(link.sessions[0]).read()
            assert again.identity.serial == "99432604107106"
            assert again.values["gridVoltageL1"] == pytest.approx(228.4)
            await second.close()
        finally:
            await link.stop()
