"""The options flow exposes an auto-update toggle that persists to options."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.const import CONF_AUTO_UPDATE, DOMAIN


@pytest.mark.asyncio
async def test_settings_step_persists_auto_update(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"inverters": []}, options={})
    entry.add_to_hass(hass)

    from custom_components.svitgrid.config_flow import SvitgridOptionsFlow

    flow = SvitgridOptionsFlow(entry)
    flow.hass = hass

    # Show the form.
    result = await flow.async_step_settings()
    assert result["type"] == "form"
    assert result["step_id"] == "settings"

    # Submit auto-update = False.
    result = await flow.async_step_settings({CONF_AUTO_UPDATE: False})
    assert result["type"] == "create_entry"
    assert result["data"][CONF_AUTO_UPDATE] is False
