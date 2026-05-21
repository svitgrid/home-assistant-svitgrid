"""Tests for the Svitgrid config flow."""
from __future__ import annotations

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
