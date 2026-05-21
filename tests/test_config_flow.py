"""Tests for the Svitgrid config flow."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.svitgrid.const import DOMAIN


@pytest.mark.asyncio
async def test_user_step_shows_menu(hass: HomeAssistant, enable_custom_integrations) -> None:
    """The user step should present the Pair vs Manual menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.MENU
    assert "pair" in result["menu_options"]
    assert "manual" in result["menu_options"]


@pytest.mark.asyncio
async def test_pair_step_calls_start_and_shows_code(hass: HomeAssistant, enable_custom_integrations) -> None:
    """Selecting Pair calls /ha-pairing/start and shows the 6-char code."""
    with patch(
        "custom_components.svitgrid.config_flow.PairingClient",
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(return_value={
            "secret": "secret-abc-def" * 4,
            "code": "7K9PA2",
            "expiresIn": 300,
        })
        # Block status forever so we stay on the waiting screen
        mock_client.get_status = AsyncMock(side_effect=Exception("don't poll yet"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )

        assert result["type"] == FlowResultType.SHOW_PROGRESS
        assert "7K9PA2" in str(result.get("description_placeholders", {}))


@pytest.mark.asyncio
async def test_pair_finalize_creates_entry(hass: HomeAssistant, enable_custom_integrations) -> None:
    """When polling returns claimed, finalize runs and an entry is created."""
    from custom_components.svitgrid.pairing_client import PairingClaimed
    from cryptography.hazmat.primitives.asymmetric import ec

    fake_priv = ec.generate_private_key(ec.SECP256R1())

    async def _instant_sleep(_: float) -> None:
        """Replace asyncio.sleep with a no-op so the poll loop runs immediately."""

    with patch(
        "custom_components.svitgrid.config_flow.PairingClient",
    ) as mock_client_cls, patch(
        "custom_components.svitgrid.config_flow.generate_keypair",
        return_value=(fake_priv, "04" + "a" * 128),
    ), patch(
        "custom_components.svitgrid.config_flow.asyncio.sleep",
        side_effect=_instant_sleep,
    ):

        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(return_value={
            "secret": "secret-1", "code": "7K9PA2", "expiresIn": 300,
        })
        mock_client.get_status = AsyncMock(return_value=PairingClaimed(
            household_id="h-abc", preset_id=None,
        ))
        mock_client.finalize = AsyncMock(return_value={
            "edgeDeviceId": "ed-1", "hardwareId": "ha-xyz",
            "apiKey": "test-key", "householdId": "h-abc", "presetId": None,
            "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
        })

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "pair"}
        )
        # SHOW_PROGRESS → eventually CREATE_ENTRY after the polling loop sees claimed.
        await hass.async_block_till_done()
        entries = hass.config_entries.async_entries(DOMAIN)
        assert len(entries) == 1
        assert entries[0].data["api_key"] == "test-key"
        assert entries[0].data["household_id"] == "h-abc"
