"""Two different dead ends must not render the same screen.

"I cannot work out your address" and "I swept your network and nothing
answered" need opposite actions from the user, and both showed the same
Network settings form with the same Docker-networking advice. On 2026-08-21
that cost a debugging round: the collector had simply dropped off WiFi, and
the form said to go and fix container networking that was already correct.
"""

from __future__ import annotations

from custom_components.svitgrid.eybond_at.setup import (
    localhost_advice,
    no_collectors_advice,
)

CONTAINER_IP = "172.17.0.2"


class TestNothingAnsweredAfterASuccessfulSweep:
    def advice(self) -> str:
        return no_collectors_advice(
            CONTAINER_IP,
            announced_from="192.168.1.34",
            swept_subnet="192.168.1.0/24",
        )

    def test_says_what_was_announced_and_where(self):
        text = self.advice()
        assert "192.168.1.34" in text
        assert "192.168.1.0/24" in text

    def test_points_at_the_inverter_not_the_network(self):
        # The addresses are known and working. The remaining causes are the
        # device: unpowered, off WiFi, or on another subnet.
        text = self.advice().lower()
        assert "powered" in text

    def test_does_NOT_repeat_the_docker_advice(self):
        # The bug. local_ip is still a container address here -- it always is
        # in a bridge container -- so the old code short-circuited to "publish
        # a port and fill in the addresses below", advice for a problem that
        # has already been solved.
        text = self.advice()
        assert "--network=host" not in text
        assert "-p 8899:8899" not in text
        assert "172.17.0.2" not in text

    def test_does_NOT_ask_for_addresses_it_already_has(self):
        assert "fill in the addresses below" not in self.advice()
        assert "skip discovery" not in self.advice()


class TestNothingAnsweredWithNoDerivedAddress:
    def test_is_unchanged_when_we_never_worked_out_an_address(self):
        # Regression guard: the container path must still give container advice.
        text = no_collectors_advice(CONTAINER_IP)
        assert "8899" in text
        assert "172.17.0.2" in text

    def test_a_host_networking_install_still_gets_the_outcome_checklist(self):
        text = no_collectors_advice("192.168.1.34")
        assert "No collector answered" in text
        assert "192.168.1.34" in text


class TestBrowsedViaLocalhost:
    def test_names_the_actual_fix(self):
        # The cause the old text never mentioned: which URL the user typed.
        text = localhost_advice("localhost:8123")
        assert "localhost" in text
        assert "8123" in text

    def test_is_empty_when_the_host_was_usable(self):
        # Nothing to say when the header was fine.
        assert localhost_advice(None) == ""
        assert localhost_advice("192.168.1.34:8123") == ""


class TestTheFormShowsTheRightDeadEnd:
    """Flow level: the screen must match the cause."""

    async def test_a_failed_sweep_says_so_instead_of_asking_again(self, hass):
        from unittest.mock import AsyncMock, MagicMock, patch

        from custom_components.svitgrid.config_flow import SvitgridConfigFlow

        flow = SvitgridConfigFlow()
        flow.hass = hass
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
                new=AsyncMock(return_value=[]),
            ),
        ):
            ctx.get.return_value = request
            result = await flow.async_step_eybond_collector()

        assert result["step_id"] == "eybond_network"
        advice = result["description_placeholders"]["advice"]
        # It announced from a real address; say that, and point at the device.
        assert "192.168.1.34" in advice
        assert "powered" in advice.lower()
        # And do NOT re-explain container networking that is already working.
        assert "--network=host" not in advice

    async def test_a_localhost_browser_is_told_which_url_to_open(self, hass):
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
        advice = result["description_placeholders"]["advice"]
        assert "localhost" in advice
        assert "network address" in advice
