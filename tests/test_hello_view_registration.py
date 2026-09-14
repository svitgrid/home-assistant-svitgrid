"""The hello view must exist on an install that has never paired.

That is the only install it is FOR: the Svitgrid app sweeps the network during
onboarding, before there is any config entry. Registering it from
`async_setup` was not enough — Home Assistant calls that when the domain is
set up, and a domain with no config entry and no YAML block is never set up at
all. The integration is merely discovered, the view is never registered, and
`GET /api/svitgrid/hello` returns 404 on exactly the install it exists to
answer. (Observed on a fresh 2026.5.3 container, 2026-09-10.)

Opening the Svitgrid config flow DOES load the integration, and that is also
the moment the pairing code appears on screen — so the flow registers it too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.svitgrid.http_views import SvitgridHelloView, ensure_hello_view


def _hass_with_http():
    hass = MagicMock()
    hass.data = {}
    hass.http = MagicMock()
    return hass


def test_registers_the_view():
    hass = _hass_with_http()
    ensure_hello_view(hass)
    registered = hass.http.register_view.call_args.args[0]
    assert isinstance(registered, SvitgridHelloView)


def test_registering_twice_is_harmless():
    """Routes are global to hass.http and outlive a reload, so the second call
    hits aiohttp's "already registered" RuntimeError. Swallowed: the existing
    route already serves, and throwing here would abort a config flow."""
    hass = _hass_with_http()
    hass.http.register_view.side_effect = RuntimeError("already registered")
    ensure_hello_view(hass)  # must not raise


def test_an_install_with_no_http_component_does_not_crash():
    """A YAML/test harness with no http component. The view is a convenience,
    never a reason setup fails."""
    hass = MagicMock()
    hass.data = {}
    del hass.http
    ensure_hello_view(hass)  # must not raise


@pytest.mark.asyncio
async def test_opening_the_config_flow_registers_it(hass, enable_custom_integrations):
    """The path that matters: a never-paired install, the owner opens Svitgrid
    in Home Assistant, and the app can now see the add-on and its code."""
    from homeassistant import config_entries
    from homeassistant.setup import async_setup_component

    from custom_components.svitgrid.const import DOMAIN

    # The fixture's hass has no http component until something asks for one.
    assert await async_setup_component(hass, "http", {})

    # Opening the flow now goes straight to pairing, which calls /start. The
    # view is registered synchronously before that, so the test does not wait
    # on the claim poll: blocking until it finishes would drive the flow into
    # its failure path, which is not what this test is about.
    with patch("custom_components.svitgrid.config_flow.PairingClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.start = AsyncMock(
            return_value={"secret": "s" * 40, "code": "7K9PA2", "expiresIn": 300}
        )
        mock_client.get_status = AsyncMock(side_effect=Exception("still waiting"))
        await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

    routes = [r for r in hass.http.app.router.routes() if "svitgrid/hello" in str(r.resource)]
    assert routes, "the hello view must be registered once the flow is open"
