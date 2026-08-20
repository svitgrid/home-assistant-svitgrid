"""One listener, many collectors, routed by what each one says it is.

── Why a hub and not one link per inverter ───────────────────────────────
A household can have three Anenji. One `EybondAtLink` per inverter does not
work, and fails in two ways at once:

* **Port collision.** Every link defaults to TCP 8899, so the second and third
  raise `OSError: address already in use`. Verified, not assumed.
* **Announce collision.** Each link broadcasts `set>server=<ip>:<its port>` to
  the whole LAN, so all three collectors hear all three announces and each
  dials whichever it heard last. They would flap between servers indefinitely.

Giving each inverter its own port and a unicast announce would need every
collector's IP up front, and would break on a DHCP change.

So: **one listener, one announcer, N sessions.** The protocol's "one
transaction at a time" rule is per collector, not per network — three
collectors are three independent serialized lines, and `CollectorSession`
owns one each.

── How we decide which inverter a connection is ──────────────────────────
The collector says so. Every connection is identified before it is used:

    AT+DTUPN?    -> collector part number, e.g. I20000282044487591
    register 186 -> inverter serial, e.g. 99432604107106

An inverter's `harvest_config` records the serial it belongs to, and the hub
routes on that. A connection whose serial matches no configured inverter is
kept and reported — it is almost certainly a collector the user has not paired
yet — but nothing publishes from it.

**Never route by connection order or by IP.** Order is whatever the collectors
happen to do after a power cut, and a DHCP lease can move.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from .at_codec import PREFIX
from .identity import identify
from .session import CollectorSession, TransactionFailed

_LOGGER = logging.getLogger(__name__)

DEFAULT_LISTEN_PORT = 8899
ANNOUNCE_UDP_PORT = 58899
DEFAULT_ANNOUNCE_TARGET = "255.255.255.255"


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
class HubConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = DEFAULT_LISTEN_PORT
    announce_target: str = DEFAULT_ANNOUNCE_TARGET
    announce_port: int = ANNOUNCE_UDP_PORT
    announce_interval_s: float = 3.0
    # Overrides the auto-detected local address in the announce. REQUIRED when
    # Home Assistant runs in a bridge-mode container: `default_local_ip` then
    # returns the CONTAINER's address, which the collector cannot reach, and
    # the only symptom is that nothing ever connects.
    advertised_ip: str | None = None
    upstream_host: str | None = None
    upstream_port: int | None = None
    upstream_backoff_initial_s: float = 5.0
    upstream_backoff_max_s: float = 60.0
    tick_interval_s: float = 0.25
    slave_id: int = 1
    txn_timeout_ms: int = 3000
    # How many collectors we expect. A LAN with more than this is a
    # misconfiguration worth reporting rather than silently serving.
    max_sessions: int = 8
    # How many we are configured to serve. Once that many are connected the
    # announce goes QUIET -- see `_send_announce` for why that is not optional.
    # 0 means "unknown", which keeps broadcasting; discovery uses that.
    expected_collectors: int = 0


class EybondAtHub:
    def __init__(
        self,
        config: HubConfig,
        *,
        ip_provider: Callable[[], str] = default_local_ip,
        clock: Callable[[], float] = time.monotonic,
        on_diag: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._ip_provider = ip_provider
        self._clock = clock
        self._on_diag = on_diag
        self._server: asyncio.AbstractServer | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._sessions: dict[str, CollectorSession] = {}
        # Addresses that have dialled in at least once. Lets a missing
        # collector be recalled by unicast without disturbing the others.
        self._known_addresses: set[str] = set()
        # Fired whenever a session is identified or lost. A harvest loop with
        # nothing to read waits on this instead of polling, so the FIRST
        # reading lands as soon as the collector is identified rather than a
        # poll cadence later.
        self._changed = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._actual_port: int | None = None

    # ── properties ────────────────────────────────────────────────────────
    @property
    def listen_port(self) -> int | None:
        return self._actual_port

    @property
    def sessions(self) -> list[CollectorSession]:
        return list(self._sessions.values())

    @property
    def collector_count(self) -> int:
        return len(self._sessions)

    @property
    def upstream_target(self) -> tuple[str, int] | None:
        if self._config.upstream_host and self._config.upstream_port:
            return self._config.upstream_host, self._config.upstream_port
        return None

    def session_for(self, serial: str | None) -> CollectorSession | None:
        """The session belonging to this inverter serial, if it is connected."""
        if not serial:
            return None
        for session in self._sessions.values():
            if session.serial == serial:
                return session
        return None

    async def wait_for_change(self, limit_s: float) -> bool:
        """Block until a session is identified or lost, or `timeout` elapses.

        Returns True when something changed. Lets a caller react to a
        collector arriving instead of discovering it on the next poll tick --
        the difference between a first reading in seconds and one a full
        cadence later.
        """
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), limit_s)
            return True
        except TimeoutError:
            return False

    def _signal_change(self) -> None:
        self._changed.set()

    def unclaimed(self) -> list[CollectorSession]:
        """Identified sessions that no configured inverter has claimed.

        These are what a pairing flow offers the user to choose from.
        """
        return [s for s in self._sessions.values() if s.identity is not None]

    def _diag(self, reason: str) -> None:
        _LOGGER.debug("eybond_at hub: %s", reason)
        if self._on_diag:
            self._on_diag(reason)

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_connection, self._config.listen_host, self._config.listen_port
        )
        self._actual_port = self._server.sockets[0].getsockname()[1]
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, family=socket.AF_INET, allow_broadcast=True
        )
        self._udp = transport
        self._tasks.append(asyncio.create_task(self._announce_loop()))
        self._tasks.append(asyncio.create_task(self._tick_loop()))
        _LOGGER.info("EyBond hub listening on port %s", self._actual_port)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        for session in list(self._sessions.values()):
            await session.close("hub stopped")
        self._sessions.clear()
        if self._udp:
            self._udp.close()
            self._udp = None
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    # ── announce ──────────────────────────────────────────────────────────
    async def _announce_loop(self) -> None:
        while True:
            self._send_announce()
            await asyncio.sleep(self._config.announce_interval_s)

    def _send_announce(self) -> None:
        """Announce only while a collector we expect is missing.

        ── Why this is not "announce continuously" ───────────────────────
        MEASURED 2026-08-20 on real hardware: a collector that receives
        `set>server=` while already connected **redials**. Announcing every
        3 s produced **18 reconnects in 45 seconds**; announcing only while
        disconnected produced **one**, held for the whole window.

        An earlier version of this docstring asserted the opposite -- that a
        connected collector "re-reads the same address and stays put" -- which
        was an assumption, and wrong.

        ── Why it is not "stop after the first" either ───────────────────
        With three on the LAN, going quiet after one connects leaves the other
        two unable to find us. So:

        * fewer connected than expected, and someone has never been seen ->
          BROADCAST, because an unknown collector has no address to unicast to;
        * fewer connected than expected, but every address is known ->
          UNICAST to just the missing ones, so the connected ones are not
          disturbed by an announce meant for someone else;
        * all expected connected -> silence.
        """
        if self._udp is None or self._actual_port is None:
            return

        expected = self._config.expected_collectors
        connected = {s.address for s in self._sessions.values()}
        if expected and len(self._sessions) >= expected:
            return  # everyone is here; announcing would only cause redials

        our_ip = self._config.advertised_ip or self._ip_provider()
        command = f"set>server={our_ip}:{self._actual_port};".encode()

        missing_known = self._known_addresses - connected
        # Broadcast only while someone we have never met is still missing.
        broadcast_needed = not expected or len(self._known_addresses) < expected
        targets = list(missing_known)
        if broadcast_needed:
            # announce_target may name SEVERAL addresses. Where broadcast
            # cannot reach the LAN, onboarding scans for candidates and we
            # unicast to all of them rather than making the user identify
            # which one is the collector.
            targets.extend(
                a.strip()
                for a in self._config.announce_target.split(",")
                if a.strip() and a.strip() not in targets
            )

        for target in targets:
            with contextlib.suppress(OSError):
                self._udp.sendto(command, (target, self._config.announce_port))

    # ── connections ───────────────────────────────────────────────────────
    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        address = peer[0] if peer else "unknown"
        key = f"{address}:{peer[1]}" if peer else str(id(writer))

        if len(self._sessions) >= self._config.max_sessions:
            _LOGGER.warning(
                "refusing collector %s: already serving %d, max_sessions=%d",
                address,
                len(self._sessions),
                self._config.max_sessions,
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        # Deliberately NO dedupe here. We cannot know which collector this is
        # until it has been identified, and two collectors can share a source
        # address -- they do on loopback, and they would behind NAT. Dedupe
        # happens in `_identify`, keyed on the serial the DEVICE reports.

        session = CollectorSession(
            writer=writer,
            address=address,
            slave_id=self._config.slave_id,
            txn_timeout_ms=self._config.txn_timeout_ms,
            clock=self._clock,
            on_diag=self._on_diag,
        )
        self._sessions[key] = session
        self._known_addresses.add(address)
        self._diag(f"collector connected from {address}")

        if self._config.upstream_host and self._config.upstream_port:
            session._upstream_task = asyncio.create_task(self._upstream_loop(session))
        identify_task = asyncio.create_task(self._identify(session))
        try:
            await self._pump(reader, session)
        finally:
            identify_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await identify_task
            await session.close("collector disconnected")
            self._sessions.pop(key, None)
            self._signal_change()

    async def _identify(self, session: CollectorSession) -> None:
        """Read the identity block so the hub can route this connection.

        Runs concurrently with the pump, because the pump is what feeds the
        responses this needs.
        """
        try:
            identity = await identify(session)
            session.identity = identity
            _LOGGER.info(
                "collector %s is inverter %s (protocol %d, firmware %s)",
                session.address,
                identity.serial,
                identity.protocol_number,
                identity.firmware or "unknown",
            )
            await self._drop_stale_duplicates(session)
            self._signal_change()
        except asyncio.CancelledError:
            raise
        except TransactionFailed as err:
            # A collector that cannot be identified stays connected and simply
            # routes nowhere. Raising here would surface as an unhandled
            # background-task exception and tell the user nothing useful.
            _LOGGER.warning("collector %s did not answer identification: %s", session.address, err)
        except Exception:
            _LOGGER.exception("identify failed for collector %s", session.address)

    async def _drop_stale_duplicates(self, session: CollectorSession) -> None:
        """Close any OTHER session reporting the same inverter serial.

        A collector that redials before we noticed the old socket die leaves
        two connections claiming one inverter. Keeping both would let two
        harvest loops poll the same serialized line, and the newer connection
        is the live one.

        Keyed on the serial the DEVICE reports, never on its address: two
        collectors can share a source address, and one collector's address can
        change under DHCP.
        """
        for key, other in list(self._sessions.items()):
            if other is session or other.serial != session.serial:
                continue
            _LOGGER.info(
                "inverter %s redialled from %s; dropping the older session",
                session.serial,
                session.address,
            )
            await other.close("superseded by a newer connection")
            self._sessions.pop(key, None)

    async def _pump(self, reader: asyncio.StreamReader, session) -> None:
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                if not await session.feed(data):
                    return
        except (ConnectionError, asyncio.CancelledError):
            return

    # ── upstream, best-effort and isolated per session ────────────────────
    async def _upstream_loop(self, session: CollectorSession) -> None:
        """A dead vendor cloud must NEVER tear down a collector session.

        See `docs/anenji-eybond-at.md`. With three collectors this matters
        more, not less: one unreachable cloud must not take the other two
        inverters down with it.
        """
        backoff = self._config.upstream_backoff_initial_s
        while session.connected:
            try:
                reader, writer = await asyncio.open_connection(
                    self._config.upstream_host, self._config.upstream_port
                )
            except OSError as err:
                self._diag(f"upstream connect failed ({err}); collector unaffected")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._config.upstream_backoff_max_s)
                continue
            session.attach_upstream(writer)
            backoff = self._config.upstream_backoff_initial_s
            self._diag("upstream connected")
            with contextlib.suppress(ConnectionError, asyncio.CancelledError):
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    if not await session.feed_upstream(data):
                        break
            self._diag("upstream closed; collector unaffected")
            session.attach_upstream(None)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            await asyncio.sleep(backoff)

    # ── tick ──────────────────────────────────────────────────────────────
    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.tick_interval_s)
            for session in list(self._sessions.values()):
                await session.tick()


__all__ = [
    "ANNOUNCE_UDP_PORT",
    "DEFAULT_ANNOUNCE_TARGET",
    "DEFAULT_LISTEN_PORT",
    "PREFIX",
    "EybondAtHub",
    "HubConfig",
    "TransactionFailed",
    "default_local_ip",
]
