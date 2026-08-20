"""Three Anenji on one LAN: one listener, many sessions, routed by identity.

The headline test is `test_a_read_never_returns_another_collectors_data`. With
no transaction id in this protocol, a mis-routed response is undetectable
downstream — it would simply publish collector B's voltage as collector A's.
Sharing one scheduler across collectors is exactly how that happens, which is
why session state is per connection.
"""

import asyncio
import contextlib

import pytest

from custom_components.svitgrid.eybond_at.demux import Direction, split_frames
from custom_components.svitgrid.eybond_at.hub import EybondAtHub, HubConfig
from custom_components.svitgrid.eybond_at.modbus_rtu import crc16
from custom_components.svitgrid.eybond_at.reader import EybondAtReader

pytestmark = pytest.mark.usefixtures("socket_enabled")


def ascii_words(text: str, registers: int) -> dict[int, int]:
    raw = text.encode("ascii").ljust(registers * 2, b"\x00")
    return {i: int.from_bytes(raw[2 * i : 2 * i + 2], "big") for i in range(registers)}


def registers_for(serial: str, grid_dv: int) -> dict[int, int]:
    """A register table for one inverter. `grid_dv` is grid voltage in 0.1 V."""
    table = {171: 0x7803, 184: 11, 201: 0x0004, 202: grid_dv, 203: 0x1387}
    for offset, word in ascii_words(serial, 12).items():
        table[186 + offset] = word
    for offset, word in ascii_words("7803_A6260126v1", 8).items():
        table[626 + offset] = word
    return table


class FakeAnenji:
    """A Modbus slave over TCP, answering from its own register table."""

    def __init__(self, registers: dict[int, int], slave: int = 1):
        self.registers = registers
        self.slave = slave
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


async def wait_for(predicate, limit_s=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def announce_listener():
    """A UDP socket that records announces, plus its port."""
    received: list[bytes] = []

    class Receiver(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            received.append(data)

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
    return received, transport, transport.get_extra_info("sockname")[1]


def make_hub(**overrides) -> EybondAtHub:
    defaults = {
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "announce_target": "127.0.0.1",
        "announce_interval_s": 0.05,
        "tick_interval_s": 0.02,
    }
    return EybondAtHub(HubConfig(**{**defaults, **overrides}), ip_provider=lambda: "127.0.0.1")


# Three inverters, distinguishable by BOTH serial and grid voltage.
FLEET = {
    "11111111111111": 2200,  # 220.0 V
    "22222222222222": 2300,  # 230.0 V
    "33333333333333": 2400,  # 240.0 V
}


class TestThreeCollectors:
    async def test_one_listener_serves_all_three(self):
        """One port per inverter does not work: the 2nd and 3rd fail to bind
        with `address already in use`, and three broadcasters each tell every
        collector a different port."""
        hub = make_hub()
        await hub.start()
        units = [FakeAnenji(registers_for(s, v)) for s, v in FLEET.items()]
        try:
            for unit in units:
                await unit.connect(hub.listen_port)
            assert await wait_for(lambda: hub.collector_count == 3)
        finally:
            for unit in units:
                await unit.close()
            await hub.stop()

    async def test_identifies_every_collector_by_its_own_serial(self):
        hub = make_hub()
        await hub.start()
        units = [FakeAnenji(registers_for(s, v)) for s, v in FLEET.items()]
        try:
            for unit in units:
                await unit.connect(hub.listen_port)
            assert await wait_for(lambda: len([s for s in hub.sessions if s.serial]) == 3)
            assert {s.serial for s in hub.sessions} == set(FLEET)
        finally:
            for unit in units:
                await unit.close()
            await hub.stop()

    async def test_a_read_never_returns_another_collectors_data(self):
        """The hazard this whole design exists to prevent.

        Each inverter reports a distinct grid voltage. If sessions shared a
        scheduler, a read on one would sometimes resolve with another's
        response — and with no transaction id, nothing downstream could tell.
        """
        hub = make_hub()
        await hub.start()
        units = [FakeAnenji(registers_for(s, v)) for s, v in FLEET.items()]
        try:
            for unit in units:
                await unit.connect(hub.listen_port)
            assert await wait_for(lambda: len([s for s in hub.sessions if s.serial]) == 3)
            # Read all three CONCURRENTLY -- the interleaving is the point.
            results = await asyncio.gather(
                *[EybondAtReader(hub.session_for(serial)).read() for serial in FLEET]
            )
            for reading, (serial, grid_dv) in zip(results, FLEET.items(), strict=True):
                assert reading.identity.serial == serial
                assert reading.values["gridVoltageL1"] == pytest.approx(grid_dv / 10)
        finally:
            for unit in units:
                await unit.close()
            await hub.stop()

    async def test_repeated_concurrent_reads_stay_attributed(self):
        # One pass could pass by luck. Five rounds of three concurrent reads
        # is where a shared-state bug shows up.
        hub = make_hub()
        await hub.start()
        units = [FakeAnenji(registers_for(s, v)) for s, v in FLEET.items()]
        try:
            for unit in units:
                await unit.connect(hub.listen_port)
            assert await wait_for(lambda: len([s for s in hub.sessions if s.serial]) == 3)
            readers = {s: EybondAtReader(hub.session_for(s)) for s in FLEET}
            for _ in range(5):
                results = await asyncio.gather(*[r.read() for r in readers.values()])
                for reading, (serial, grid_dv) in zip(results, FLEET.items(), strict=True):
                    assert reading.identity.serial == serial
                    assert reading.values["gridVoltageL1"] == pytest.approx(grid_dv / 10)
        finally:
            for unit in units:
                await unit.close()
            await hub.stop()

    async def test_one_collector_leaving_does_not_disturb_the_others(self):
        hub = make_hub()
        await hub.start()
        serials = list(FLEET)
        units = {s: FakeAnenji(registers_for(s, FLEET[s])) for s in serials}
        try:
            for unit in units.values():
                await unit.connect(hub.listen_port)
            assert await wait_for(lambda: len([s for s in hub.sessions if s.serial]) == 3)
            await units[serials[1]].close()
            assert await wait_for(lambda: hub.collector_count == 2)
            # The survivors still read their OWN data.
            for serial in (serials[0], serials[2]):
                reading = await EybondAtReader(hub.session_for(serial)).read()
                assert reading.identity.serial == serial
                assert reading.values["gridVoltageL1"] == pytest.approx(FLEET[serial] / 10)
        finally:
            for unit in units.values():
                await unit.close()
            await hub.stop()

    async def test_an_unconfigured_collector_is_reported_not_hidden(self):
        """This is what a pairing flow offers the user to choose from.

        Silently ignoring an unknown collector would leave a user staring at a
        device that is plainly connected and plainly not working.
        """
        hub = make_hub()
        await hub.start()
        unit = FakeAnenji(registers_for("99999999999999", 2350))
        try:
            await unit.connect(hub.listen_port)
            assert await wait_for(lambda: len(hub.unclaimed()) == 1)
            assert hub.unclaimed()[0].serial == "99999999999999"
            # And a serial nobody reports routes nowhere.
            assert hub.session_for("does-not-exist") is None
            assert hub.session_for(None) is None
        finally:
            await unit.close()
            await hub.stop()

    async def test_refuses_more_collectors_than_configured(self):
        hub = make_hub(max_sessions=2)
        await hub.start()
        units = [FakeAnenji(registers_for(s, v)) for s, v in FLEET.items()]
        try:
            for unit in units:
                await unit.connect(hub.listen_port)
                await asyncio.sleep(0.05)
            assert await wait_for(lambda: hub.collector_count == 2)
            await asyncio.sleep(0.2)
            assert hub.collector_count == 2
        finally:
            for unit in units:
                await unit.close()
            await hub.stop()

    async def test_goes_quiet_once_every_expected_collector_is_connected(self):
        """MEASURED on hardware: a connected collector that receives
        `set>server=` REDIALS. Announcing every 3 s produced 18 reconnects in
        45 seconds; announcing only while someone is missing produced one.
        """
        received, transport, port = await announce_listener()
        hub = make_hub(announce_port=port, expected_collectors=1)
        await hub.start()
        unit = FakeAnenji(registers_for("11111111111111", 2200))
        try:
            assert await wait_for(lambda: len(received) >= 1)  # looking for it
            await unit.connect(hub.listen_port)
            assert await wait_for(lambda: hub.collector_count == 1)
            settled = len(received)
            await asyncio.sleep(0.3)  # several announce intervals
            assert len(received) == settled, "announced at a connected collector"
        finally:
            await unit.close()
            await hub.stop()
            transport.close()

    async def test_keeps_looking_while_an_expected_collector_is_missing(self):
        # Going quiet after the first would leave the other two unable to
        # find us.
        received, transport, port = await announce_listener()
        hub = make_hub(announce_port=port, expected_collectors=3)
        await hub.start()
        unit = FakeAnenji(registers_for("11111111111111", 2200))
        try:
            await unit.connect(hub.listen_port)
            assert await wait_for(lambda: hub.collector_count == 1)
            settled = len(received)
            assert await wait_for(lambda: len(received) > settled, limit_s=2.0)
        finally:
            await unit.close()
            await hub.stop()
            transport.close()

    async def test_recalls_a_missing_collector_by_unicast_not_broadcast(self):
        """A dead inverter must not make the healthy ones churn.

        Once every address is known, a broadcast would reach the connected
        collectors too -- and make them redial. Unicast reaches only the one
        that is missing.
        """
        received, transport, port = await announce_listener()
        # announce_target is a DIFFERENT address, so a broadcast is
        # distinguishable from a unicast to the known collector.
        hub = make_hub(announce_port=port, announce_target="127.0.0.2", expected_collectors=1)
        await hub.start()
        unit = FakeAnenji(registers_for("11111111111111", 2200))
        try:
            await unit.connect(hub.listen_port)
            assert await wait_for(lambda: hub.collector_count == 1)
            await unit.close()
            assert await wait_for(lambda: hub.collector_count == 0)
            received.clear()
            # Now it is missing and its address is known: unicast to 127.0.0.1,
            # which our listener sees. A broadcast would go to 127.0.0.2.
            assert await wait_for(lambda: len(received) >= 1, limit_s=2.0)
        finally:
            await hub.stop()
            transport.close()


class TestUpstreamIsolation:
    async def test_a_dead_cloud_takes_down_none_of_the_three(self):
        """With three collectors this matters more, not less.

        One unreachable vendor cloud must not take the other two inverters
        down with it.
        """
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()

        hub = make_hub(
            upstream_host="127.0.0.1",
            upstream_port=dead_port,
            upstream_backoff_initial_s=0.05,
            upstream_backoff_max_s=0.1,
        )
        await hub.start()
        units = [FakeAnenji(registers_for(s, v)) for s, v in FLEET.items()]
        try:
            for unit in units:
                await unit.connect(hub.listen_port)
            assert await wait_for(lambda: len([s for s in hub.sessions if s.serial]) == 3)
            await asyncio.sleep(0.4)  # several failed upstream attempts
            assert hub.collector_count == 3
            for serial in FLEET:
                reading = await EybondAtReader(hub.session_for(serial)).read()
                assert reading.identity.serial == serial
        finally:
            for unit in units:
                await unit.close()
            await hub.stop()


class TestChangeSignal:
    """A harvest loop with nothing to read waits on this instead of polling."""

    async def test_fires_when_a_collector_is_identified(self):
        hub = make_hub()
        await hub.start()
        unit = FakeAnenji(registers_for("11111111111111", 2200))
        try:
            waiter = asyncio.create_task(hub.wait_for_change(5.0))
            await asyncio.sleep(0.02)
            await unit.connect(hub.listen_port)
            assert await waiter is True
        finally:
            await unit.close()
            await hub.stop()

    async def test_fires_when_a_collector_is_lost(self):
        hub = make_hub()
        await hub.start()
        unit = FakeAnenji(registers_for("11111111111111", 2200))
        try:
            await unit.connect(hub.listen_port)
            assert await wait_for(lambda: hub.collector_count == 1)
            waiter = asyncio.create_task(hub.wait_for_change(5.0))
            await asyncio.sleep(0.02)
            await unit.close()
            assert await waiter is True
        finally:
            await hub.stop()

    async def test_returns_false_when_nothing_happens(self):
        # Bounds the case where no collector ever arrives.
        hub = make_hub()
        await hub.start()
        try:
            assert await hub.wait_for_change(0.05) is False
        finally:
            await hub.stop()
