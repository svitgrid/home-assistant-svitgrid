"""One collector's connection: its framing buffer, its turn-taking, its relay.

Everything here is **per connection**, which is the unit the protocol actually
constrains. The "exactly one at a time" rule is per collector, not per network:
three collectors on one LAN are three independent serialized lines, and each
needs its own demultiplexer buffer, its own scheduler, and its own vendor
relay.

Getting that wrong is not subtle. Sharing one scheduler across collectors would
attribute collector A's response to collector B's request — and with no
transaction id in this protocol, nothing downstream could ever detect it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .at_codec import AtProtocolError, build_query, parse_response
from .demux import Direction, split_frames
from .modbus_rtu import ModbusError, build_read, parse_read_response
from .scheduler import ActionKind, SchedulerBusy, TxnScheduler

_LOGGER = logging.getLogger(__name__)


class TransactionFailed(Exception):
    """A request could not be completed: no collector, timeout, or desync."""


class CollectorSession:
    """One accepted collector connection."""

    def __init__(
        self,
        *,
        writer: asyncio.StreamWriter,
        address: str,
        slave_id: int,
        txn_timeout_ms: int,
        clock,
        on_diag=None,
    ) -> None:
        self.address = address
        self.slave_id = slave_id
        # Set once the identity block has been read. The hub routes on it, so
        # a session stays unclaimed until then.
        self.identity = None

        self._writer = writer
        self._clock = clock
        self._on_diag = on_diag
        self._scheduler = TxnScheduler(txn_timeout_ms=txn_timeout_ms)
        self._buf = b""
        self._pending: asyncio.Future | None = None
        self._lock = asyncio.Lock()
        self._closed = False

        self._upstream_writer: asyncio.StreamWriter | None = None
        self._upstream_buf = b""
        self._upstream_task: asyncio.Task | None = None

    # ── properties ────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return not self._closed

    @property
    def upstream_connected(self) -> bool:
        return self._upstream_writer is not None

    @property
    def serial(self) -> str | None:
        return self.identity.serial if self.identity else None

    def _diag(self, reason: str) -> None:
        _LOGGER.debug("eybond_at[%s]: %s", self.address, reason)
        if self._on_diag:
            self._on_diag(f"[{self.address}] {reason}")

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    # ── inbound ───────────────────────────────────────────────────────────
    async def feed(self, data: bytes) -> bool:
        """Feed bytes from the collector. Returns False when the session died."""
        self._buf += data
        try:
            frames, self._buf = split_frames(self._buf, Direction.RESPONSE)
        except ModbusError as err:
            await self.close(f"framing error: {err}")
            return False
        for frame in frames:
            actions = self._scheduler.on_collector_frame(frame, self._now_ms())
            if await self._apply(actions):
                return False
        return True

    async def feed_upstream(self, data: bytes) -> bool:
        """Feed bytes from the vendor cloud, bound for the collector."""
        self._upstream_buf += data
        try:
            frames, self._upstream_buf = split_frames(self._upstream_buf, Direction.REQUEST)
        except ModbusError as err:
            # A broken vendor frame is the vendor's problem. Drop the upstream
            # connection, never the collector.
            self._diag(f"upstream framing error: {err}")
            return False
        for frame in frames:
            actions = self._scheduler.on_cloud_frame(frame, self._now_ms())
            if await self._apply(actions):
                return False
        return True

    async def tick(self) -> None:
        actions = self._scheduler.on_tick(self._now_ms())
        if actions:
            await self._apply(actions)

    # ── outbound ──────────────────────────────────────────────────────────
    async def _apply(self, actions) -> bool:
        """Returns True when the session was closed."""
        for action in actions:
            if action.kind is ActionKind.SEND_TO_COLLECTOR:
                await self._write(action.payload)
            elif action.kind is ActionKind.SEND_TO_CLOUD:
                await self._write_upstream(action.payload)
            elif action.kind is ActionKind.RESOLVE_OURS:
                self._resolve(action.payload)
            elif action.kind is ActionKind.FAIL_OURS:
                self._fail(action.reason or "transaction failed")
            elif action.kind is ActionKind.DROP_COLLECTOR:
                await self.close(action.reason or "desynchronised")
                return True
        return False

    async def _write(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (OSError, ConnectionError) as err:
            await self.close(f"write failed: {err}")

    async def _write_upstream(self, data: bytes) -> None:
        writer = self._upstream_writer
        if writer is None:
            return  # local-only, or the cloud is away. Not an error.
        try:
            writer.write(data)
            await writer.drain()
        except (OSError, ConnectionError) as err:
            self._diag(f"upstream write dropped ({err}); collector unaffected")
            self._upstream_writer = None

    def attach_upstream(self, writer: asyncio.StreamWriter | None) -> None:
        self._upstream_writer = writer
        self._upstream_buf = b""

    def _resolve(self, payload: bytes) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_result(payload)

    def _fail(self, reason: str) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_exception(TransactionFailed(reason))

    # ── transactions ──────────────────────────────────────────────────────
    async def _transact(self, payload: bytes, timeout_s: float) -> bytes:
        async with self._lock:
            if self._closed:
                raise TransactionFailed("collector disconnected")
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
        raw = await self._transact(build_read(self.slave_id, address, count), timeout_s)
        try:
            return parse_read_response(raw)
        except ModbusError as err:
            raise TransactionFailed(str(err)) from err

    async def at_query(self, command: str, timeout_s: float = 3.0) -> str:
        raw = await self._transact(build_query(command), timeout_s)
        try:
            response = parse_response(raw)
        except AtProtocolError as err:
            raise TransactionFailed(str(err)) from err
        if response.command != command:
            raise TransactionFailed(f"reply is for {response.command!r}, expected {command!r}")
        return response.value

    # ── teardown ──────────────────────────────────────────────────────────
    async def close(self, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        self._diag(f"closing: {reason}")
        for action in self._scheduler.reset():
            if action.kind is ActionKind.FAIL_OURS:
                self._fail(action.reason or reason)
        self._fail(reason)
        if self._upstream_task:
            self._upstream_task.cancel()
        for writer in (self._writer, self._upstream_writer):
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
                    await writer.wait_closed()
        self._upstream_writer = None
