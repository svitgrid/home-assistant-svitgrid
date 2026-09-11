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

import pytest
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

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
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

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )

        assert set(_pending(hass)) == {"code"}


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("expired", id="the pairing window ran out"),
        pytest.param("failed", id="polling failed"),
    ],
)
async def test_the_code_stops_being_published_once_the_pairing_ends(
    hass: HomeAssistant, enable_custom_integrations, failure: str
) -> None:
    from custom_components.svitgrid.pairing_client import PairingExpired

    async def _instant_sleep(_: float) -> None:
        pass

    error = PairingExpired() if failure == "expired" else RuntimeError("boom")

    with (
        patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls,
        patch("custom_components.svitgrid.config_flow.asyncio.sleep", side_effect=_instant_sleep),
    ):
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={"secret": "s" * 40, "code": "7K9PA2", "expiresIn": 300}
        )
        mock_client.get_status = AsyncMock(side_effect=error)

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )
        await hass.async_block_till_done()

    assert not _pending(hass)
