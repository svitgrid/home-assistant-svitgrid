"""One `set_cloud_ingest` apply reloads the config entry once.

Both apply paths write `cloud_ingest_enabled` into `entry.data` and then
schedule their own reload. The data write also fires the entry's update
listener, `_async_reload_entry`, which reloaded a second time. A production
log showed "Svitgrid started from config entry …" twice within about 200 ms
per tap, restarting the harvest loop, the poller and MQTT twice
(ivanursul/svitgrid#751, part 4).

These tests use a real config entry with the real update listener attached, so
the listener's reload is counted alongside the explicit one.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid import _async_reload_entry
from custom_components.svitgrid.command_poller import process_command
from custom_components.svitgrid.const import DOMAIN, SET_CLOUD_INGEST_COMMAND
from custom_components.svitgrid.http_views import SvitgridCommandsView
from custom_components.svitgrid.signing import generate_keypair


def _entry(hass, *, cloud_ingest: bool = True, with_listener: bool = True) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={"cloud_ingest_enabled": cloud_ingest})
    entry.add_to_hass(hass)
    if with_listener:
        entry.add_update_listener(_async_reload_entry)
    return entry


async def _apply_over_lan(hass, enabled: bool) -> None:
    await SvitgridCommandsView()._apply_integration_command(
        hass, SET_CLOUD_INGEST_COMMAND, {"enabled": enabled}
    )
    await hass.async_block_till_done()


async def _apply_from_cloud(hass, entry, enabled: bool) -> None:
    api_client = MagicMock()
    api_client.ack_command = AsyncMock()
    priv, _pub = generate_keypair()
    await process_command(
        command={
            "commandId": "cmd-1",
            "command": SET_CLOUD_INGEST_COMMAND,
            "payload": {"enabled": enabled},
        },
        api_client=api_client,
        api_key="k",
        trusted_public_keys_hex={},
        our_private_key=priv,
        our_signing_key_id="ours",
        executor_version="0.3.0",
        keystore=MagicMock(),
        hass=hass,
        entry=entry,
    )
    await hass.async_block_till_done()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_lan_apply_reloads_once(hass, enabled):
    entry = _entry(hass, cloud_ingest=not enabled)
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        await _apply_over_lan(hass, enabled)

    assert entry.data["cloud_ingest_enabled"] is enabled
    assert reload.await_count == 1


@pytest.mark.asyncio
async def test_lan_apply_reloads_each_entry_once(hass):
    first = _entry(hass)
    second = _entry(hass)
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        await _apply_over_lan(hass, False)

    assert sorted(c.args[0] for c in reload.await_args_list) == sorted(
        [first.entry_id, second.entry_id]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_cloud_apply_reloads_once(hass, enabled):
    entry = _entry(hass, cloud_ingest=not enabled)
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        await _apply_from_cloud(hass, entry, enabled)

    assert entry.data["cloud_ingest_enabled"] is enabled
    assert reload.await_count == 1


@pytest.mark.asyncio
async def test_an_unchanged_apply_does_not_swallow_the_next_reload(hass):
    """Re-sending the current value changes nothing, so the listener does not
    fire. The skip it was armed with must not linger and eat the reload of an
    unrelated update later on."""
    entry = _entry(hass, cloud_ingest=True)
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        await _apply_over_lan(hass, True)
        assert reload.await_count == 1

        hass.config_entries.async_update_entry(entry, data={**entry.data, "other": 1})
        await hass.async_block_till_done()

    assert reload.await_count == 2


@pytest.mark.asyncio
async def test_an_entry_without_a_listener_still_reloads_once(hass):
    """Setup registers the listener last, so an entry whose setup failed has
    none. The explicit reload is then the only one, and the skip must not
    linger for a later listener."""
    entry = _entry(hass, cloud_ingest=True, with_listener=False)
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        await _apply_over_lan(hass, False)
        assert reload.await_count == 1

        entry.add_update_listener(_async_reload_entry)
        hass.config_entries.async_update_entry(entry, data={**entry.data, "other": 1})
        await hass.async_block_till_done()

    assert reload.await_count == 2
