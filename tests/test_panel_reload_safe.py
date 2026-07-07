"""Reload-safety tests for register_panel (Fix 1) and register_views (Fix 3).

On a config-entry reload, hass.data[DOMAIN] is cleared so the in-memory guard
flags (_panel_registered / _views_registered) are lost. But the static path,
the sidebar panel, and the HTTP view routes are all GLOBAL to hass.http and
PERSIST across reloads. Re-registering them must therefore NOT raise.

Exact exceptions (verified against the HA/aiohttp source in the venv):
  - duplicate static path  -> RuntimeError  (aiohttp add_route: "Added route
    will never be executed, method GET is already registered")
  - duplicate custom panel -> ValueError    (frontend.async_register_built_in_panel:
    "Overwriting panel svitgrid")
  - duplicate HTTP view    -> RuntimeError  (same aiohttp add_route path)

Written BEFORE implementation (RED phase).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.panel import _PANEL_FLAG, register_panel

_DUP_ROUTE_ERR = RuntimeError(
    "Added route will never be executed, method GET is already registered"
)
_DUP_PANEL_ERR = ValueError("Overwriting panel svitgrid")


@pytest.mark.asyncio
async def test_register_panel_survives_already_registered_static_path_and_panel(hass):
    """Second register_panel after a reload (flag cleared, path+panel already
    present) must complete without raising and re-set the flag."""
    hass.http = MagicMock()
    # 1st call succeeds; 2nd call raises the aiohttp duplicate-route RuntimeError.
    hass.http.async_register_static_paths = AsyncMock(side_effect=[None, _DUP_ROUTE_ERR])
    with patch(
        "custom_components.svitgrid.panel.panel_custom.async_register_panel",
        new_callable=AsyncMock,
        side_effect=[None, _DUP_PANEL_ERR],
    ):
        await register_panel(hass)
        # Simulate an entry reload: hass.data[DOMAIN] is cleared, so the guard
        # flag is gone but the global registrations remain.
        hass.data.get(DOMAIN, {}).pop(_PANEL_FLAG, None)
        # Must NOT raise even though both registrations report "already registered".
        await register_panel(hass)

    assert hass.http.async_register_static_paths.await_count == 2
    assert hass.data[DOMAIN][_PANEL_FLAG] is True


@pytest.mark.asyncio
async def test_register_panel_reraises_unexpected_static_path_error(hass):
    """A non-"already registered" error must still propagate (no bare except)."""
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock(side_effect=OSError("disk gone"))
    with (
        patch(
            "custom_components.svitgrid.panel.panel_custom.async_register_panel",
            new_callable=AsyncMock,
        ),
        pytest.raises(OSError),
    ):
        await register_panel(hass)


@pytest.mark.asyncio
async def test_register_views_survives_already_registered_routes(hass):
    """Calling register_views twice (as happens on entry reload) must not raise
    the aiohttp 'already registered' RuntimeError."""
    from custom_components.svitgrid.http_views import register_views

    registered: list = []

    def _register_view(view):
        # First registration of each view type succeeds; a repeat raises like
        # aiohttp does for a duplicate GET route.
        key = type(view).__name__
        if key in registered:
            raise _DUP_ROUTE_ERR
        registered.append(key)

    hass.http = MagicMock()
    hass.http.register_view = MagicMock(side_effect=_register_view)

    store = object()
    register_views(hass, store)
    first_count = hass.http.register_view.call_count
    # Second pass — every view is already registered; must be swallowed.
    register_views(hass, store)
    assert hass.http.register_view.call_count == first_count * 2
