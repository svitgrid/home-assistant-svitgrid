"""Branded Svitgrid sidebar panel registration (Sub-project 2)."""
from __future__ import annotations

import hashlib
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


def _module_hash() -> str:
    """Short content hash of the panel JS, for cache-busting the module URL.

    HA serves the module at a fixed path with cache headers, so without a
    content-keyed query the browser keeps the previously-cached module after an
    add-on update. Hashing the file content means each new build gets a fresh
    URL the browser must re-fetch. Returns "" if the file can't be read (the
    caller then falls back to the bare URL — no worse than before)."""
    try:
        with open(_module_path(), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:12]
    except OSError:
        return ""


async def register_panel(hass: HomeAssistant) -> None:
    """Serve the panel module and register the sidebar panel. Idempotent."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(_PANEL_FLAG):
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_MODULE_URL, _module_path(), True)]
    )
    # Static path stays at the bare URL (the static server ignores the query);
    # the panel loads a content-hashed URL so each build busts the browser cache.
    digest = await hass.async_add_executor_job(_module_hash)
    module_url = f"{_MODULE_URL}?h={digest}" if digest else _MODULE_URL
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=_PANEL_URL,
        webcomponent_name=_WEBCOMPONENT,
        sidebar_title="Svitgrid",
        sidebar_icon="mdi:solar-power",
        module_url=module_url,
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
