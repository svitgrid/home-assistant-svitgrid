from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from custom_components.svitgrid.panel import register_panel, remove_panel
from custom_components.svitgrid.const import DOMAIN


@pytest.mark.asyncio
async def test_register_panel_serves_module_and_registers(hass):
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    with patch("custom_components.svitgrid.panel.panel_custom.async_register_panel",
               new_callable=AsyncMock) as reg:
        await register_panel(hass)
    # static path registered for the JS module
    sp_call = hass.http.async_register_static_paths.await_args.args[0][0]
    assert sp_call.url_path == "/svitgrid_panel/svitgrid-panel.js"
    assert sp_call.path.endswith("panel_assets/svitgrid-panel.js")
    # panel registered with the right identity
    kw = reg.await_args.kwargs
    assert kw["frontend_url_path"] == "svitgrid"
    assert kw["webcomponent_name"] == "svitgrid-panel"
    # module_url is the static path plus a content-hash cache-buster so browsers
    # re-fetch the module after each add-on update (static path stays bare).
    assert kw["module_url"].startswith("/svitgrid_panel/svitgrid-panel.js?h=")
    assert sp_call.url_path == "/svitgrid_panel/svitgrid-panel.js"


@pytest.mark.asyncio
async def test_register_panel_is_idempotent(hass):
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    with patch("custom_components.svitgrid.panel.panel_custom.async_register_panel",
               new_callable=AsyncMock) as reg:
        await register_panel(hass)
        await register_panel(hass)
    assert reg.await_count == 1
    assert hass.http.async_register_static_paths.await_count == 1


@pytest.mark.asyncio
async def test_remove_panel_calls_frontend(hass):
    hass.data.setdefault(DOMAIN, {})["_panel_registered"] = True
    with patch("custom_components.svitgrid.panel.frontend.async_remove_panel") as rm:
        remove_panel(hass)
    rm.assert_called_once_with(hass, "svitgrid")
