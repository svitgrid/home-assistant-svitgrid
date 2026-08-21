"""Home Assistant should not ask a user for Home Assistant's own address.

The Network settings form exists because a bridge-mode container reports a
container address (172.17.0.2) that no collector can reach. But the user is
filling that form IN a browser, over HTTP, having typed the very address it is
asking for. HA exposes that request, so the answer is already in hand.

The collector address is derivable too: given the LAN address we know the /24,
and the hub already unicasts to a comma-separated target list -- so we can
announce across the subnet and let whoever answers show up in the picker,
instead of making the user identify which host is an inverter.
"""

from __future__ import annotations

import pytest

from custom_components.svitgrid.eybond_at.setup import (
    lan_ip_from_host_header,
    subnet_announce_targets,
)


class TestLanIpFromHostHeader:
    def test_takes_the_address_the_user_reached_home_assistant_on(self):
        assert lan_ip_from_host_header("192.168.1.34:8123") == "192.168.1.34"

    def test_a_bare_address_without_a_port_works(self):
        assert lan_ip_from_host_header("192.168.1.34") == "192.168.1.34"

    @pytest.mark.parametrize(
        "host", ["localhost:8123", "homeassistant.local:8123", "ha.example.com"]
    )
    def test_refuses_a_hostname(self, host):
        # A name is not usable: the collector is told a literal address, and
        # resolving here would happily produce a PUBLIC address that no
        # inverter on the LAN can dial.
        assert lan_ip_from_host_header(host) is None

    @pytest.mark.parametrize("host", ["127.0.0.1:8123", "[::1]:8123"])
    def test_refuses_loopback(self, host):
        # Reaching HA on loopback means the browser is on the HA host itself.
        # The address is real but tells us nothing a collector can use.
        assert lan_ip_from_host_header(host) is None

    @pytest.mark.parametrize("host", ["172.17.0.2:8123", "10.88.0.4:8123"])
    def test_refuses_a_container_address(self, host):
        # The whole point. Accepting this reintroduces the silent failure the
        # form exists to prevent: announce sent, nothing dials back, no error.
        assert lan_ip_from_host_header(host) is None

    def test_refuses_a_public_address(self):
        # Reached over a reverse proxy or a tunnel. Announcing a public
        # address to a collector produces a connection that never arrives.
        assert lan_ip_from_host_header("8.8.8.8:8123") is None
        # `is_private` is TRUE for the documentation ranges, so this is the
        # case that makes the RFC1918 check explicit rather than convenient.
        assert lan_ip_from_host_header("203.0.113.10:8123") is None

    def test_refuses_a_tailscale_address(self):
        # The sharp one. 100.64/10 is reachable by the BROWSER and not by an
        # inverter on the LAN, so it is exactly the address that would produce
        # a silent failure -- announce sent, nothing ever dials back.
        assert lan_ip_from_host_header("100.126.1.1:8123") is None

    def test_accepts_a_10_dot_lan(self):
        assert lan_ip_from_host_header("10.0.5.20:8123") == "10.0.5.20"

    def test_refuses_172_16_even_though_it_is_a_legitimate_lan(self):
        """A deliberate trade, not an oversight.

        Docker's default bridge sits inside 172.16/12, and we cannot tell a
        real 172.16 LAN from a container network from in here. Guessing wrong
        fails SILENTLY; asking is merely mildly annoying. So a user on a
        genuine 172.16 network still gets the form.
        """
        assert lan_ip_from_host_header("172.16.4.9:8123") is None

    def test_none_and_junk_are_not_a_crash(self):
        for host in (None, "", ":", "not a host", "999.1.1.1:8123"):
            assert lan_ip_from_host_header(host) is None


class TestSubnetAnnounceTargets:
    def test_covers_the_whole_24_as_a_comma_list(self):
        targets = subnet_announce_targets("192.168.1.34").split(",")
        assert "192.168.1.116" in targets
        assert "192.168.1.1" in targets
        assert "192.168.1.254" in targets

    def test_omits_network_broadcast_and_ourselves(self):
        # Ourselves because announcing to Home Assistant is pointless; the
        # other two because they are not hosts.
        targets = subnet_announce_targets("192.168.1.34").split(",")
        for absent in ("192.168.1.0", "192.168.1.255", "192.168.1.34"):
            assert absent not in targets
        assert len(targets) == 253

    def test_is_the_subnet_of_the_address_given(self):
        assert subnet_announce_targets("10.0.5.20").startswith("10.0.5.1,")

    def test_refuses_to_guess_from_nothing(self):
        assert subnet_announce_targets(None) == ""
        assert subnet_announce_targets("not-an-ip") == ""


class TestTheFormIsSkippedWhenWeCanAnswerItOurselves:
    """The flow-level half: derive, and do not ask."""

    async def test_a_container_flow_no_longer_asks_when_the_request_tells_us(self, hass):
        from unittest.mock import AsyncMock, MagicMock, patch

        from homeassistant.data_entry_flow import FlowResultType

        from custom_components.svitgrid.config_flow import SvitgridConfigFlow
        from custom_components.svitgrid.eybond_at.register_map import OutputMode
        from custom_components.svitgrid.eybond_at.setup import DiscoveredCollector

        flow = SvitgridConfigFlow()
        flow.hass = hass
        found = [
            DiscoveredCollector(
                serial="99432604107106",
                address="192.168.65.1",
                protocol_number=11,
                firmware="fw",
                output_mode=OutputMode.SINGLE,
            )
        ]
        request = MagicMock()
        request.headers = {"Host": "192.168.1.34:8123"}

        with (
            patch(
                "custom_components.svitgrid.config_flow.default_local_ip",
                new=MagicMock(return_value="172.17.0.2"),
            ),
            patch("custom_components.svitgrid.config_flow.current_request") as ctx,
            patch(
                "custom_components.svitgrid.config_flow.discover_collectors",
                new=AsyncMock(return_value=found),
            ),
        ):
            ctx.get.return_value = request
            result = await flow.async_step_eybond_collector()

        # Straight to the picker: the address question answered itself.
        assert result["step_id"] == "eybond_collector"
        assert result["type"] == FlowResultType.FORM
        assert flow._eybond_network is not None
        assert flow._eybond_network["advertised_ip"] == "192.168.1.34"
        # And the collector question too: announce across the subnet rather
        # than making the user say which host is an inverter.
        targets = flow._eybond_network["announce_target"].split(",")
        assert "192.168.1.116" in targets
        assert len(targets) == 253

    async def test_still_asks_when_the_request_cannot_answer_it(self, hass):
        """Browsing on the HA host itself gives Host: localhost."""
        from unittest.mock import MagicMock, patch

        from custom_components.svitgrid.config_flow import SvitgridConfigFlow

        flow = SvitgridConfigFlow()
        flow.hass = hass
        request = MagicMock()
        request.headers = {"Host": "localhost:8123"}

        with (
            patch(
                "custom_components.svitgrid.config_flow.default_local_ip",
                new=MagicMock(return_value="172.17.0.2"),
            ),
            patch("custom_components.svitgrid.config_flow.current_request") as ctx,
        ):
            ctx.get.return_value = request
            result = await flow.async_step_eybond_collector()

        assert result["step_id"] == "eybond_network"

    async def test_no_request_context_is_not_a_crash(self, hass):
        # Config flows can be resumed from a background task, where there is
        # no HTTP request in context at all.
        from unittest.mock import MagicMock, patch

        from custom_components.svitgrid.config_flow import SvitgridConfigFlow

        flow = SvitgridConfigFlow()
        flow.hass = hass

        with (
            patch(
                "custom_components.svitgrid.config_flow.default_local_ip",
                new=MagicMock(return_value="172.17.0.2"),
            ),
            patch("custom_components.svitgrid.config_flow.current_request") as ctx,
        ):
            ctx.get.return_value = None
            result = await flow.async_step_eybond_collector()

        assert result["step_id"] == "eybond_network"
