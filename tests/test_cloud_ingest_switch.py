"""Task 6: cloud_ingest_enabled gates the cloud sender; harvester writes
local store always (island SP1).

Flag is read from entry.data, then entry.options, defaulting to True when
absent from both — so existing entries (no flag) continue to ingest to cloud.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.reading_store import ReadingStore

_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}

_HARVEST_INVERTER = {
    "inverter_id": "ha-island-inv",
    "entity_map": {},
    "command_recipes": [],
    "command_config": {},
    "brand": "Deye",
    "model": "SG04LP3",
    "phases": 3,
    "has_battery": True,
    "pv_strings": 2,
    "preset_id": None,
    "harvest_config": {
        "model_id": "deye_sg04lp3",
        "host": "10.0.0.5",
        "port": 8899,
        "slave_id": 1,
    },
}

_BASE_DATA = {
    "api_base": "https://api.example.com",
    "api_key": "test-key",
    "edge_device_id": "ed-1",
    "household_id": "h-island",
    "signing_key_id": "ha-home-01",
    "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
    "public_key_hex": "04" + "a" * 128,
    "trusted_keys": [],
    "inverters": [_HARVEST_INVERTER],
}

_MINIMAL_SPEC = {
    "modelId": "deye_sg04lp3",
    "version": 1,
    "protocol": "solarman_v5",
    "port": 8899,
    "defaultSlaveId": 1,
    "reads": [],
    "derivations": [],
}


@pytest.fixture(autouse=True)
def _stub_store_side_effects():
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
    ):
        yield


def _make_entry(cloud_ingest_enabled=None, entry_id="entry-island"):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    data = dict(_BASE_DATA)
    if cloud_ingest_enabled is not None:
        data["cloud_ingest_enabled"] = cloud_ingest_enabled
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (island)",
        data=data,
        entry_id=entry_id,
    )


@pytest.mark.asyncio
async def test_cloud_ingest_disabled_skips_sender_but_runs_harvest(
    hass, enable_custom_integrations
):
    """cloud_ingest_enabled=False: sender NOT spawned; harvest loop IS spawned.
    The rollup timer and local-store writes run regardless — pure island mode."""
    from custom_components.svitgrid import async_setup_entry

    entry = _make_entry(cloud_ingest_enabled=False)
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch(
            "custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock
        ) as harvest,
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock) as sender,
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
    ):
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(return_value=dict(_MINIMAL_SPEC))
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    # Sender must NOT have been spawned in island (pure-offline) mode.
    assert sender.call_count == 0, (
        "run_sender_loop must not be called when cloud_ingest_enabled=False"
    )
    # Harvest loop must still run — local-store writes are unaffected.
    assert harvest.call_count == 1, (
        "harvest loop must always run regardless of cloud_ingest_enabled"
    )

    # Confirm entry state: sender_task is None but rollup is present (store keeps running).
    entry_state = hass.data[DOMAIN][entry.entry_id]
    assert entry_state.get("sender_task") is None, "sender_task must be None in island mode"
    assert callable(entry_state.get("cancel_rollup")), "rollup must still run in island mode"
    assert entry_state.get("store") is not None, "store must always be created"
    # The store must know cloud ingest is off so the panel's sync footer can
    # render "local only" instead of a false ⚠ pending-sync warning.
    assert entry_state["store"].cloud_ingest_enabled is False


@pytest.mark.asyncio
async def test_cloud_ingest_absent_defaults_to_enabled(hass, enable_custom_integrations):
    """No cloud_ingest_enabled key → defaults True (existing entries unaffected,
    backward-compatible)."""
    from custom_components.svitgrid import async_setup_entry

    entry = _make_entry(cloud_ingest_enabled=None, entry_id="entry-island-absent")
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch(
            "custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock
        ) as harvest,
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock) as sender,
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
    ):
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(return_value=dict(_MINIMAL_SPEC))
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    # Default behavior (backward-compat): sender IS spawned when flag is absent.
    assert sender.call_count == 1, (
        "sender must be called when cloud_ingest_enabled is absent (default True)"
    )
    assert harvest.call_count == 1

    entry_state = hass.data[DOMAIN][entry.entry_id]
    assert entry_state.get("sender_task") is not None
    assert entry_state["store"].cloud_ingest_enabled is True


@pytest.mark.asyncio
async def test_cloud_ingest_explicitly_true_enables_sender(hass, enable_custom_integrations):
    """cloud_ingest_enabled=True explicitly → sender IS spawned (regression guard)."""
    from custom_components.svitgrid import async_setup_entry

    entry = _make_entry(cloud_ingest_enabled=True, entry_id="entry-island-explicit-true")
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch(
            "custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock
        ) as harvest,
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock) as sender,
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
    ):
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(return_value=dict(_MINIMAL_SPEC))
        client.get_preset = AsyncMock(return_value=None)

        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    assert sender.call_count == 1
    assert harvest.call_count == 1

    entry_state = hass.data[DOMAIN][entry.entry_id]
    assert entry_state.get("sender_task") is not None
