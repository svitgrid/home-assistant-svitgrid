from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.panel import register_panel, remove_panel


@pytest.mark.asyncio
async def test_register_panel_serves_module_and_registers(hass):
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    with patch(
        "custom_components.svitgrid.panel.panel_custom.async_register_panel", new_callable=AsyncMock
    ) as reg:
        await register_panel(hass)
    # both static paths registered in one call: the panel module + the helper
    sp_calls = hass.http.async_register_static_paths.await_args.args[0]
    urls = {c.url_path for c in sp_calls}
    assert "/svitgrid_panel/svitgrid-panel.js" in urls
    assert "/svitgrid_panel/history_periods.js" in urls
    panel_cfg = next(c for c in sp_calls if c.url_path.endswith("svitgrid-panel.js"))
    assert panel_cfg.path.endswith("panel_assets/svitgrid-panel.js")
    helper_cfg = next(c for c in sp_calls if c.url_path.endswith("history_periods.js"))
    assert helper_cfg.path.endswith("panel_assets/history_periods.js")
    # panel registered with the right identity
    kw = reg.await_args.kwargs
    assert kw["frontend_url_path"] == "svitgrid"
    assert kw["webcomponent_name"] == "svitgrid-panel"
    # module_url is the static path plus a content-hash cache-buster so browsers
    # re-fetch the module after each add-on update (static path stays bare).
    assert kw["module_url"].startswith("/svitgrid_panel/svitgrid-panel.js?h=")


@pytest.mark.asyncio
async def test_register_panel_is_idempotent(hass):
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    with patch(
        "custom_components.svitgrid.panel.panel_custom.async_register_panel", new_callable=AsyncMock
    ) as reg:
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
