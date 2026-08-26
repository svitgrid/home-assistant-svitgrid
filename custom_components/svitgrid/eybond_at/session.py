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
from .modbus_rtu import (
    ModbusError,
    ModbusExceptionError,
    build_read,
    build_write_multiple,
    describe_write_exception,
    parse_read_response,
    parse_write_multiple_response,
)
from .scheduler import ActionKind, SchedulerBusy, TxnScheduler

_LOGGER = logging.getLogger(__name__)

# How long a line may sit silent before we send AT+HTBT?.
#
# The vendor cloud sends one roughly every 20 s, and a session that goes quiet
# gets dropped: measured 2026-08-21, Home Assistant's sessions closed at 91,
# 92, 93, 115, 119 and 120 seconds while polling only every 300 s. A direct
# comparison against the collector gave a silent session a 90 s maximum and a
# heartbeated one 156 s.
#
# That evidence is suggestive rather than conclusive -- the sample is small --
# but it costs one AT query per 20 s on an otherwise idle line, and it matches
# what the vendor's own server does.
HEARTBEAT_IDLE_MS = 20_000


class TransactionFailed(Exception):
    """A request could not be completed: no collector, timeout, or desync."""


class WriteRefused(TransactionFailed):
    """The device answered a write with a Modbus exception.

    Distinct from every other `TransactionFailed` because it is a real ANSWER,
    not an absence of one: the device parsed the frame and declined it, and
    the code says why. "The inverter will not change this in its current
    working mode" and "the line dropped" need opposite things from the user,
    so they must not arrive as the same failure.

    Subclasses `TransactionFailed` so the callers that already catch that --
    the harvest loop, setup, the hub -- keep working unchanged.
    """

    def __init__(self, *, code: int, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        # Stable and machine-readable; see `describe_write_exception`.
        self.reason = reason


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
        self._last_activity_ms = int(clock() * 1000)

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
        self._last_activity_ms = self._now_ms()
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

    async def heartbeat_if_idle(self, idle_ms: int = HEARTBEAT_IDLE_MS) -> None:
        """Send AT+HTBT? when the line has been silent too long.

        Our poll cadence is 300 s and the collector drops a session after
        roughly 90. Without this the connection churns constantly: it works,
        because a reconnect re-identifies and reads, but every reading costs a
        fresh handshake and the log fills with disconnects.
        """
        if self._closed or self._lock.locked():
            return
        if self._now_ms() - self._last_activity_ms < idle_ms:
            return
        try:
            await self.at_query("HTBT", timeout_s=3.0)
        except TransactionFailed as err:
            self._diag(f"heartbeat failed: {err}")

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
            self._last_activity_ms = self._now_ms()
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

    async def write_register(self, address: int, value: int, timeout_s: float = 5.0) -> None:
        """Write one register with FC16, the only write this protocol defines.

        Returns nothing, deliberately. An FC16 acknowledgement carries the
        QUANTITY of registers written, never the value, so there is no value
        here that could be mistaken for confirmation -- and confirmation is
        not what an acknowledgement is. It proves the device parsed the
        request. Only a read-back proves the setting took effect; see
        `write_register_verified`.

        Raises `WriteRefused` when the device declines, so the caller can say
        which of the three documented refusals it was.
        """
        raw = await self._transact(
            build_write_multiple(self.slave_id, address, [value]), timeout_s
        )
        try:
            acked_address, acked_quantity = parse_write_multiple_response(raw)
        except ModbusExceptionError as err:
            described = describe_write_exception(err.code)
            raise WriteRefused(
                code=err.code,
                reason=described.reason,
                message=f"write to register {address} refused: {described.message}",
            ) from err
        except ModbusError as err:
            raise TransactionFailed(str(err)) from err

        # No transaction id, so a reply describing a different request is a
        # desync, not something to trust -- see the module docstring. Both
        # fields are checked: an ack for the right register but the wrong
        # count still describes a frame we did not send.
        if acked_address != address:
            raise TransactionFailed(
                f"write ack is for register {acked_address}, expected {address}"
            )
        if acked_quantity != 1:
            raise TransactionFailed(
                f"write ack covers {acked_quantity} registers, expected 1"
            )

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
