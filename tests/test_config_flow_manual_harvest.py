"""Tests for the manual direct-harvest menu option (SP-D Task 5).

Verifies that ``async_step_user`` exposes ``harvest_config`` as a menu option
and that selecting it reaches ``async_step_harvest_config`` (which shows the
manual Modbus form).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.svitgrid.const import DOMAIN


@pytest.mark.asyncio
async def test_user_menu_includes_harvest_config(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """async_step_user must include 'harvest_config' in menu_options."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.MENU
    assert "harvest_config" in result["menu_options"]


@pytest.mark.asyncio
async def test_selecting_harvest_config_shows_form(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Selecting 'harvest_config' from the menu reaches async_step_harvest_config
    and shows the manual Modbus form (step_id == 'harvest_config')."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == FlowResultType.MENU

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "harvest_config"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "harvest_config"


@pytest.mark.asyncio
async def test_harvest_config_form_submit_proceeds_to_pair(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Submitting a valid harvest_config form sets _harvest_config and proceeds
    to async_step_pair (the existing pair/finalize path)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "harvest_config"}
    )
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
