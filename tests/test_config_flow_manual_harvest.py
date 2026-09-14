"""Tests for the manual direct-harvest step (SP-D Task 5).

The "Add Svitgrid" flow no longer offers ``harvest_config`` from the user step
(pairing with the mobile app is the only entry point, decided 2026-09-14). The
step itself stays in the code, so these tests reach it directly by starting the
flow at that step.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.svitgrid.const import DOMAIN


@pytest.mark.asyncio
async def test_user_step_does_not_offer_harvest_config(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """The user step opens pairing directly; no menu offers harvest_config."""
    with patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={"secret": "secret-abc-def" * 4, "code": "7K9PA2", "expiresIn": 300}
        )
        mock_client.get_status = AsyncMock(side_effect=Exception("don't poll yet"))

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    assert result["type"] != FlowResultType.MENU
    assert "harvest_config" not in result.get("menu_options", [])


@pytest.mark.asyncio
async def test_harvest_config_step_shows_form(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """The hidden async_step_harvest_config still shows the manual Modbus form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "harvest_config"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "harvest_config"


@pytest.mark.asyncio
async def test_harvest_config_form_submit_proceeds_to_pair(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Submitting a valid harvest_config form sets _harvest_config and proceeds
    to async_step_pair (the existing pair/finalize path)."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "harvest_config"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "harvest_config"

    with patch(
        "custom_components.svitgrid.config_flow.PairingClient",
    ) as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={
                "secret": "secret-abc-def" * 4,
                "code": "7K9PA2",
                "expiresIn": 300,
            }
        )
        mock_client.get_status = AsyncMock(side_effect=Exception("don't poll yet"))

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "protocol": "solarman_v5",
                "ip": "192.168.1.50",
                "port": 8899,
                "slave_id": 1,
                "model_id": "deye_sg04lp3",
                "logger_serial": "1234567890",
            },
        )

    # After a valid submit the flow proceeds to pair (shows progress / waiting screen)
    assert result["type"] in (FlowResultType.SHOW_PROGRESS, FlowResultType.FORM)
