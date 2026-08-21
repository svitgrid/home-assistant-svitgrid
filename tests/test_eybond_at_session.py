"""Per-connection behaviour, now owned by CollectorSession under the hub.

Ported from the single-collector link suite when `link.py` was superseded.
These behaviours did not change -- framing reassembly, timeouts, desync
handling and vendor-relay isolation are all properties of ONE connection, and
that is exactly what `CollectorSession` is. Only the owner of the socket moved.
"""

import asyncio
import contextlib

import pytest

from custom_components.svitgrid.eybond_at.demux import Direction, split_frames
from custom_components.svitgrid.eybond_at.hub import EybondAtHub, HubConfig
from custom_components.svitgrid.eybond_at.modbus_rtu import build_read, crc16
from custom_components.svitgrid.eybond_at.session import TransactionFailed

pytestmark = pytest.mark.usefixtures("socket_enabled")

RESP_TYPE = bytes.fromhex("0103027803da45")  # register 171 -> 0x7803
REQ_TYPE = build_read(slave=1, address=0x00AB, count=1)
AT_REPLY_DTUPN = b"AT+DTUPN:I20000282044487591\r\n"


async def wait_for(predicate, limit_s=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class FakeCollector:
    """Dials the hub and answers whatever it is asked."""

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


def make_hub(**overrides) -> EybondAtHub:
    defaults = {
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "announce_target": "127.0.0.1",
        "announce_interval_s": 0.05,
        "tick_interval_s": 0.02,
    }
    return EybondAtHub(HubConfig(**{**defaults, **overrides}), ip_provider=lambda: "127.0.0.1")


class ModbusCollector(FakeCollector):
    """Answers register reads properly, so identification completes.

    The hub identifies every connection as soon as it arrives, so a session is
    never idle immediately after connecting. Tests that need a QUIET session
    have to let that finish first.
    """

    def __init__(self, registers: dict[int, int] | None = None):
        super().__init__()
        self.registers = registers or {171: 0x7803, 184: 11, 300: 0}
        self._buf = b""
        self.answer = True

    async def _pump(self):
        try:
            while True:
                data = await self.reader.read(4096)
                if not data:
                    return
                self.received.append(data)
                if not self.answer:
                    continue
                self._buf += data
                frames, self._buf = split_frames(self._buf, Direction.REQUEST)
                for frame in frames:
                    if frame.raw.startswith(b"AT+"):
                        # Answer AT queries too: a heartbeat that never gets a
                        # reply times out and desyncs the session, which would
                        # make these tests measure the wrong thing.
                        cmd = frame.raw[3:-2].split(b"?")[0].split(b"=")[0]
                        await self.send(b"AT+" + cmd + b":\r\n")
                        continue
                    address = int.from_bytes(frame.raw[2:4], "big")
                    count = int.from_bytes(frame.raw[4:6], "big")
                    words = [self.registers.get(address + i, 0) for i in range(count)]
                    body = bytes([1, 0x03, count * 2]) + b"".join(
                        w.to_bytes(2, "big") for w in words
                    )
                    await self.send(body + crc16(body).to_bytes(2, "little"))
        except (asyncio.CancelledError, ConnectionError):
            return


async def one_session(hub, collector):
    await collector.connect(hub.listen_port)
    assert await wait_for(lambda: hub.collector_count == 1)
    return hub.sessions[0]


async def quiet_session(hub, collector):
    """A session whose identification has finished, so the line is idle."""
    session = await one_session(hub, collector)
    assert await wait_for(lambda: session.identity is not None, limit_s=3.0)
    collector.answer = False
    collector.received.clear()
    return session


class TestTransactions:
    async def test_reads_registers_over_a_session(self):
        hub = make_hub()
        await hub.start()
        collector = FakeCollector()
        collector.auto_reply = RESP_TYPE
        try:
            session = await one_session(hub, collector)
            assert await session.read_registers(0x00AB, 1, timeout_s=2.0) == [0x7803]
            assert REQ_TYPE in b"".join(collector.received)
        finally:
            await collector.close()
            await hub.stop()

    async def test_an_at_query_round_trips(self):
        hub = make_hub()
        await hub.start()
        collector = FakeCollector()
        collector.auto_reply = AT_REPLY_DTUPN
        try:
            session = await one_session(hub, collector)
            assert await session.at_query("DTUPN", timeout_s=2.0) == "I20000282044487591"
        finally:
            await collector.close()
            await hub.stop()

    async def test_an_at_reply_for_a_different_command_is_rejected(self):
        # No transaction id, so a mismatched reply is a desync signal. Accepting
        # it would return another command's value as the answer we asked for.
        hub = make_hub()
        await hub.start()
        collector = FakeCollector()
        collector.auto_reply = b"AT+ATVER:1.14\r\n"
        try:
            session = await one_session(hub, collector)
            with pytest.raises(TransactionFailed):
                await session.at_query("DTUPN", timeout_s=2.0)
        finally:
            await collector.close()
            await hub.stop()

    async def test_a_read_times_out_when_the_collector_stays_silent(self):
        hub = make_hub()
        await hub.start()
        collector = FakeCollector()  # never answers
        try:
            session = await one_session(hub, collector)
            with pytest.raises(TransactionFailed):
                await session.read_registers(0x00AB, 1, timeout_s=0.3)
        finally:
            await collector.close()
            await hub.stop()

    async def test_a_pending_read_fails_when_the_collector_disconnects(self):
        # Otherwise the caller waits on a future nothing will ever resolve.
        hub = make_hub()
        await hub.start()
        collector = FakeCollector()
        try:
            session = await one_session(hub, collector)
            task = asyncio.create_task(session.read_registers(0x00AB, 1, timeout_s=5.0))
            await asyncio.sleep(0.05)
            await collector.close()
            with pytest.raises(TransactionFailed):
                await task
        finally:
            await hub.stop()


class TestFraming:
    async def test_reassembles_a_response_split_across_two_packets(self):
        # TCP may deliver a frame in any two pieces.
        hub = make_hub()
        await hub.start()
        collector = ModbusCollector()
        try:
            session = await quiet_session(hub, collector)
            task = asyncio.create_task(session.read_registers(0x00AB, 1, timeout_s=3.0))
            await asyncio.sleep(0.05)
            await collector.send(RESP_TYPE[:3])
            await asyncio.sleep(0.05)
            await collector.send(RESP_TYPE[3:])
            assert await task == [0x7803]
        finally:
            await collector.close()
            await hub.stop()

    async def test_an_exception_response_fails_the_read(self):
        hub = make_hub()
        await hub.start()
        collector = FakeCollector()
        try:
            session = await one_session(hub, collector)
            pdu = bytes([0x01, 0x83, 0x02])
            task = asyncio.create_task(session.read_registers(0x00AB, 1, timeout_s=3.0))
            await asyncio.sleep(0.05)
            await collector.send(pdu + crc16(pdu).to_bytes(2, "little"))
            with pytest.raises(TransactionFailed):
                await task
        finally:
            await collector.close()
            await hub.stop()


class TestDesync:
    async def test_an_unsolicited_response_drops_the_session(self):
        # No transaction id means a stray response cannot be discarded safely.
        hub = make_hub()
        await hub.start()
        collector = ModbusCollector()
        try:
            await quiet_session(hub, collector)
            await collector.send(RESP_TYPE)  # nobody asked
            assert await wait_for(lambda: hub.collector_count == 0, limit_s=2.0)
        finally:
            await collector.close()
            await hub.stop()

    async def test_a_corrupt_frame_drops_even_with_nothing_outstanding(self):
        # Isolates the FRAMING path from the transaction-timeout path: with no
        # request in flight the scheduler has no deadline, so only the framing
        # error can drop the connection.
        hub = make_hub(txn_timeout_ms=60_000)
        await hub.start()
        collector = FakeCollector()
        try:
            await one_session(hub, collector)
            bad = bytearray(RESP_TYPE)
            bad[3] ^= 0xFF
            await collector.send(bytes(bad))
            assert await wait_for(lambda: hub.collector_count == 0, limit_s=2.0)
        finally:
            await collector.close()
            await hub.stop()

    async def test_recovers_on_the_next_connection(self):
        # A drop must not poison the hub: the collector redials within about a
        # second in the field.
        hub = make_hub()
        await hub.start()
        first = ModbusCollector()
        try:
            await quiet_session(hub, first)
            await first.send(RESP_TYPE)
            assert await wait_for(lambda: hub.collector_count == 0, limit_s=2.0)
            await first.close()

            second = FakeCollector()
            second.auto_reply = RESP_TYPE
            session = await one_session(hub, second)
            assert await session.read_registers(0x00AB, 1, timeout_s=2.0) == [0x7803]
            await second.close()
        finally:
            await hub.stop()


class TestUpstreamIsolation:
    async def test_a_dead_upstream_never_tears_down_the_session(self):
        """Regression test for the 2026-08-20 reconnect storm.

        A hand-rolled proxy coupled the collector session to the vendor
        cloud's liveness, turning one ordinary cloud disconnect into 19
        collector reconnects in 43 seconds.
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
        collector = FakeCollector()
        collector.auto_reply = RESP_TYPE
        try:
            session = await one_session(hub, collector)
            await asyncio.sleep(0.4)  # several failed upstream attempts
            assert hub.collector_count == 1
            assert session.upstream_connected is False
            assert await session.read_registers(0x00AB, 1, timeout_s=2.0) == [0x7803]
        finally:
            await collector.close()
            await hub.stop()

    async def test_local_only_is_a_first_class_mode(self):
        hub = make_hub()  # no upstream configured
        await hub.start()
        collector = FakeCollector()
        collector.auto_reply = RESP_TYPE
        try:
            session = await one_session(hub, collector)
            assert session.upstream_connected is False
            assert await session.read_registers(0x00AB, 1, timeout_s=2.0) == [0x7803]
        finally:
            await collector.close()
            await hub.stop()


class TestIdleHeartbeat:
    """Our poll cadence is 300 s; the collector drops an idle line at ~90 s.

    Without a heartbeat the session churns constantly. It still works -- a
    reconnect re-identifies and reads -- but every reading costs a fresh
    handshake, and the log fills with disconnects that look like a fault.
    """

    async def test_sends_a_heartbeat_once_the_line_goes_quiet(self):
        hub = make_hub()
        await hub.start()
        collector = ModbusCollector()
        try:
            session = await quiet_session(hub, collector)
            collector.answer = True
            collector.received.clear()
            # Pretend the line has been silent longer than the threshold.
            session._last_activity_ms -= 60_000
            await session.heartbeat_if_idle(idle_ms=20_000)
            sent = b"".join(collector.received)
            assert b"AT+HTBT?" in sent
        finally:
            await collector.close()
            await hub.stop()

    async def test_stays_quiet_while_the_line_is_busy(self):
        # A heartbeat on a line that is already talking is pure noise, and on
        # a protocol with no transaction id it is an extra chance to desync.
        hub = make_hub()
        await hub.start()
        collector = ModbusCollector()
        try:
            session = await quiet_session(hub, collector)
            collector.answer = True
            collector.received.clear()
            await session.heartbeat_if_idle(idle_ms=20_000)
            assert b"AT+HTBT?" not in b"".join(collector.received)
        finally:
            await collector.close()
            await hub.stop()

    async def test_a_read_counts_as_activity(self):
        """Otherwise a polled session would still be heartbeated needlessly."""
        hub = make_hub()
        await hub.start()
        collector = ModbusCollector()
        try:
            session = await quiet_session(hub, collector)
            collector.answer = True
            session._last_activity_ms -= 60_000
            await session.read_registers(0x00AB, 1, timeout_s=2.0)
            collector.received.clear()
            await session.heartbeat_if_idle(idle_ms=20_000)
            assert b"AT+HTBT?" not in b"".join(collector.received)
        finally:
            await collector.close()
            await hub.stop()

    async def test_a_failed_heartbeat_does_not_raise(self):
        # Best effort: a heartbeat that times out must not kill the loop that
        # sent it.
        hub = make_hub(txn_timeout_ms=200)
        await hub.start()
        collector = ModbusCollector()
        try:
            session = await quiet_session(hub, collector)
            collector.answer = False  # never replies
            session._last_activity_ms -= 60_000
            await session.heartbeat_if_idle(idle_ms=20_000)
        finally:
            await collector.close()
            await hub.stop()
