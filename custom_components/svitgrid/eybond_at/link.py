"""Asyncio transport for the EyBond/SmartESS collector.

Owns the sockets and the clock, and pumps the pure core: `demux` for framing,
`scheduler` for whose turn it is. No Home Assistant imports, so it is
importable and testable standalone.

── Shape ─────────────────────────────────────────────────────────────────
An EyBond/SmartESS collector is a TCP *client*. It dials its vendor cloud and
never listens, so nothing can poll it directly. We broadcast an
unauthenticated UDP command telling it to dial *us* instead
(`set>server=<our-ip>:<our-port>;` to UDP 58899), accept its single TCP
connection, and optionally relay onward to the vendor cloud so the customer's
SmartESS app keeps working.

Measured 2026-08-20: the collector dialled in 0.6 s after the first announce.

── The one contract that is not negotiable ───────────────────────────────
**A dead, refusing, or throttled vendor cloud must NEVER tear down the
collector session.**

This was learned by breaking it. On 2026-08-20 a hand-rolled proxy coupled
the two, so an ordinary cloud disconnect closed the collector socket, the
collector redialled immediately, the proxy opened a fresh cloud connection,
the cloud closed it again -- 19 collector reconnects in 43 seconds. With the
two decoupled, one collector session then stayed up for a full hour across
roughly fifty cloud disconnects.

So `_upstream_loop` is best-effort and backed off, `_write_upstream` swallows
its errors, and nothing on the upstream path can reach the collector socket.

── Why a desync closes the connection ────────────────────────────────────
See `scheduler.py`. There is no transaction id, so a stray or corrupt frame
means attribution is lost and cannot be recovered. Dropping is cheap: the
collector redials within about a second.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from .at_codec import AtProtocolError, build_query, parse_response
from .demux import Direction, split_frames
from .modbus_rtu import ModbusError, build_read, parse_read_response
from .scheduler import ActionKind, SchedulerBusy, TxnScheduler

_LOGGER = logging.getLogger(__name__)

DEFAULT_LISTEN_PORT = 8899
ANNOUNCE_UDP_PORT = 58899
DEFAULT_ANNOUNCE_TARGET = "255.255.255.255"


class TransactionFailed(Exception):
    """A request could not be completed: no collector, timeout, or desync."""


def default_local_ip() -> str:
    """Best-effort local address, re-read on every announce so DHCP self-heals."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


@dataclass
class LinkConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = DEFAULT_LISTEN_PORT
    announce_target: str = DEFAULT_ANNOUNCE_TARGET
    announce_port: int = ANNOUNCE_UDP_PORT
    announce_interval_s: float = 3.0
    upstream_host: str | None = None
    upstream_port: int | None = None
    upstream_backoff_initial_s: float = 5.0
    upstream_backoff_max_s: float = 60.0
    tick_interval_s: float = 0.25
    slave_id: int = 1
    txn_timeout_ms: int = 3000


class EybondAtLink:
    def __init__(
        self,
        config: LinkConfig,
        *,
        ip_provider: Callable[[], str] = default_local_ip,
        clock: Callable[[], float] = time.monotonic,
        on_diag: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._ip_provider = ip_provider
        self._clock = clock
        self._on_diag = on_diag
        self._scheduler = TxnScheduler(txn_timeout_ms=config.txn_timeout_ms)

        self._server: asyncio.AbstractServer | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._collector_writer: asyncio.StreamWriter | None = None
        self._upstream_writer: asyncio.StreamWriter | None = None
        self._collector_buf = b""
        self._upstream_buf = b""
        self._pending: asyncio.Future | None = None
        self._tasks: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._actual_port: int | None = None

    # ── properties ────────────────────────────────────────────────────────
    @property
    def listen_port(self) -> int | None:
        return self._actual_port

    @property
    def collector_connected(self) -> bool:
        return self._collector_writer is not None

    @property
    def upstream_connected(self) -> bool:
        return self._upstream_writer is not None

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_collector, self._config.listen_host, self._config.listen_port
        )
        self._actual_port = self._server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, family=socket.AF_INET, allow_broadcast=True
        )
        self._udp = transport
        self._spawn(self._announce_loop())
        self._spawn(self._tick_loop())

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        await self._drop_collector("link stopped")
        if self._udp:
            self._udp.close()
            self._udp = None
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    def _spawn(self, coro) -> None:
        self._tasks.append(asyncio.create_task(coro))

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _diag(self, reason: str) -> None:
        _LOGGER.debug("eybond_at: %s", reason)
        if self._on_diag:
            self._on_diag(reason)

    # ── announce ──────────────────────────────────────────────────────────
    async def _announce_loop(self) -> None:
        while True:
            if self._collector_writer is None:
                self._send_announce()
            await asyncio.sleep(self._config.announce_interval_s)

    def _send_announce(self) -> None:
        if self._udp is None or self._actual_port is None:
            return
        # ip_provider is called FRESH every time, never cached, so a DHCP
        # renewal repairs itself without a restart.
        command = f"set>server={self._ip_provider()}:{self._actual_port};".encode()
        with contextlib.suppress(OSError):
            self._udp.sendto(command, (self._config.announce_target, self._config.announce_port))

    # ── collector connection ──────────────────────────────────────────────
    async def _on_collector(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._collector_writer is not None:
            # Exactly one session. A second would interleave responses that
            # cannot be attributed, because there is no transaction id.
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        self._collector_writer = writer
        self._collector_buf = b""
        self._scheduler.reset()
        self._diag("collector connected")
        if self._config.upstream_host and self._config.upstream_port:
            self._spawn(self._upstream_loop())
        await self._pump_collector(reader)

    async def _pump_collector(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self._collector_buf += data
                try:
                    frames, self._collector_buf = split_frames(
                        self._collector_buf, Direction.RESPONSE
                    )
                except ModbusError as err:
                    await self._drop_collector(f"framing error: {err}")
                    return
                for frame in frames:
                    actions = self._scheduler.on_collector_frame(frame, self._now_ms())
                    if await self._apply(actions):
                        return
        except (ConnectionError, asyncio.CancelledError):
            pass
        await self._drop_collector("collector disconnected")

    # ── upstream, best-effort and isolated ────────────────────────────────
    async def _upstream_loop(self) -> None:
        backoff = self._config.upstream_backoff_initial_s
        while self._collector_writer is not None:
            try:
                reader, writer = await asyncio.open_connection(
                    self._config.upstream_host, self._config.upstream_port
                )
            except OSError as err:
                # NOTHING here may touch the collector socket.
                self._diag(f"upstream connect failed ({err}); collector unaffected")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._config.upstream_backoff_max_s)
                continue
            self._upstream_writer = writer
            backoff = self._config.upstream_backoff_initial_s
            self._diag("upstream connected")
            with contextlib.suppress(ConnectionError, asyncio.CancelledError):
                await self._pump_upstream(reader)
            self._diag("upstream closed; collector unaffected")
            self._upstream_writer = None
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            await asyncio.sleep(backoff)

    async def _pump_upstream(self, reader: asyncio.StreamReader) -> None:
        self._upstream_buf = b""
        while True:
            data = await reader.read(4096)
            if not data:
                return
            self._upstream_buf += data
            try:
                frames, self._upstream_buf = split_frames(self._upstream_buf, Direction.REQUEST)
            except ModbusError as err:
                # A broken vendor frame is the vendor's problem. Drop the
                # upstream connection, never the collector.
                self._diag(f"upstream framing error: {err}")
                return
            for frame in frames:
                actions = self._scheduler.on_cloud_frame(frame, self._now_ms())
                if await self._apply(actions):
                    return

    async def _write_upstream(self, data: bytes) -> None:
        writer = self._upstream_writer
        if writer is None:
            return  # local-only mode, or the cloud is away. Not an error.
        try:
            writer.write(data)
            await writer.drain()
        except (OSError, ConnectionError) as err:
            self._diag(f"upstream write dropped ({err}); collector unaffected")
            self._upstream_writer = None

    # ── action application ────────────────────────────────────────────────
    async def _apply(self, actions) -> bool:
        for action in actions:
            if action.kind is ActionKind.SEND_TO_COLLECTOR:
                await self._write_collector(action.payload)
            elif action.kind is ActionKind.SEND_TO_CLOUD:
                await self._write_upstream(action.payload)
            elif action.kind is ActionKind.RESOLVE_OURS:
                self._resolve(action.payload)
            elif action.kind is ActionKind.FAIL_OURS:
                self._fail(action.reason or "transaction failed")
            elif action.kind is ActionKind.DROP_COLLECTOR:
                await self._drop_collector(action.reason or "desynchronised")
                return True
        return False

    async def _write_collector(self, data: bytes) -> None:
        writer = self._collector_writer
        if writer is None:
            return
        try:
            writer.write(data)
            await writer.drain()
        except (OSError, ConnectionError) as err:
            await self._drop_collector(f"collector write failed: {err}")

    async def _drop_collector(self, reason: str) -> None:
        writer = self._collector_writer
        self._collector_writer = None
        self._collector_buf = b""
        if writer is not None:
            self._diag(f"dropping collector: {reason}")
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
        for action in self._scheduler.reset():
            if action.kind is ActionKind.FAIL_OURS:
                self._fail(action.reason or reason)
        self._fail(reason)
        upstream = self._upstream_writer
        self._upstream_writer = None
        if upstream is not None:
            with contextlib.suppress(Exception):
                upstream.close()
                await upstream.wait_closed()

    def _resolve(self, payload: bytes) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_result(payload)

    def _fail(self, reason: str) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(TransactionFailed(reason))

    # ── tick ──────────────────────────────────────────────────────────────
    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.tick_interval_s)
            actions = self._scheduler.on_tick(self._now_ms())
            if actions:
                await self._apply(actions)

    # ── public request API ────────────────────────────────────────────────
    async def _transact(self, payload: bytes, timeout_s: float) -> bytes:
        async with self._lock:
            if self._collector_writer is None:
                raise TransactionFailed("no collector connected")
            loop = asyncio.get_running_loop()
            self._pending = loop.create_future()
            try:
                actions = self._scheduler.request(payload, self._now_ms())
            except SchedulerBusy as err:
                self._pending = None
                raise TransactionFailed(str(err)) from err
            await self._apply(actions)
            try:
                return await asyncio.wait_for(self._pending, timeout_s)
            except TimeoutError as err:
                raise TransactionFailed("timed out waiting for the collector") from err
            finally:
                self._pending = None

    async def read_registers(self, address: int, count: int, timeout_s: float = 5.0) -> list[int]:
        """Read `count` holding registers. Returns raw unsigned words."""
        request = build_read(self._config.slave_id, address, count)
        raw = await self._transact(request, timeout_s)
        try:
            return parse_read_response(raw)
        except ModbusError as err:
            raise TransactionFailed(str(err)) from err

    async def at_query(self, command: str, timeout_s: float = 3.0) -> str:
        """Send `AT+{command}?` and return the value from the reply."""
        raw = await self._transact(build_query(command), timeout_s)
        try:
            response = parse_response(raw)
        except AtProtocolError as err:
            raise TransactionFailed(str(err)) from err
        if response.command != command:
            raise TransactionFailed(f"reply is for {response.command!r}, expected {command!r}")
        return response.value
