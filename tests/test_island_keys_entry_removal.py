"""Island keys do not outlive the entry that adopted them (issue #6).

The keystore (`.storage/svitgrid`) belongs to the Home Assistant install, not to
a config entry. Removing an entry, including the add-on's own removal after the
cloud deprovisions it, used to leave every island key it had adopted in place,
and those keys still authenticated LAN requests.

An entry now records the roster ids it adopted at pairing, and removing it (or
the device being deprovisioned) revokes exactly those. Another entry's keys stay.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.keystore import SvitgridKeystore, pairing_island_device_id
from custom_components.svitgrid.reading_store import ReadingStore
from tests.test_init_harvest_wiring import _ACTIVE_LIFECYCLE, _MINIMAL_SPEC, _make_entry

KEY_A = "pairing-key-of-entry-a"
KEY_B = "pairing-key-of-entry-b"
OLD_SCALAR_KEY = "key-from-a-pre-0-16-enable-island"

_SNAKE = {
    "protocol": "solarman_v5",
    "ip": "192.168.1.50",
    "port": 8899,
    "slave_id": 1,
    "model_id": "deye_sg04lp3",
    "logger_serial": "1234567890",
}


async def _prime(ks: SvitgridKeystore, **kwargs) -> None:
    await ks.save(
        api_key="ak",
        public_key_hex="04ff",
        private_key_pem="pem",
        signing_key_id="ha-1",
        trusted_key_ids=[],
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _stub_store_side_effects():
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
        patch.object(ReadingStore, "set_lifecycle", AsyncMock()),
    ):
        yield


def _entry(hass, *, island_key: str | None, extra: dict | None = None):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    base = _make_entry(dict(_SNAKE))
    data = dict(base.data)
    if island_key is not None:
        data["island_key"] = island_key
    data.update(extra or {})
    entry = MockConfigEntry(domain=DOMAIN, version=2, title=base.title, data=data)
    entry.add_to_hass(hass)
    return entry


def _setup_patches(hass):
    return (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_settings_sync_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)),
    )


async def _setup_and_unload(hass, entry, *, between=None):
    from contextlib import ExitStack

    from custom_components.svitgrid import async_setup_entry, async_unload_entry

    with ExitStack() as stack:
        for p in _setup_patches(hass):
            stack.enter_context(p)
        mock_cls = stack.enter_context(patch("custom_components.svitgrid.SvitgridApiClient"))
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(return_value=dict(_MINIMAL_SPEC))
        client.get_preset = AsyncMock(return_value=None)

        assert await async_setup_entry(hass, entry) is True
        await hass.async_block_till_done()
        if between is not None:
            await between()
            await hass.async_block_till_done()
        await async_unload_entry(hass, entry)
        await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Keystore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopt_returns_the_roster_id_holding_the_key(hass):
    ks = SvitgridKeystore(hass)
    await _prime(ks, island_keys={"phone-1": {"key": KEY_B, "label": "Pixel", "pairedAt": None}})

    assert await ks.async_adopt_pairing_island_key(KEY_A) == pairing_island_device_id(KEY_A)
    # A key an existing entry already holds reports that entry's id.
    assert await ks.async_adopt_pairing_island_key(KEY_B) == "phone-1"


@pytest.mark.asyncio
async def test_adopt_on_an_empty_keystore_returns_none(hass):
    assert await SvitgridKeystore(hass).async_adopt_pairing_island_key(KEY_A) is None


@pytest.mark.asyncio
async def test_revoke_island_keys_clears_only_the_matching_scalar(hass):
    ks = SvitgridKeystore(hass)
    await _prime(ks, island_key=KEY_A)
    await ks.async_revoke_island_keys([pairing_island_device_id(KEY_A)])
    assert (await ks.load()).island_key is None

    await _prime(ks, island_key=OLD_SCALAR_KEY)
    await ks.async_revoke_island_keys([pairing_island_device_id(KEY_A)], key=KEY_A)
    assert (await ks.load()).island_key == OLD_SCALAR_KEY


# ---------------------------------------------------------------------------
# Setup records, removal revokes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_records_the_adopted_roster_id_on_the_entry(hass, enable_custom_integrations):
    entry = _entry(hass, island_key=KEY_A)
    await _setup_and_unload(hass, entry)

    assert "island_key" not in entry.data
    assert entry.data["island_device_ids"] == [pairing_island_device_id(KEY_A)]


@pytest.mark.asyncio
async def test_removing_an_entry_drops_its_keys_and_keeps_another_entrys(
    hass, enable_custom_integrations
):
    from custom_components.svitgrid import async_remove_entry

    entry_a = _entry(hass, island_key=KEY_A)
    entry_b = _entry(hass, island_key=KEY_B)
    await _setup_and_unload(hass, entry_a)
    await _setup_and_unload(hass, entry_b)
    ks = SvitgridKeystore(hass)
    assert sorted(await ks.async_get_island_keys()) == sorted([KEY_A, KEY_B])

    await async_remove_entry(hass, entry_a)

    # Read through the instance LAN auth uses, which setup left in hass.data.
    ks = hass.data[DOMAIN]["keystore"]
    assert await ks.async_get_island_keys() == [KEY_B]
    assert list((await ks.load()).island_keys) == [pairing_island_device_id(KEY_B)]


@pytest.mark.asyncio
async def test_removing_an_entry_that_never_set_up_clears_its_scalar(
    hass, enable_custom_integrations
):
    """The key is still in entry.data; the scalar holding it goes, a different one stays."""
    from custom_components.svitgrid import async_remove_entry

    ks = SvitgridKeystore(hass)
    await _prime(ks, island_key=KEY_A)
    await async_remove_entry(hass, _entry(hass, island_key=KEY_A))
    assert await ks.async_get_island_keys() == []

    await _prime(ks, island_key=OLD_SCALAR_KEY)
    await async_remove_entry(hass, _entry(hass, island_key=KEY_A))
    assert await ks.async_get_island_keys() == [OLD_SCALAR_KEY]


@pytest.mark.asyncio
async def test_removing_the_last_unrecorded_entry_revokes_pairing_keys_only(
    hass, enable_custom_integrations
):
    """An entry adopted before ids were recorded names none. When no other entry
    remains, its pairing-time keys can only be its own; a device added through
    the island roster and the legacy scalar are left alone."""
    from custom_components.svitgrid import async_remove_entry

    ks = SvitgridKeystore(hass)
    await _prime(
        ks,
        island_key=OLD_SCALAR_KEY,
        island_keys={
            pairing_island_device_id(KEY_A): {"key": KEY_A, "label": None, "pairedAt": None},
            "phone-1": {"key": KEY_B, "label": "Pixel", "pairedAt": None},
        },
    )
    entry = _entry(hass, island_key=None)

    await async_remove_entry(hass, entry)

    state = await ks.load()
    assert list(state.island_keys) == ["phone-1"]
    assert state.island_key == OLD_SCALAR_KEY


@pytest.mark.asyncio
async def test_removing_an_unrecorded_entry_beside_another_entry_revokes_nothing(
    hass, enable_custom_integrations
):
    from custom_components.svitgrid import async_remove_entry

    ks = SvitgridKeystore(hass)
    paired = {pairing_island_device_id(KEY_A): {"key": KEY_A, "label": None, "pairedAt": None}}
    await _prime(ks, island_keys=paired)
    _entry(hass, island_key=None)  # the other entry
    entry = _entry(hass, island_key=None)

    await async_remove_entry(hass, entry)

    assert await ks.async_get_island_keys() == [KEY_A]


@pytest.mark.asyncio
async def test_deprovisioning_revokes_the_entrys_keys(hass, enable_custom_integrations):
    entry_a = _entry(hass, island_key=KEY_A)
    entry_b = _entry(hass, island_key=KEY_B)
    await _setup_and_unload(hass, entry_b)

    async def _deprovision():
        lifecycle = hass.data[DOMAIN][entry_a.entry_id]["lifecycle"]
        lifecycle.deprovision("revoked", "2026-09-14T10:00:00Z")

    await _setup_and_unload(hass, entry_a, between=_deprovision)

    assert await SvitgridKeystore(hass).async_get_island_keys() == [KEY_B]
