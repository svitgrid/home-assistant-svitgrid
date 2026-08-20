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


class FakeCloud:
    """Accepts a connection then hangs up, like the throttled vendor cloud did."""

    def __init__(self):
        self.server = None
        self.port = None
        self.connections = 0

    async def start(self):
        self.server = await asyncio.start_server(self._on_conn, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _on_conn(self, reader, writer):
        self.connections += 1
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def stop(self):
        if self.server:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()


async def free_port() -> int:
    """Reserve and release a port, so connecting to it is refused."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


def make_link(**overrides) -> EybondAtLink:
    defaults = {
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "announce_target": "127.0.0.1",
        "announce_interval_s": 0.05,
        "tick_interval_s": 0.02,
    }
    return EybondAtLink(LinkConfig(**{**defaults, **overrides}), ip_provider=lambda: "127.0.0.1")


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

    async def test_an_upstream_that_connects_then_hangs_up_leaves_the_collector_alone(
        self,
    ):
        """The exact shape of the 2026-08-20 incident.

        `test_a_dead_upstream_never_tears_down_the_collector_session` covers an
        upstream that never accepts. The real cloud DID accept, ran a
        handshake, and then closed -- and that is what the broken proxy
        coupled to. Mutation testing showed the connect-failure test alone did
        not catch a teardown on upstream CLOSE.
        """
        cloud = FakeCloud()
        await cloud.start()
        link = make_link(
            upstream_host="127.0.0.1",
            upstream_port=cloud.port,
            upstream_backoff_initial_s=0.05,
            upstream_backoff_max_s=0.1,
        )
        await link.start()
        collector = FakeCollector()
        collector.auto_reply = RESP_TYPE
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            # Several accept-then-close cycles, exactly as the cloud behaved.
            assert await wait_for(lambda: cloud.connections >= 3, limit_s=3.0)
            assert link.collector_connected is True
            words = await link.read_registers(address=0x00AB, count=1, timeout_s=2.0)
            assert words == [0x7803]
        finally:
            await collector.close()
            await link.stop()
            await cloud.stop()

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

    async def test_a_corrupt_frame_drops_even_with_nothing_outstanding(self):
        """Isolates the FRAMING path from the transaction-timeout path.

        With no request in flight the scheduler has no deadline, so the tick
        loop cannot rescue this. Only the framing error can drop the
        connection -- mutation testing showed the other corrupt-frame test
        passed via the 3 s timeout instead.
        """
        link = make_link(txn_timeout_ms=60_000)
        await link.start()
        collector = FakeCollector()
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            bad = bytearray(RESP_TYPE)
            bad[3] ^= 0xFF  # breaks the CRC
            await collector.send(bytes(bad))
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

    async def test_an_at_reply_for_a_different_command_is_rejected(self):
        """No transaction id means a mismatched reply is a desync signal.

        Accepting it would return another command's value as though it were
        the answer we asked for.
        """
        link = make_link()
        await link.start()
        collector = FakeCollector()
        collector.auto_reply = b"AT+ATVER:1.14\r\n"
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            with pytest.raises(TransactionFailed):
                await link.at_query("DTUPN", timeout_s=2.0)
        finally:
            await collector.close()
            await link.stop()

    async def test_exposes_the_vendor_target_without_reaching_into_config(self):
        link = make_link(upstream_host="cloud.example", upstream_port=18899)
        assert link.upstream_target == ("cloud.example", 18899)

    async def test_local_only_reports_no_vendor_target(self):
        assert make_link().upstream_target is None


class TestAnnounceTargeting:
    """Finding the collector, and telling it an address it can actually reach."""

    async def test_an_explicit_advertised_ip_overrides_auto_detection(self):
        """REQUIRED when Home Assistant runs in a bridge-mode container.

        `default_local_ip` would return the CONTAINER's address (172.x). The
        collector cannot reach that, and the only symptom is that nothing ever
        connects -- no error, no log, just silence.
        """
        received: list[bytes] = []

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                received.append(data)

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        link = make_link(announce_port=port, advertised_ip="192.168.1.50")
        await link.start()
        try:
            assert await wait_for(lambda: received)
            assert received[0].startswith(b"set>server=192.168.1.50:")
        finally:
            await link.stop()
            transport.close()

    async def test_learns_the_collector_address_when_it_dials_in(self):
        # Never needed to REACH it -- the announce is a broadcast. It matters
        # for telling a user which device this is, and for announce_target on a
        # network where broadcast does not cross a VLAN.
        link = make_link()
        await link.start()
        collector = FakeCollector()
        try:
            assert link.collector_address is None
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            assert link.collector_address == "127.0.0.1"
        finally:
            await collector.close()
            await link.stop()

    async def test_also_unicasts_to_the_last_known_collector(self):
        """Broadcast finds a collector we have never met; unicast keeps a known
        one reachable on a network that filters broadcast."""
        received: list[tuple[bytes, tuple]] = []

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                received.append((data, addr))

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        # announce_target is 127.0.0.1 in tests, and so is the peer, so point
        # the broadcast elsewhere to tell the two datagrams apart.
        link = make_link(announce_port=port, announce_target="127.0.0.2")
        await link.start()
        collector = FakeCollector()
        try:
            await collector.connect(link.listen_port)
            assert await wait_for(lambda: link.collector_connected)
            await collector.close()
            assert await wait_for(lambda: not link.collector_connected)
            # Now disconnected with a known address: the unicast must appear.
            assert await wait_for(lambda: len(received) >= 1, limit_s=2.0)
        finally:
            await link.stop()
            transport.close()

    async def test_does_not_unicast_before_a_collector_is_known(self):
        received: list[tuple[bytes, tuple]] = []

        class Receiver(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                received.append((data, addr))

        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
        port = transport.get_extra_info("sockname")[1]
        link = make_link(announce_port=port, announce_target="127.0.0.1")
        await link.start()
        try:
            assert await wait_for(lambda: len(received) >= 3)
            # One target only, so no duplicate datagrams per announce round.
            assert link.collector_address is None
        finally:
            await link.stop()
            transport.close()
