"""Turning a harvest_config into a running link, and reading the vendor endpoint."""

import asyncio
import contextlib
from dataclasses import dataclass, field

import pytest

from custom_components.svitgrid.eybond_at.link import (
    ANNOUNCE_UDP_PORT,
    DEFAULT_LISTEN_PORT,
    TransactionFailed,
)
from custom_components.svitgrid.eybond_at.setup import (
    EYBOND_PROTOCOL,
    EybondConfigError,
    discover_upstream,
    is_eybond_harvest,
    link_config_from,
    start_eybond_harvest,
)


class TestDetection:
    def test_recognises_an_eybond_harvest_config(self):
        assert is_eybond_harvest({"protocol": EYBOND_PROTOCOL}) is True

    def test_ignores_the_solarman_and_modbus_paths(self):
        # Those go to harvest/transport.py, driven by a cloud RegisterSpec.
        assert is_eybond_harvest({"protocol": "solarman_v5"}) is False
        assert is_eybond_harvest({"protocol": "modbus_tcp"}) is False

    def test_ignores_an_absent_config(self):
        assert is_eybond_harvest(None) is False
        assert is_eybond_harvest({}) is False


class TestLinkConfig:
    def test_defaults_are_the_protocol_constants(self):
        config = link_config_from({"protocol": EYBOND_PROTOCOL})
        assert config.listen_port == DEFAULT_LISTEN_PORT
        assert config.announce_port == ANNOUNCE_UDP_PORT
        assert config.slave_id == 1

    def test_defaults_to_local_only_with_no_vendor_proxy(self):
        """Local-only by default, and that is a deliberate trade-off.

        Proxying keeps the customer's SmartESS app alive, but it needs the
        vendor host, which varies per collector and must not be hardcoded.
        `discover_upstream` reads it from the device instead.
        """
        config = link_config_from({"protocol": EYBOND_PROTOCOL})
        assert config.upstream_host is None
        assert config.upstream_port is None

    def test_carries_an_explicit_vendor_proxy(self):
        config = link_config_from(
            {
                "protocol": EYBOND_PROTOCOL,
                "cloud_proxy_host": "dtu_ess.eybond.com",
                "cloud_proxy_port": 18899,
            }
        )
        assert config.upstream_host == "dtu_ess.eybond.com"
        assert config.upstream_port == 18899

    def test_overrides_the_listen_port(self):
        config = link_config_from({"protocol": EYBOND_PROTOCOL, "listen_port": 9899})
        assert config.listen_port == 9899

    def test_rejects_a_proxy_host_with_no_port(self):
        # Half a proxy config silently disables the relay, and the symptom is
        # a dark SmartESS app with nothing in the log.
        with pytest.raises(EybondConfigError):
            link_config_from(
                {"protocol": EYBOND_PROTOCOL, "cloud_proxy_host": "dtu_ess.eybond.com"}
            )

    def test_rejects_a_port_outside_the_valid_range(self):
        for port in (-1, 65536):
            with pytest.raises(EybondConfigError):
                link_config_from({"protocol": EYBOND_PROTOCOL, "listen_port": port})

    def test_allows_an_ephemeral_listen_port(self):
        # 0 means "let the OS choose". The announce advertises the port that
        # was actually bound, so this works end to end.
        assert link_config_from({"protocol": EYBOND_PROTOCOL, "listen_port": 0}).listen_port == 0

    def test_still_rejects_an_ephemeral_port_for_a_port_we_dial(self):
        # 0 is meaningful for a port we BIND and meaningless for one we CONNECT
        # to. Accepting it there would produce a relay that silently never works.
        with pytest.raises(EybondConfigError):
            link_config_from(
                {
                    "protocol": EYBOND_PROTOCOL,
                    "cloud_proxy_host": "dtu_ess.eybond.com",
                    "cloud_proxy_port": 0,
                }
            )

    def test_rejects_a_slave_id_outside_modbus_range(self):
        with pytest.raises(EybondConfigError):
            link_config_from({"protocol": EYBOND_PROTOCOL, "slave_id": 256})


class FakeLink:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.queries: list[str] = []

    async def at_query(self, command: str, timeout_s: float = 3.0) -> str:
        self.queries.append(command)
        if self.error is not None:
            raise self.error
        return self.reply


class TestDiscoverUpstream:
    async def test_reads_the_vendor_endpoint_from_the_collector(self):
        """The collector knows its own cloud, so we never hardcode one.

        Captured 2026-08-20: `AT+CLDSRVHOST1:dtu_ess.eybond.com,18899,TCP`.
        """
        link = FakeLink(reply="dtu_ess.eybond.com,18899,TCP")
        assert await discover_upstream(link) == ("dtu_ess.eybond.com", 18899)
        assert link.queries == ["CLDSRVHOST1"]

    async def test_returns_none_for_the_empty_secondary_slot(self):
        # Captured: `AT+CLDSRVHOST2:,28899,TCP` -- a port with no host.
        assert await discover_upstream(FakeLink(reply=",28899,TCP")) is None

    async def test_returns_none_when_the_collector_refuses(self):
        assert await discover_upstream(FakeLink(reply="R001")) is None

    async def test_returns_none_on_a_transport_failure(self):
        # Discovery is best-effort: failing it must not stop the harvest.
        link = FakeLink(error=TransactionFailed("no collector connected"))
        assert await discover_upstream(link) is None

    async def test_returns_none_on_an_unparseable_reply(self):
        for reply in ("", "garbage", "host,notaport,TCP", "host"):
            assert await discover_upstream(FakeLink(reply=reply)) is None

    async def test_ignores_a_non_tcp_transport(self):
        # Only TCP is implemented; a UDP endpoint would be relayed wrongly.
        assert await discover_upstream(FakeLink(reply="host.example,18899,UDP")) is None


@dataclass
class FakeHass:
    is_stopping: bool = False
    tasks: list = field(default_factory=list)

    def async_create_background_task(self, coro, name=None):
        task = asyncio.create_task(coro, name=name)
        self.tasks.append(task)
        return task


class FakeStore:
    def __init__(self):
        self.appended = []

    async def append(self, payload):
        self.appended.append(payload)


@dataclass
class FakeCadence:
    interval_s: float = 5.0


class TestStartHarvest:
    """The factory owns a real listening socket, so its lifecycle is the risk."""

    async def test_starts_a_listener_and_returns_the_link_and_task(self, socket_enabled):
        hass = FakeHass()
        link, task = await start_eybond_harvest(
            hass=hass,
            harvest_config={
                "protocol": EYBOND_PROTOCOL,
                "listen_port": 0,  # ephemeral, so the test never fights port 8899
                "announce_target": "127.0.0.1",
            },
            inverter_id="inv1",
            store=FakeStore(),
            cadence=FakeCadence(),
        )
        try:
            assert link.listen_port and link.listen_port > 0
            assert link.collector_connected is False
            assert task in hass.tasks
        finally:
            hass.is_stopping = True
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await link.stop()

    async def test_stopping_the_link_releases_the_port(self, socket_enabled):
        """A reload that leaks the socket leaves 8899 bound and the next setup fails.

        Binding the same port a second time is the only way to prove it was
        actually released.
        """
        hass = FakeHass()
        link, task = await start_eybond_harvest(
            hass=hass,
            harvest_config={
                "protocol": EYBOND_PROTOCOL,
                "listen_port": 0,
                "announce_target": "127.0.0.1",
            },
            inverter_id="inv1",
            store=FakeStore(),
            cadence=FakeCadence(),
        )
        port = link.listen_port
        hass.is_stopping = True
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await link.stop()

        # Re-bind the very same port. This raises if the first link leaked it.
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
        server.close()
        await server.wait_closed()

    async def test_a_bad_config_raises_before_any_socket_is_opened(self):
        with pytest.raises(EybondConfigError):
            await start_eybond_harvest(
                hass=FakeHass(),
                harvest_config={"protocol": EYBOND_PROTOCOL, "listen_port": 0xFFFFF},
                inverter_id="inv1",
                store=FakeStore(),
                cadence=FakeCadence(),
            )
