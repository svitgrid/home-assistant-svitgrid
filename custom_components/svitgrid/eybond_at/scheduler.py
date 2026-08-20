"""Interleaving scheduler for the collector's single serialized line.

Pure state machine. No sockets, no clock, no Home Assistant imports: time
arrives as a `now_ms` argument, and every effect leaves as an `Action` for the
caller to perform.

── Why this module exists ────────────────────────────────────────────────
The collector has **no transaction id**. A response is matched to its request
by ORDER and by nothing else. The vendor cloud polls the same collector
through us, and we want to inject our own reads without corrupting its
session, so every frame in both directions passes through one place that
knows whose turn it is.

Measured 2026-08-20 across 89 polling cycles: request and response counts
paired exactly, 84/84 Modbus and 67/67 AT. The line is strictly serialized in
both protocols, with no pipelining. AT is request/response too, so an
outstanding `AT+HTBT?` occupies the line exactly as a register read does.

── Why desynchronisation is fatal rather than recoverable ────────────────
With a transaction id, a stray response is discarded and the stream carries
on. Here there is no id to match, so a response that arrives when nothing is
outstanding proves we have lost track of whose turn it is — and every later
attribution would be a guess that produces plausible, wrong data.

Both hazards therefore end the connection:

  * a response with no outstanding request, and
  * a request that never gets one.

That is cheap in practice. The collector redials within about a second, and
the vendor cloud already closes and reopens its own session every 25 to 60
seconds, so a drop costs at most one poll cycle.

── Why a full queue drops the connection too ─────────────────────────────
Discarding a vendor frame to make room would break the customer's SmartESS
session silently, and nobody could diagnose it from the app. Failing loudly
is the lesser harm.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto

from .demux import Frame

DEFAULT_TXN_TIMEOUT_MS = 3000
DEFAULT_MAX_QUEUED = 16


class State(Enum):
    IDLE = auto()
    CLOUD_PENDING = auto()
    OURS_PENDING = auto()
    DESYNCED = auto()


class ActionKind(Enum):
    SEND_TO_COLLECTOR = auto()
    SEND_TO_CLOUD = auto()
    RESOLVE_OURS = auto()
    FAIL_OURS = auto()
    DROP_COLLECTOR = auto()


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    payload: bytes = b""
    reason: str | None = None


class SchedulerBusy(Exception):
    """A second own request was made while one is already outstanding."""


@dataclass
class _Queued:
    payload: bytes
    ours: bool


@dataclass
class TxnScheduler:
    txn_timeout_ms: int = DEFAULT_TXN_TIMEOUT_MS
    max_queued: int = DEFAULT_MAX_QUEUED

    _state: State = field(default=State.IDLE, init=False)
    _deadline_ms: int | None = field(default=None, init=False)
    _queue: deque[_Queued] = field(default_factory=deque, init=False)
    _own_outstanding: bool = field(default=False, init=False)

    @property
    def state(self) -> State:
        return self._state

    def request(self, pdu: bytes, now_ms: int) -> list[Action]:
        """Offer one of our own requests.

        Returns the actions to perform. An empty list means the line was busy
        and the request is held; it goes out when the line frees.
        """
        if self._state is State.DESYNCED:
            raise SchedulerBusy("scheduler is desynchronised; reset after reconnect")
        if self._own_outstanding or any(q.ours for q in self._queue):
            raise SchedulerBusy("an own request is already in flight")
        self._queue.append(_Queued(payload=pdu, ours=True))
        return self._pump(now_ms)

    def on_cloud_frame(self, frame: Frame, now_ms: int) -> list[Action]:
        """A frame from the vendor cloud, bound for the collector."""
        if self._state is State.DESYNCED:
            return []
        if len(self._queue) >= self.max_queued:
            return self._desync("cloud queue overflow; refusing to discard a vendor frame")
        self._queue.append(_Queued(payload=frame.raw, ours=False))
        return self._pump(now_ms)

    def on_collector_frame(self, frame: Frame, now_ms: int) -> list[Action]:
        """A frame from the collector, answering whatever is outstanding."""
        if self._state is State.DESYNCED:
            return []
        if self._state is State.OURS_PENDING:
            actions = [Action(kind=ActionKind.RESOLVE_OURS, payload=frame.raw)]
        elif self._state is State.CLOUD_PENDING:
            actions = [Action(kind=ActionKind.SEND_TO_CLOUD, payload=frame.raw)]
        else:
            return self._desync("response with no outstanding request")
        self._own_outstanding = False
        self._state = State.IDLE
        self._deadline_ms = None
        return actions + self._pump(now_ms)

    def on_tick(self, now_ms: int) -> list[Action]:
        """Drive the timeout. Call often enough to notice one promptly."""
        if self._deadline_ms is None or now_ms <= self._deadline_ms:
            return []
        actions: list[Action] = []
        if self._state is State.OURS_PENDING:
            # Fail the waiter explicitly, or the poller blocks on a future that
            # nothing will ever resolve.
            actions.append(Action(kind=ActionKind.FAIL_OURS, reason="transaction timeout"))
        return actions + self._desync("transaction timeout")

    def reset(self) -> list[Action]:
        """Clear state for a fresh collector connection."""
        actions: list[Action] = []
        if self._own_outstanding or any(q.ours for q in self._queue):
            actions.append(Action(kind=ActionKind.FAIL_OURS, reason="collector connection closed"))
        self._state = State.IDLE
        self._deadline_ms = None
        self._own_outstanding = False
        self._queue.clear()
        return actions

    def _desync(self, reason: str) -> list[Action]:
        self._state = State.DESYNCED
        self._deadline_ms = None
        self._own_outstanding = False
        self._queue.clear()
        return [Action(kind=ActionKind.DROP_COLLECTOR, reason=reason)]

    def _pump(self, now_ms: int) -> list[Action]:
        """Send the next queued frame, if the line is free."""
        if self._state is not State.IDLE or not self._queue:
            return []
        item = self._queue.popleft()
        self._state = State.OURS_PENDING if item.ours else State.CLOUD_PENDING
        self._own_outstanding = item.ours
        self._deadline_ms = now_ms + self.txn_timeout_ms
        return [Action(kind=ActionKind.SEND_TO_COLLECTOR, payload=item.payload)]
