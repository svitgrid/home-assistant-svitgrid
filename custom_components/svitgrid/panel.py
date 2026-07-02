"""Branded Svitgrid sidebar panel registration (Sub-project 2)."""
from __future__ import annotations

import hashlib
import logging
import os

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_PANEL_URL = "svitgrid"
_WEBCOMPONENT = "svitgrid-panel"
_MODULE_URL = "/svitgrid_panel/svitgrid-panel.js"
_PANEL_FLAG = "_panel_registered"


def _is_already_registered(err: Exception) -> bool:
    """True if the error is HA/aiohttp complaining a global registration
    (static path route or sidebar panel) already exists — expected after an
    entry reload, since these registrations persist on hass.http while the
    in-memory guard flag in hass.data[DOMAIN] is cleared on unload."""
    return "already registered" in str(err) or "Overwriting panel" in str(err)


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
    # The static path and the sidebar panel are GLOBAL to hass.http and PERSIST
    # across config-entry reloads, but the _PANEL_FLAG guard lives in
    # hass.data[DOMAIN] which is cleared on unload. So on a reload we reach here
    # with the guard gone but the registrations still present — a re-register
    # raises "already registered" (aiohttp RuntimeError) / "Overwriting panel"
    # (frontend ValueError). Treat those as a no-op so setup doesn't fail.
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_MODULE_URL, _module_path(), True)]
        )
    except (RuntimeError, ValueError) as err:
        if not _is_already_registered(err):
            raise
        _LOGGER.debug("Reusing already-registered panel static path: %s", err)
    # Static path stays at the bare URL (the static server ignores the query);
    # the panel loads a content-hashed URL so each build busts the browser cache.
    digest = await hass.async_add_executor_job(_module_hash)
    module_url = f"{_MODULE_URL}?h={digest}" if digest else _MODULE_URL
    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=_PANEL_URL,
            webcomponent_name=_WEBCOMPONENT,
            sidebar_title="Svitgrid",
            sidebar_icon="mdi:solar-power",
            module_url=module_url,
            require_admin=False,
        )
    except (RuntimeError, ValueError) as err:
        if not _is_already_registered(err):
            raise
        _LOGGER.debug("Reusing already-registered sidebar panel: %s", err)
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
