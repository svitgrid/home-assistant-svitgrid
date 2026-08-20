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

import logging

from .harvest import run_eybond_harvest_loop
from .link import (
    ANNOUNCE_UDP_PORT,
    DEFAULT_ANNOUNCE_TARGET,
    DEFAULT_LISTEN_PORT,
    EybondAtLink,
    LinkConfig,
    TransactionFailed,
)
from .reader import EybondAtReader

_LOGGER = logging.getLogger(__name__)

EYBOND_PROTOCOL = "eybond_at"
_MIN_PORT = 1
_MAX_PORT = 65535


class EybondConfigError(Exception):
    """A harvest_config for this protocol is unusable."""


def is_eybond_harvest(harvest_config: dict | None) -> bool:
    """True when this inverter is served by an EyBond/SmartESS collector."""
    return bool(harvest_config) and harvest_config.get("protocol") == EYBOND_PROTOCOL


def _port(value, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as err:
        raise EybondConfigError(f"{name} is not a number: {value!r}") from err
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise EybondConfigError(f"{name} out of range: {port}")
    return port


def link_config_from(harvest_config: dict) -> LinkConfig:
    """Translate a `harvest_config` into a `LinkConfig`, validating as we go."""
    listen_port = _port(harvest_config.get("listen_port", DEFAULT_LISTEN_PORT), "listen_port")
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

    return LinkConfig(
        listen_host=harvest_config.get("listen_host", "0.0.0.0"),
        listen_port=listen_port,
        announce_target=harvest_config.get("announce_target", DEFAULT_ANNOUNCE_TARGET),
        announce_port=announce_port,
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


async def start_eybond_harvest(
    *,
    hass,
    harvest_config: dict,
    inverter_id: str,
    store,
    cadence,
    lifecycle=None,
) -> tuple[EybondAtLink, object]:
    """Start the listener and the harvest loop. Returns `(link, task)`."""
    link = EybondAtLink(link_config_from(harvest_config))
    await link.start()
    reader = EybondAtReader(link)
    task = hass.async_create_background_task(
        run_eybond_harvest_loop(
            hass=hass,
            link=link,
            reader=reader,
            store=store,
            cadence=cadence,
            inverter_id=inverter_id,
            lifecycle=lifecycle,
        ),
        name=f"svitgrid_eybond_{inverter_id}",
    )
    _LOGGER.info(
        "EyBond listener for inverter %s on port %s (vendor relay: %s)",
        inverter_id,
        link.listen_port,
        link.upstream_target or "off",
    )
    return link, task
