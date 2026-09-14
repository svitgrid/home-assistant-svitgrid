"""A pairing-time island key is a named device, not the legacy row.

HA pairing hands the add-on the phone's island key through `/status`. The add-on
used to store it in the keystore's single pre-0.16.0 `island_key` scalar, which
`/api/svitgrid/island-devices` reports as `isLegacy: true` with no label, so the
phone that had just paired was listed as "Device paired before this update — not
identifiable" (ivanursul/svitgrid#751, part 3).

A pairing key now lands in the per-device `island_keys` map. An install that
already holds its pairing key in the scalar migrates on the next setup, and the
phone presenting that key keeps authenticating throughout. A scalar that did
not come from pairing (a genuine pre-0.16.0 `enable_island`) stays legacy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.const import DOMAIN, LEGACY_ISLAND_DEVICE_ID
from custom_components.svitgrid.keystore import (
    PAIRING_ISLAND_DEVICE_LABEL,
    SvitgridKeystore,
    pairing_island_device_id,
)
from custom_components.svitgrid.reading_store import ReadingStore
from tests.test_init_harvest_wiring import _ACTIVE_LIFECYCLE, _MINIMAL_SPEC, _make_entry

PAIRING_KEY = "pairing-key-from-the-app"
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


# ---------------------------------------------------------------------------
# Keystore
# ---------------------------------------------------------------------------


def test_pairing_device_id_is_stable_and_does_not_contain_the_key():
    device_id = pairing_island_device_id(PAIRING_KEY)
    assert device_id == pairing_island_device_id(PAIRING_KEY)
    assert device_id != pairing_island_device_id("another-key")
    assert PAIRING_KEY not in device_id
    assert device_id != LEGACY_ISLAND_DEVICE_ID


@pytest.mark.asyncio
async def test_adopting_a_pairing_key_lists_a_named_device(hass):
    ks = SvitgridKeystore(hass)
    await _prime(ks)

    await ks.async_adopt_pairing_island_key(PAIRING_KEY, paired_at="2026-09-14T18:00:00Z")

    devices = await ks.async_list_island_devices()
    assert devices == [
        {
            "deviceId": pairing_island_device_id(PAIRING_KEY),
            "label": PAIRING_ISLAND_DEVICE_LABEL,
            "pairedAt": "2026-09-14T18:00:00Z",
            "isLegacy": False,
        }
    ]
    assert await ks.async_get_island_keys() == [PAIRING_KEY]


@pytest.mark.asyncio
async def test_adopting_moves_the_matching_scalar_into_the_map(hass):
    ks = SvitgridKeystore(hass)
    await _prime(ks, island_key=PAIRING_KEY)

    await ks.async_adopt_pairing_island_key(PAIRING_KEY)

    state = await ks.load()
    assert state.island_key is None
    assert [d["isLegacy"] for d in await ks.async_list_island_devices()] == [False]
    assert await ks.async_get_island_keys() == [PAIRING_KEY]


@pytest.mark.asyncio
async def test_adopting_leaves_a_different_scalar_as_legacy(hass):
    ks = SvitgridKeystore(hass)
    await _prime(ks, island_key=OLD_SCALAR_KEY)

    await ks.async_adopt_pairing_island_key(PAIRING_KEY)

    state = await ks.load()
    assert state.island_key == OLD_SCALAR_KEY
    ids = [d["deviceId"] for d in await ks.async_list_island_devices()]
    assert ids == [pairing_island_device_id(PAIRING_KEY), LEGACY_ISLAND_DEVICE_ID]


@pytest.mark.asyncio
async def test_adopting_twice_keeps_one_entry_and_its_first_pairing_time(hass):
    ks = SvitgridKeystore(hass)
    await _prime(ks)

    await ks.async_adopt_pairing_island_key(PAIRING_KEY, paired_at="2026-09-14T18:00:00Z")
    await ks.async_adopt_pairing_island_key(PAIRING_KEY, paired_at="2026-09-15T09:00:00Z")

    devices = await ks.async_list_island_devices()
    assert len(devices) == 1
    assert devices[0]["pairedAt"] == "2026-09-14T18:00:00Z"


@pytest.mark.asyncio
async def test_adopting_a_key_already_held_by_a_named_device_adds_nothing(hass):
    ks = SvitgridKeystore(hass)
    await _prime(
        ks, island_keys={"phone-1": {"key": PAIRING_KEY, "label": "Pixel", "pairedAt": None}}
    )

    await ks.async_adopt_pairing_island_key(PAIRING_KEY)

    devices = await ks.async_list_island_devices()
    assert [(d["deviceId"], d["label"]) for d in devices] == [("phone-1", "Pixel")]


@pytest.mark.asyncio
async def test_adopting_on_an_empty_keystore_is_a_no_op(hass):
    ks = SvitgridKeystore(hass)
    await ks.async_adopt_pairing_island_key(PAIRING_KEY)
    assert await ks.load() is None


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_store_side_effects():
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
    ):
        yield


def _island_entry(hass, island_key: str | None = PAIRING_KEY):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    base = _make_entry(dict(_SNAKE))
    data = dict(base.data)
    if island_key is not None:
        data["island_key"] = island_key
    entry = MockConfigEntry(domain=DOMAIN, version=2, title=base.title, data=data)
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, entry):
    from custom_components.svitgrid import async_setup_entry, async_unload_entry

    with (
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
        patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls,
    ):
        client = mock_cls.return_value
        client.get_register_spec = AsyncMock(return_value=dict(_MINIMAL_SPEC))
        client.get_preset = AsyncMock(return_value=None)

        assert await async_setup_entry(hass, entry) is True
        await hass.async_block_till_done()
        await async_unload_entry(hass, entry)
        await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_setup_lists_the_pairing_phone_as_a_named_device(hass, enable_custom_integrations):
    entry = _island_entry(hass)

    await _setup(hass, entry)

    ks = SvitgridKeystore(hass)
    devices = await ks.async_list_island_devices()
    assert [(d["deviceId"], d["label"], d["isLegacy"]) for d in devices] == [
        (pairing_island_device_id(PAIRING_KEY), PAIRING_ISLAND_DEVICE_LABEL, False)
    ]
    assert await ks.async_get_island_keys() == [PAIRING_KEY]


@pytest.mark.asyncio
async def test_setup_migrates_a_pairing_key_stored_in_the_legacy_scalar(
    hass, enable_custom_integrations
):
    """An install paired before this fix holds its phone's key in the scalar."""
    entry = _island_entry(hass)
    ks = SvitgridKeystore(hass)
    await _prime(ks, island_key=PAIRING_KEY)

    await _setup(hass, entry)

    state = await ks.load()
    assert state.island_key is None
    assert list(state.island_keys) == [pairing_island_device_id(PAIRING_KEY)]
    # The phone still presenting that key keeps authenticating.
    assert await ks.async_get_island_keys() == [PAIRING_KEY]


@pytest.mark.asyncio
async def test_setup_keeps_a_genuine_legacy_scalar_legacy(hass, enable_custom_integrations):
    """No pairing key on the entry: the scalar came from a pre-0.16.0
    `enable_island`, so it is not identifiable and stays the legacy row."""
    entry = _island_entry(hass, island_key=None)
    ks = SvitgridKeystore(hass)
    await _prime(ks, island_key=OLD_SCALAR_KEY)

    await _setup(hass, entry)

    devices = await ks.async_list_island_devices()
    assert [(d["deviceId"], d["isLegacy"]) for d in devices] == [(LEGACY_ISLAND_DEVICE_ID, True)]
    assert await ks.async_get_island_keys() == [OLD_SCALAR_KEY]


@pytest.mark.asyncio
async def test_a_revoked_pairing_key_stays_revoked_after_a_reload(hass, enable_custom_integrations):
    """Setup reads the entry's pairing key on every reload. If it re-adopted the
    key each time, revoking the phone would last only until the next restart."""
    entry = _island_entry(hass)
    await _setup(hass, entry)

    ks = SvitgridKeystore(hass)
    assert await ks.async_revoke_island_key(pairing_island_device_id(PAIRING_KEY)) is True

    await _setup(hass, entry)

    assert await ks.async_get_island_keys() == []
    assert "island_key" not in entry.data
