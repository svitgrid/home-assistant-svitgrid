"""Asyncio transport for the EyBond/SmartESS collector.

Everything here runs on loopback with ephemeral ports. No hardware, no
broadcast, no vendor cloud.

The most important test in this file is
`test_a_dead_upstream_never_tears_down_the_collector_session`. That bug was
made for real on 2026-08-20 while investigating this protocol: a hand-rolled
proxy coupled the collector's session to the vendor cloud's liveness, so one
ordinary cloud disconnect became 19 collector reconnects in 43 seconds. It is
a regression test for a mistake, not a hypothetical.
"""

import asyncio
import contextlib

import pytest

from custom_components.svitgrid.eybond_at.link import (
    EybondAtLink,
    LinkConfig,
    TransactionFailed,
)
from custom_components.svitgrid.eybond_at.modbus_rtu import build_read, crc16

# The Home Assistant test harness blocks real sockets by default. This module
# is the transport layer, so it needs them -- on loopback, ephemeral ports only.
pytestmark = pytest.mark.usefixtures("socket_enabled")

RESP_TYPE = bytes.fromhex("0103027803da45")  # register 171 -> 0x7803
REQ_TYPE = build_read(slave=1, address=0x00AB, count=1)
AT_REPLY_DTUPN = b"AT+DTUPN:I20000282044487591\r\n"


async def wait_for(predicate, limit_s=3.0, interval=0.01):
    """Poll a condition instead of sleeping a guessed interval."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


class FakeCollector:
    """Dials the link and answers whatever it is asked, like the real one."""

    def __init__(self):
        self.reader = None
        self.writer = None
        self.received: list[bytes] = []
        self._task = None
        self.auto_reply: bytes | None = None

    async def connect(self, port: int):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        self._task = asyncio.create_task(self._pump())

    async def _pump(self):
        try:
            while True:
                data = await self.reader.read(4096)
                if not data:
                    return
                self.received.append(data)
                if self.auto_reply is not None:
                    self.writer.write(self.auto_reply)
                    await self.writer.drain()
        except (asyncio.CancelledError, ConnectionError):
            return

    async def send(self, data: bytes):
        self.writer.write(data)
        await self.writer.drain()

    async def close(self):
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.writer:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()


async def free_port() -> int:
    """Reserve and release a port, so connecting to it is refused."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


def make_link(**overrides) -> EybondAtLink:
    config = LinkConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        announce_target="127.0.0.1",
        announce_interval_s=0.05,
        tick_interval_s=0.02,
        **overrides,
    )
    return EybondAtLink(config, ip_provider=lambda: "127.0.0.1")


class TestLifecycle:
    async def test_start_binds_an_ephemeral_port(self):
        link = make_link()
        await link.start()
        try:
            assert link.listen_port and link.listen_port > 0
            assert link.collector_connected is False
        finally:
            await link.stop()

    async def test_stop_is_safe_without_a_collector(self):
        link = make_link()
        await link.start()
        await link.stop()

    async def test_accepts_a_collector_connection(self):
        link = make_link()
        await link.start()
        collector = FakeCollector()
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
        finally:
            await collector.close()
            await link.stop()

    async def test_closes_a_second_collector_connection(self):
        # The protocol allows exactly one session; a second would interleave
        # responses that cannot be attributed.
        link = make_link()
        await link.start()
        first, second = FakeCollector(), FakeCollector()
        try:
            await first.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            await second.connect(link.listen_port)
            assert await wait_for(lambda: second.reader.at_eof(), limit_s=2.0)
            assert link.collector_connected is True  # the first survives
        finally:
            await first.close()
            await second.close()
            await link.stop()


class TestAnnounce:
    async def test_announces_while_no_collector_is_connected(self):
        received: list[bytes] = []

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                received.append(data)

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        link = make_link(announce_port=port)
        await link.start()
        try:
            assert await wait_for(lambda: len(received) >= 2)
            expected = f"set>server=127.0.0.1:{link.listen_port};".encode()
            assert received[0] == expected
        finally:
            await link.stop()
            transport.close()

    async def test_stops_announcing_once_the_collector_connects(self):
        received: list[bytes] = []

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                received.append(data)

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        link = make_link(announce_port=port)
        await link.start()
        collector = FakeCollector()
        try:
            assert await wait_for(lambda: len(received) >= 1)
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            settled = len(received)
            await asyncio.sleep(0.2)  # several announce intervals
            assert len(received) == settled
        finally:
            await collector.close()
            await link.stop()
            transport.close()


class TestTransactions:
    async def test_reads_registers_end_to_end(self):
        link = make_link()
        await link.start()
        collector = FakeCollector()
        collector.auto_reply = RESP_TYPE
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            words = await link.read_registers(address=0x00AB, count=1, timeout_s=2.0)
            assert words == [0x7803]
            assert collector.received[0] == REQ_TYPE
        finally:
            await collector.close()
            await link.stop()

    async def test_an_at_query_round_trips(self):
        link = make_link()
        await link.start()
        collector = FakeCollector()
        collector.auto_reply = AT_REPLY_DTUPN
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            value = await link.at_query("DTUPN", timeout_s=2.0)
            assert value == "I20000282044487591"
        finally:
            await collector.close()
            await link.stop()

    async def test_a_read_fails_when_no_collector_is_connected(self):
        link = make_link()
        await link.start()
        try:
            with pytest.raises(TransactionFailed):
                await link.read_registers(address=0x00AB, count=1, timeout_s=0.2)
        finally:
            await link.stop()

    async def test_a_read_times_out_when_the_collector_stays_silent(self):
        link = make_link()
        await link.start()
        collector = FakeCollector()  # no auto_reply: never answers
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            with pytest.raises(TransactionFailed):
                await link.read_registers(address=0x00AB, count=1, timeout_s=0.3)
        finally:
            await collector.close()
            await link.stop()

    async def test_a_pending_read_fails_when_the_collector_disconnects(self):
        # Otherwise the caller waits on a future nothing will ever resolve.
        link = make_link()
        await link.start()
        collector = FakeCollector()
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            task = asyncio.create_task(link.read_registers(address=0x00AB, count=1, timeout_s=5.0))
            await asyncio.sleep(0.05)
            await collector.close()
            with pytest.raises(TransactionFailed):
                await task
        finally:
            await link.stop()


class TestUpstreamIsolation:
    async def test_a_dead_upstream_never_tears_down_the_collector_session(self):
        """The regression test for the 2026-08-20 reconnect storm.

        A hand-rolled proxy tore down the collector whenever the vendor cloud
        closed or refused, turning one ordinary cloud disconnect into 19
        collector reconnects in 43 seconds. The collector session must survive
        an upstream that is dead for the whole run.
        """
        dead_port = await free_port()
        link = make_link(
            upstream_host="127.0.0.1",
            upstream_port=dead_port,
            upstream_backoff_initial_s=0.05,
            upstream_backoff_max_s=0.1,
        )
        await link.start()
        collector = FakeCollector()
        collector.auto_reply = RESP_TYPE
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            # Long enough for several failed upstream attempts and backoffs.
            await asyncio.sleep(0.4)
            assert link.collector_connected is True
            assert link.upstream_connected is False
            # And our own polling still works with no cloud at all.
            words = await link.read_registers(address=0x00AB, count=1, timeout_s=2.0)
            assert words == [0x7803]
        finally:
            await collector.close()
            await link.stop()

    async def test_no_upstream_configured_is_a_clean_local_only_mode(self):
        link = make_link()  # upstream_host is None
        await link.start()
        collector = FakeCollector()
        collector.auto_reply = RESP_TYPE
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            assert link.upstream_connected is False
            words = await link.read_registers(address=0x00AB, count=1, timeout_s=2.0)
            assert words == [0x7803]
        finally:
            await collector.close()
            await link.stop()


class TestDesync:
    async def test_an_unsolicited_response_drops_the_collector(self):
        # No transaction id means a stray response cannot be discarded safely.
        link = make_link()
        await link.start()
        collector = FakeCollector()
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            await collector.send(RESP_TYPE)  # nobody asked
            assert await wait_for(lambda: not link.collector_connected, limit_s=2.0)
        finally:
            await collector.close()
            await link.stop()

    async def test_a_corrupt_frame_drops_the_collector(self):
        link = make_link()
        await link.start()
        collector = FakeCollector()
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            task = asyncio.create_task(link.read_registers(address=0x00AB, count=1, timeout_s=5.0))
            await asyncio.sleep(0.05)
            bad = bytearray(RESP_TYPE)
            bad[3] ^= 0xFF  # breaks the CRC
            await collector.send(bytes(bad))
            with pytest.raises(TransactionFailed):
                await task
            assert await wait_for(lambda: not link.collector_connected, limit_s=2.0)
        finally:
            await collector.close()
            await link.stop()

    async def test_recovers_on_the_next_connection_after_a_desync(self):
        # A drop must not poison the link permanently: the collector redials
        # within about a second in the field.
        link = make_link()
        await link.start()
        first = FakeCollector()
        try:
            await first.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            await first.send(RESP_TYPE)
            assert await wait_for(lambda: not link.collector_connected, limit_s=2.0)
            await first.close()

            second = FakeCollector()
            second.auto_reply = RESP_TYPE
            await second.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected, limit_s=2.0)
            words = await link.read_registers(address=0x00AB, count=1, timeout_s=2.0)
            assert words == [0x7803]
            await second.close()
        finally:
            await link.stop()


class TestFraming:
    async def test_reassembles_a_response_split_across_two_packets(self):
        # TCP may deliver a frame in any two pieces.
        link = make_link()
        await link.start()
        collector = FakeCollector()
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            task = asyncio.create_task(link.read_registers(address=0x00AB, count=1, timeout_s=3.0))
            await asyncio.sleep(0.05)
            await collector.send(RESP_TYPE[:3])
            await asyncio.sleep(0.05)
            await collector.send(RESP_TYPE[3:])
            assert await task == [0x7803]
        finally:
            await collector.close()
            await link.stop()

    async def test_handles_an_at_line_arriving_before_our_modbus_reply(self):
        """Both protocols share the socket, and the AT line is the cloud's.

        With no upstream configured the AT line has nowhere to go, and it must
        not be mistaken for our Modbus answer.
        """
        link = make_link()
        await link.start()
        collector = FakeCollector()
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            pdu = bytes([0x01, 0x83, 0x02])
            exception_frame = pdu + crc16(pdu).to_bytes(2, "little")
            task = asyncio.create_task(link.read_registers(address=0x00AB, count=1, timeout_s=3.0))
            await asyncio.sleep(0.05)
            await collector.send(exception_frame)
            with pytest.raises(TransactionFailed):
                await task
        finally:
            await collector.close()
            await link.stop()
