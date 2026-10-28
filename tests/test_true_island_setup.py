"""True island: island mode on and the cloud copy off.

The owner's rule (2026-10-15): the cloud then takes no part in day-to-day
operation, and Home Assistant works with the app directly. Setup starts no
command poller, no MQTT wake loop, no settings sync, no reading sender, and so
reports no version to the cloud either. Presets and register specs are still
read from the public endpoint at startup, which needs no API key, with the
saved and bundled copies as the fallback.

An entry paired through the cloud as an island with the cloud copy off becomes
a true island on upgrade. Its keystore, island keys, trusted keys and calendar
must come through untouched, because the app now reaches it only over the LAN.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.const import DEFAULT_API_BASE, DOMAIN
from custom_components.svitgrid.keystore import SvitgridKeystore
from custom_components.svitgrid.reading_store import ReadingStore
from custom_components.svitgrid.signing import generate_keypair, serialize_private_key

_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}

ISLAND_KEY = "i" * 43
TRUSTED = {"app-key": "04" + "c" * 128}


@pytest.fixture(autouse=True)
def _stub_store_side_effects():
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
    ):
        yield


def _cloud_entry(*, cloud_ingest: bool | None) -> MockConfigEntry:
    priv, pub = generate_keypair()
    data = {
        "api_base": "https://api.example.test",
        "api_key": "cloud-api-key",
        "edge_device_id": "ed-1",
        "household_id": "hh-1",
        "signing_key_id": "ha-cloud01",
        "private_key_pem": serialize_private_key(priv),
        "public_key_hex": pub,
        "trusted_keys": [{"keyId": k, "publicKeyHex": v} for k, v in TRUSTED.items()],
        "inverters": [
            {
                "inverter_id": "inv-direct",
                "entity_map": {},
                "command_recipes": [],
                "command_config": {},
                "preset_id": "deye-sg04lp3-solarman-v1",
                "harvest_config": {
                    "protocol": "solarman_v5",
                    "ip": "10.0.0.5",
                    "port": 8899,
                    "slave_id": 1,
                    "model_id": "deye_sg04lp3",
                    "logger_serial": "1234567890",
                },
            }
        ],
    }
    if cloud_ingest is not None:
        data["cloud_ingest_enabled"] = cloud_ingest
    return MockConfigEntry(domain=DOMAIN, version=2, title="Дім", data=data, entry_id="e-cloud")


async def _seed_keystore(hass, *, island_keys: bool = True) -> SvitgridKeystore:
    """The keystore a 0.22.x island install carries before the upgrade."""
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="cloud-api-key",
        public_key_hex="04" + "b" * 128,
        private_key_pem="pem",
        signing_key_id="ha-cloud01",
        trusted_key_ids=sorted(TRUSTED),
        trusted_public_keys_hex=dict(TRUSTED),
    )
    if island_keys:
        await ks.async_add_island_key(
            "phone-1", ISLAND_KEY, label="Pixel 8", paired_at="2026-10-01T10:00:00Z"
        )
    return ks


def _api_client_mock():
    client = MagicMock()
    client.get_preset = AsyncMock(return_value=None)
    client.get_register_spec = AsyncMock(return_value=None)
    return client


async def _setup(hass, entry):
    from custom_components.svitgrid import async_setup_entry

    entry.add_to_hass(hass)
    client = _api_client_mock()
    names = (
        "readings",
        "harvest",
        "command",
        "mqtt_wake",
        "sender",
        "settings_sync",
        "scheduler",
    )
    with ExitStack() as stack:
        mocks = {
            name: stack.enter_context(
                patch(f"custom_components.svitgrid.{target}", new_callable=AsyncMock)
            )
            for name, target in zip(
                names,
                (
                    "run_readings_loop",
                    "run_direct_harvest_loop",
                    "run_command_loop",
                    "run_mqtt_wake_loop",
                    "run_sender_loop",
                    "run_settings_sync_loop",
                    "run_event_scheduler_loop",
                ),
                strict=True,
            )
        }
        stack.enter_context(patch("custom_components.svitgrid.register_views"))
        stack.enter_context(
            patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock)
        )
        stack.enter_context(patch("custom_components.svitgrid.remove_panel"))
        stack.enter_context(
            patch.object(
                hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
            )
        )
        mocks["api_client_cls"] = stack.enter_context(
            patch("custom_components.svitgrid.SvitgridApiClient", return_value=client)
        )
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()
    mocks["client"] = client
    return ok, mocks


@pytest.mark.asyncio
async def test_true_island_starts_no_cloud_loop(hass, enable_custom_integrations):
    await _seed_keystore(hass)
    entry = _cloud_entry(cloud_ingest=False)
    ok, m = await _setup(hass, entry)

    assert ok is True
    m["command"].assert_not_called()
    m["mqtt_wake"].assert_not_called()
    m["sender"].assert_not_called()
    state = hass.data[DOMAIN][entry.entry_id]
    assert state["command_task"] is None
    assert state["mqtt_wake_task"] is None
    assert state["sender_task"] is None
    assert state["true_island"] is True


@pytest.mark.asyncio
async def test_settings_read_runs_on_true_island(hass, enable_custom_integrations):
    """The settings mirror needs a read on the island too; it never uploads."""
    await _seed_keystore(hass)
    entry = _cloud_entry(cloud_ingest=False)
    _, m = await _setup(hass, entry)

    m["settings_sync"].assert_called_once()
    assert m["settings_sync"].call_args.kwargs["cloud_upload"] is False
    assert hass.data[DOMAIN][entry.entry_id]["settings_sync_task"] is not None


@pytest.mark.asyncio
async def test_true_island_runs_harvest_store_and_scheduler(hass, enable_custom_integrations):
    await _seed_keystore(hass)
    entry = _cloud_entry(cloud_ingest=False)
    _, m = await _setup(hass, entry)

    assert m["harvest"].call_count == 1
    assert m["scheduler"].call_count == 1
    assert hass.data[DOMAIN][entry.entry_id]["cancel_rollup"] is not None
    assert hass.data[DOMAIN]["event_store"] is not None


@pytest.mark.asyncio
async def test_true_island_still_reads_presets_and_specs_from_the_public_endpoint(
    hass, enable_custom_integrations
):
    await _seed_keystore(hass)
    entry = _cloud_entry(cloud_ingest=False)
    _, m = await _setup(hass, entry)

    m["client"].get_preset.assert_awaited_with("deye-sg04lp3-solarman-v1")
    m["client"].get_register_spec.assert_awaited_with("deye_sg04lp3")


@pytest.mark.asyncio
async def test_upgrade_keeps_the_keystore_island_keys_and_trusted_keys(
    hass, enable_custom_integrations
):
    """An island entry from 0.22.x becomes a true island on upgrade. The app
    reaches it only over the LAN from then on, so losing a key locks it out."""
    ks = await _seed_keystore(hass)
    entry = _cloud_entry(cloud_ingest=False)
    await _setup(hass, entry)

    state = await ks.load()
    assert state.api_key == "cloud-api-key"
    assert state.signing_key_id == "ha-cloud01"
    assert state.trusted_public_keys_hex == TRUSTED
    assert state.island_keys["phone-1"]["key"] == ISLAND_KEY
    # The cloud identity stays on the entry, so the cloud copy can come back.
    assert entry.data["api_key"] == "cloud-api-key"
    assert entry.data["cloud_ingest_enabled"] is False


@pytest.mark.asyncio
async def test_cloud_copy_on_starts_the_cloud_loops(hass, enable_custom_integrations):
    await _seed_keystore(hass)
    entry = _cloud_entry(cloud_ingest=True)
    _, m = await _setup(hass, entry)

    m["command"].assert_called_once()
    m["mqtt_wake"].assert_called_once()
    m["sender"].assert_called_once()
    m["settings_sync"].assert_called_once()
    assert m["settings_sync"].call_args.kwargs["cloud_upload"] is True
    m["scheduler"].assert_not_called()
    assert hass.data[DOMAIN][entry.entry_id]["true_island"] is False


@pytest.mark.asyncio
async def test_a_plain_cloud_entry_is_not_a_true_island(hass, enable_custom_integrations):
    """No `cloud_ingest_enabled` key: a non-island pairing, copy on by default."""
    await _seed_keystore(hass, island_keys=False)
    entry = _cloud_entry(cloud_ingest=None)
    _, m = await _setup(hass, entry)

    m["command"].assert_called_once()
    assert hass.data[DOMAIN][entry.entry_id]["true_island"] is False


@pytest.mark.asyncio
async def test_copy_off_with_no_phone_key_keeps_the_command_channel(
    hass, enable_custom_integrations
):
    """Cloud copy off and no island key anywhere: no phone can reach this
    install over the LAN, so stopping the poller would strand it for good."""
    await _seed_keystore(hass, island_keys=False)
    entry = _cloud_entry(cloud_ingest=False)
    _, m = await _setup(hass, entry)

    m["command"].assert_called_once()
    m["sender"].assert_not_called()
    assert hass.data[DOMAIN][entry.entry_id]["true_island"] is False


@pytest.mark.asyncio
async def test_a_local_only_entry_reads_presets_and_specs_from_the_public_endpoint(
    hass, enable_custom_integrations
):
    """No account and no key: the public endpoint still answers."""
    from tests.test_local_only_setup import _local_entry

    entry, _, _ = _local_entry()
    _, m = await _setup(hass, entry)

    m["api_client_cls"].assert_called_once()
    assert m["api_client_cls"].call_args.kwargs["api_base"] == DEFAULT_API_BASE
    m["client"].get_preset.assert_awaited_with("deye-sg04lp3-solarman-v1")
    m["client"].get_register_spec.assert_awaited_with("deye_sg04lp3")
    m["command"].assert_not_called()
    assert hass.data[DOMAIN][entry.entry_id]["true_island"] is True
    # Nothing else may hold a client that carries no key.
    assert hass.data[DOMAIN][entry.entry_id]["api_client"] is None
