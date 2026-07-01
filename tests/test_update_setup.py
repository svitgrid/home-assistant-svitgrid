"""Verify the update coordinator is created and the platform is forwarded."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.reading_store import ReadingStore

_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}


def test_update_platform_is_forwarded():
    # The list passed to async_forward_entry_setups must include "update".
    import custom_components.svitgrid as init_mod

    src = init_mod.__file__
    with open(src) as f:
        text = f.read()
    assert '"update"' in text and "async_forward_entry_setups" in text
    # And the coordinator must be stored for the platform to read.
    assert '"update_coordinator"' in text


@pytest.fixture(autouse=True)
def _stub_store_side_effects():
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
    ):
        yield


def _make_entry():
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (update)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-update",
            "signing_key_id": "ha-home-01",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-xyz",
                    "entity_map": {"batterySoc": "sensor.soc"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                    "harvest_config": None,
                }
            ],
        },
        entry_id="entry-update",
    )


@pytest.mark.asyncio
async def test_setup_entry_creates_and_stores_update_coordinator(hass, enable_custom_integrations):
    """async_setup_entry must build a SvitgridUpdateCoordinator, store it under
    hass.data[DOMAIN][entry.entry_id]["update_coordinator"], and forward the
    "update" platform alongside "sensor"/"binary_sensor"."""
    from custom_components.svitgrid import async_setup_entry
    from custom_components.svitgrid.update import SvitgridUpdateCoordinator

    entry = _make_entry()
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ) as forward_mock,
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
        patch(
            "custom_components.svitgrid.SvitgridUpdateCoordinator.async_refresh",
            new_callable=AsyncMock,
        ),
    ):
        client = mock_cls.return_value
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True

    entry_state = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_state["update_coordinator"]
    assert isinstance(coordinator, SvitgridUpdateCoordinator)

    forward_mock.assert_awaited_once()
    forwarded_platforms = forward_mock.call_args.args[1]
    assert "update" in forwarded_platforms
