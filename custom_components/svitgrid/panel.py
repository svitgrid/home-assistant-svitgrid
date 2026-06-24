"""Branded Svitgrid sidebar panel registration (Sub-project 2)."""
from __future__ import annotations

import os

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_PANEL_URL = "svitgrid"
_WEBCOMPONENT = "svitgrid-panel"
_MODULE_URL = "/svitgrid_panel/svitgrid-panel.js"
_PANEL_FLAG = "_panel_registered"


def _module_path() -> str:
    return os.path.join(os.path.dirname(__file__), "panel_assets", "svitgrid-panel.js")


async def register_panel(hass: HomeAssistant) -> None:
    """Serve the panel module and register the sidebar panel. Idempotent."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_PANEL_FLAG):
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_MODULE_URL, _module_path(), True)]
    )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=_PANEL_URL,
        webcomponent_name=_WEBCOMPONENT,
        sidebar_title="Svitgrid",
        sidebar_icon="mdi:solar-power",
        module_url=_MODULE_URL,
        require_admin=False,
    )
    domain_data[_PANEL_FLAG] = True


def remove_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar panel (best-effort; ignore if absent)."""
    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data.get(_PANEL_FLAG):
        return
    try:
        frontend.async_remove_panel(hass, _PANEL_URL)
    finally:
        domain_data[_PANEL_FLAG] = False
