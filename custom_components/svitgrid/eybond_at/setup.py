"""Build a link from a `harvest_config`, and start the harvest loop.

Keeps `__init__.py` thin: it decides *whether* an inverter is an EyBond one
and calls `start_eybond_harvest`, and everything else lives here.

── Why local-only is the default ─────────────────────────────────────────
Relaying to the vendor cloud keeps the customer's SmartESS app working, and
we want that. But the vendor host is a property of the collector, not of us:
`AT+CLDSRVHOST1?` returned `dtu_ess.eybond.com,18899,TCP` on the bench unit,
and other collectors in this family are configured for `m2m.eybond.com` or
`iot.eybond.com`. Hardcoding one would relay some customers' traffic to a
cloud that is not theirs.

So the proxy is opt-in by explicit config, and `discover_upstream` reads the
endpoint from the device for callers that want to enable it automatically.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .harvest import run_eybond_harvest_loop
from .hub import (
    ANNOUNCE_UDP_PORT,
    DEFAULT_ANNOUNCE_TARGET,
    DEFAULT_LISTEN_PORT,
    EybondAtHub,
    HubConfig,
)
from .register_map import OutputMode
from .session import TransactionFailed

_LOGGER = logging.getLogger(__name__)

EYBOND_PROTOCOL = "eybond_at"
_MIN_PORT = 1
_MAX_PORT = 65535


class EybondConfigError(Exception):
    """A harvest_config for this protocol is unusable."""


def is_eybond_harvest(harvest_config: dict | None) -> bool:
    """True when this inverter is served by an EyBond/SmartESS collector."""
    return bool(harvest_config) and harvest_config.get("protocol") == EYBOND_PROTOCOL


def needs_inverter_ip(protocol: str | None) -> bool:
    """False for this family: the collector dials US, so there is no IP to dial.

    The manual config-flow step requires an IP for every other protocol. Asking
    for one here would be unanswerable -- the collector's address is not needed
    and is not even known until it connects.
    """
    return protocol != EYBOND_PROTOCOL


def needs_reachability_check(harvest_config: dict | None) -> bool:
    """False for this family, for the same reason.

    `check_inverter_reachable` TCP-connects to the inverter. Nothing here
    listens: we are the server. Running the probe would fail every pairing for
    a collector that is working perfectly.
    """
    return not is_eybond_harvest(harvest_config)


def build_manual_config(user_input: dict) -> dict:
    """Build a `harvest_config` for this family from the manual flow's input.

    Deliberately narrow: everything the collector path needs has a working
    default, so the pairing form stays short. The vendor relay is opt-in and
    OFF here -- `discover_upstream` can fill it in later from the device, which
    is safer than asking a user to type a cloud hostname.
    """
    config = {
        "protocol": EYBOND_PROTOCOL,
        "listen_port": int(user_input.get("port") or DEFAULT_LISTEN_PORT),
        "slave_id": int(user_input.get("slave_id") or 1),
        "model_id": (user_input.get("model_id") or "").strip(),
    }
    # The routing key. The hub matches it against the serial each collector
    # REPORTS at register 186 -- never connection order, never IP.
    serial = (user_input.get("inverter_serial") or "").strip()
    if serial:
        config["inverter_serial"] = serial
    return config


def _port(value, name: str, *, allow_ephemeral: bool = False) -> int:
    """Validate a port.

    `allow_ephemeral` permits 0, which means "let the OS choose". That is
    meaningful for OUR listener -- the announce advertises `link.listen_port`,
    the port actually bound -- and meaningless for a port we dial, where 0 is
    simply wrong.
    """
    try:
        port = int(value)
    except (TypeError, ValueError) as err:
        raise EybondConfigError(f"{name} is not a number: {value!r}") from err
    if allow_ephemeral and port == 0:
        return 0
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise EybondConfigError(f"{name} out of range: {port}")
    return port


def link_config_from(harvest_config: dict) -> HubConfig:
    """Translate ONE `harvest_config` into a `HubConfig`, validating as we go.

    Kept for the single-inverter case and for validation. With several
    inverters, `hub_config_from` reconciles them -- there is one hub, so
    conflicting listener settings have to be caught rather than silently
    resolved by whichever inverter is first in the list.
    """
    listen_port = _port(
        harvest_config.get("listen_port", DEFAULT_LISTEN_PORT),
        "listen_port",
        allow_ephemeral=True,
    )
    announce_port = _port(harvest_config.get("announce_port", ANNOUNCE_UDP_PORT), "announce_port")

    upstream_host = harvest_config.get("cloud_proxy_host") or None
    upstream_port = harvest_config.get("cloud_proxy_port")
    if upstream_host and upstream_port is None:
        # Half a proxy config silently disables the relay, and the symptom is
        # a dark SmartESS app with nothing in the log to explain it.
        raise EybondConfigError("cloud_proxy_host set without cloud_proxy_port")
    if upstream_port is not None:
        upstream_port = _port(upstream_port, "cloud_proxy_port")
        if not upstream_host:
            raise EybondConfigError("cloud_proxy_port set without cloud_proxy_host")

    slave_id = int(harvest_config.get("slave_id", 1))
    if not 0 <= slave_id <= 255:
        raise EybondConfigError(f"slave_id out of range: {slave_id}")

    return HubConfig(
        listen_host=harvest_config.get("listen_host", "0.0.0.0"),
        listen_port=listen_port,
        announce_target=harvest_config.get("announce_target", DEFAULT_ANNOUNCE_TARGET),
        announce_port=announce_port,
        advertised_ip=harvest_config.get("advertised_ip") or None,
        upstream_host=upstream_host,
        upstream_port=upstream_port,
        slave_id=slave_id,
    )


async def discover_upstream(link) -> tuple[str, int] | None:
    """Ask the collector where its own vendor cloud is.

    Returns `(host, port)`, or None when the collector cannot say. Best-effort
    by design: the harvest works without a proxy, so a failure here must never
    stop it.
    """
    try:
        reply = await link.at_query("CLDSRVHOST1")
    except TransactionFailed as err:
        _LOGGER.debug("vendor endpoint unavailable: %s", err)
        return None
    parts = [p.strip() for p in (reply or "").split(",")]
    if len(parts) != 3:
        return None
    host, port, transport = parts
    if not host:
        return None  # the empty secondary slot reads ",28899,TCP"
    if transport.upper() != "TCP":
        _LOGGER.debug("vendor endpoint is %s, not TCP; not relaying", transport)
        return None
    try:
        return host, _port(port, "vendor port")
    except EybondConfigError:
        return None


def hub_config_from(harvest_configs: list[dict]) -> HubConfig:
    """Reconcile several inverters' configs into the ONE hub they share.

    There is a single listener, so a disagreement about the listener settings
    cannot be resolved by taking whichever inverter happens to be first --
    that would silently ignore the other's configuration. Conflicts raise.

    Per-inverter settings (model, serial) are not the hub's business and are
    ignored here.
    """
    if not harvest_configs:
        raise EybondConfigError("no EyBond inverters to build a hub for")
    configs = [link_config_from(hc) for hc in harvest_configs]
    base = configs[0]
    for other in configs[1:]:
        for field_name in (
            "listen_host",
            "listen_port",
            "announce_target",
            "announce_port",
            "advertised_ip",
            "upstream_host",
            "upstream_port",
        ):
            mine, theirs = getattr(base, field_name), getattr(other, field_name)
            if mine != theirs:
                raise EybondConfigError(
                    f"EyBond inverters disagree on {field_name}: "
                    f"{mine!r} vs {theirs!r}. They share one listener."
                )
    # Room for every configured inverter, plus headroom for one that redials
    # before we noticed the old socket die.
    base.max_sessions = max(len(configs) + 2, HubConfig.max_sessions)
    return base


async def start_eybond_hub(
    *,
    hass,
    inverters: list[dict],
    store,
    cadence,
    lifecycle=None,
) -> tuple[EybondAtHub, dict[str, object]]:
    """Start ONE hub and a harvest loop per inverter. Returns `(hub, tasks)`.

    `inverters` are dicts with `inverter_id` and `harvest_config`.
    """
    hub = EybondAtHub(hub_config_from([inv["harvest_config"] for inv in inverters]))
    await hub.start()
    tasks: dict[str, object] = {}
    for inv in inverters:
        inverter_id = inv["inverter_id"]
        serial = inv["harvest_config"].get("inverter_serial")
        if not serial and len(inverters) > 1:
            # With one inverter the only connection is unambiguous. With
            # several, an unset serial would match nothing and the inverter
            # would silently never publish.
            _LOGGER.error(
                "inverter %s has no inverter_serial and is one of %d EyBond "
                "inverters; it cannot be routed and will not publish",
                inverter_id,
                len(inverters),
            )
        tasks[inverter_id] = hass.async_create_background_task(
            run_eybond_harvest_loop(
                hass=hass,
                hub=hub,
                inverter_serial=serial,
                store=store,
                cadence=cadence,
                inverter_id=inverter_id,
                lifecycle=lifecycle,
            ),
            name=f"svitgrid_eybond_{inverter_id}",
        )
    _LOGGER.info(
        "EyBond hub on port %s serving %d inverter(s) (vendor relay: %s)",
        hub.listen_port,
        len(inverters),
        hub.upstream_target or "off",
    )
    return hub, tasks


@dataclass(frozen=True)
class DiscoveredCollector:
    """One collector seen on the LAN, as a pairing form would show it."""

    serial: str
    address: str
    protocol_number: int
    output_mode: OutputMode
    firmware: str

    @property
    def label(self) -> str:
        """What the user reads in the picker.

        The topology is included because it is the thing a user can check
        against reality: if they wired one inverter per phase, three entries
        reading "Phase P1/P2/P3" confirm it, and three reading "Single" say
        the inverters have not been told they are a three-phase set.
        """
        topology = {
            OutputMode.SINGLE: "standalone",
            OutputMode.PARALLEL: "parallel bank",
            OutputMode.PHASE_P1: "phase L1",
            OutputMode.PHASE_P2: "phase L2",
            OutputMode.PHASE_P3: "phase L3",
            OutputMode.UNKNOWN: "topology unknown",
        }[self.output_mode]
        return f"{self.serial} — {topology} — {self.address}"


def snapshot_collectors(hub, *, exclude: set[str] | None = None) -> list[DiscoveredCollector]:
    """Identified collectors on `hub`, minus serials already configured.

    Excluding the configured ones is what makes adding the second and third
    inverter unambiguous: the list shows only what is still unclaimed.
    """
    exclude = exclude or set()
    found = []
    for session in hub.sessions:
        identity = session.identity
        if identity is None or identity.serial in exclude:
            continue
        found.append(
            DiscoveredCollector(
                serial=identity.serial,
                address=session.address,
                protocol_number=identity.protocol_number,
                output_mode=identity.output_mode,
                firmware=identity.firmware,
            )
        )
    return sorted(found, key=lambda c: c.serial)


async def discover_collectors(
    hass,
    *,
    running_hub=None,
    harvest_config: dict | None = None,
    settle_s: float = 12.0,
    exclude: set[str] | None = None,
    sleep=None,
) -> list[DiscoveredCollector]:
    """Find collectors on the LAN for a pairing form.

    **Reuses the running hub when there is one.** Starting a second listener
    would collide on the port AND its broadcast would yank already-working
    collectors onto a listener that is about to be torn down. That is the same
    `address already in use` that makes one-listener-per-inverter impossible.

    Only when no hub is running -- the first EyBond inverter on this
    installation -- does this open a temporary one.
    """
    waiter = sleep or asyncio.sleep
    if running_hub is not None:
        # Already serving. Collectors are connected or will be within one
        # announce interval, so give it a moment rather than answering "none".
        if not snapshot_collectors(running_hub, exclude=exclude):
            await waiter(settle_s)
        return snapshot_collectors(running_hub, exclude=exclude)

    hub = EybondAtHub(link_config_from(harvest_config or {"protocol": EYBOND_PROTOCOL}))
    await hub.start()
    try:
        await waiter(settle_s)
        return snapshot_collectors(hub, exclude=exclude)
    finally:
        await hub.stop()
