"""The pairing code is published to `hass.data` while a pairing is pending.

`/api/svitgrid/hello` reads it from there so the Svitgrid app can prefill the
six characters instead of asking the owner to read them off one screen and type
them into another. The window has to open when the code appears and close the
moment the pairing stops being pending — a code offered after that sends the
owner into a pairing that cannot finish, which looks exactly like the app being
broken.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from custom_components.svitgrid.const import DOMAIN


def _pending(hass: HomeAssistant):
    return (hass.data.get(DOMAIN) or {}).get("pending_pairing")


async def test_the_code_is_published_while_the_owner_is_looking_at_it(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    with patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={"secret": "s" * 40, "code": "7K9PA2", "expiresIn": 300}
        )
        # Never claims, so the flow stays on the waiting screen — which is
        # exactly the window the app is scanning in.
        mock_client.get_status = AsyncMock(side_effect=Exception("still waiting"))

        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        assert _pending(hass) == {"code": "7K9PA2"}


async def test_only_the_code_is_published(hass: HomeAssistant, enable_custom_integrations) -> None:
    """The view serving this is unauthenticated. The pairing SECRET is what
    finalizes a pairing — publishing it beside the code would let anyone on the
    network complete the pairing themselves."""
    with patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={"secret": "top-secret-value" * 3, "code": "7K9PA2", "expiresIn": 300}
        )
        mock_client.get_status = AsyncMock(side_effect=Exception("still waiting"))

        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        assert set(_pending(hass)) == {"code"}


async def test_the_code_stops_being_published_once_the_pairing_expires(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    from custom_components.svitgrid.pairing_client import PairingExpired

    async def _instant_sleep(_: float) -> None:
        pass

    with (
        patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls,
        patch("custom_components.svitgrid.config_flow.asyncio.sleep", side_effect=_instant_sleep),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={"secret": "s" * 40, "code": "7K9PA2", "expiresIn": 300}
        )
        mock_client.get_status = AsyncMock(side_effect=PairingExpired())

        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.async_block_till_done()

    assert not _pending(hass)


async def test_a_failing_cloud_poll_keeps_the_code_for_the_lan_until_the_window_ends(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Losing the cloud after /start ends only the cloud path. The code stays
    on offer, because a guest can still claim it over the LAN, and goes when
    the pairing window does."""
    import asyncio

    with (
        patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls,
        patch("custom_components.svitgrid.config_flow.PAIRING_POLL_INTERVAL_S", 0),
        patch("custom_components.svitgrid.config_flow.PAIRING_MAX_POLL_DURATION_S", 0.3),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={"secret": "s" * 40, "code": "7K9PA2", "expiresIn": 300}
        )
        mock_client.get_status = AsyncMock(side_effect=RuntimeError("boom"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await asyncio.sleep(0.05)
        assert _pending(hass) == {"code": "7K9PA2"}

        await asyncio.sleep(0.4)
        await hass.async_block_till_done()
        ended = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert ended["type"] == "abort"
    assert ended["reason"] == "pairing_expired"
    assert not _pending(hass)
