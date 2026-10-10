"""A local-only entry: paired over the LAN by a guest, with no cloud account.

It carries `local_only: true` and no `api_key`, `api_base`, `edge_device_id`
or `household_id`. Setup must not touch any of them, and must start nothing
that calls the cloud: no reading sender, no command poller, no MQTT wake, no
settings sync, no preset refresh, no register-spec download. A loop that runs
anyway logs a 401 every few seconds, forever.

What must run: harvest, the local store with its rollup, the island HTTP API,
and the local event scheduler.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.keystore import ENTRY_ISLAND_DEVICE_IDS, SvitgridKeystore
from custom_components.svitgrid.reading_store import ReadingStore
from custom_components.svitgrid.signing import (
    compute_key_id,
    generate_keypair,
    serialize_private_key,
)

_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}

ISLAND_KEY = "k" * 43
DEVICE_ID = "install-123"


@pytest.fixture(autouse=True)
def _stub_store_side_effects():
    with (
        patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)),
        patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)),
    ):
        yield


def _local_entry() -> tuple[MockConfigEntry, str, str]:
    priv, pub = generate_keypair()
    _, app_pub = generate_keypair()
    app_key_id = compute_key_id(app_pub)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Дім",
        data={
            "local_only": True,
            "cloud_ingest_enabled": False,
            "signing_key_id": "ha-local01",
            "private_key_pem": serialize_private_key(priv),
            "public_key_hex": pub,
            "trusted_keys": [],
            "preset_id": None,
            "inverters": [
                {
                    "inverter_id": "local-inv-direct",
                    "entity_map": {},
                    "command_recipes": [],
                    "command_config": {},
                    "harvest_config": {
                        "protocol": "solarman_v5",
                        "ip": "10.0.0.5",
                        "port": 8899,
                        "slave_id": 1,
                        "model_id": "deye_sg04lp3",
                        "logger_serial": "1234567890",
                    },
                },
                {
                    "inverter_id": "local-inv-relay",
                    "entity_map": {"batterySoc": "sensor.soc"},
                    "command_recipes": [],
                    "command_config": {},
                    "preset_id": "deye-sg04lp3-solarman-v1",
                },
            ],
            "local_pairing_grant": {
                "deviceId": DEVICE_ID,
                "islandKey": ISLAND_KEY,
                "deviceLabel": "Pixel 8",
                "pairedAt": "2026-10-10T10:00:00Z",
                "signingKeyId": app_key_id,
                "publicKeyHex": app_pub,
            },
        },
        entry_id="entry-local",
    )
    return entry, app_key_id, app_pub


def _patches(hass):
    return (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_direct_harvest_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_settings_sync_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_event_scheduler_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)
        ),
        patch("custom_components.svitgrid.SvitgridApiClient"),
    )


async def _setup(hass, entry):
    from contextlib import ExitStack

    from custom_components.svitgrid import async_setup_entry

    entry.add_to_hass(hass)
    with ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in _patches(hass)]
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()
    names = [
        "readings",
        "harvest",
        "command",
        "mqtt_wake",
        "sender",
        "settings_sync",
        "scheduler",
        "register_views",
        "register_panel",
        "remove_panel",
        "forward",
        "api_client_cls",
    ]
    return ok, dict(zip(names, mocks, strict=True))


@pytest.mark.asyncio
async def test_setup_succeeds_without_any_cloud_field(hass, enable_custom_integrations):
    entry, _, _ = _local_entry()
    ok, _ = await _setup(hass, entry)
    assert ok is True


@pytest.mark.asyncio
async def test_no_loop_that_calls_the_cloud_starts(hass, enable_custom_integrations):
    entry, _, _ = _local_entry()
    _, m = await _setup(hass, entry)

    m["command"].assert_not_called()
    m["mqtt_wake"].assert_not_called()
    m["sender"].assert_not_called()
    m["settings_sync"].assert_not_called()
    # No client means no preset refresh and no register-spec download.
    m["api_client_cls"].assert_not_called()


@pytest.mark.asyncio
async def test_harvest_store_views_and_scheduler_run(hass, enable_custom_integrations):
    from custom_components.svitgrid.harvest.register_spec import RegisterSpec

    entry, _, _ = _local_entry()
    _, m = await _setup(hass, entry)

    # Direct harvest runs, on the bundled spec (no cloud to fetch it from).
    assert m["harvest"].call_count == 1
    spec = m["harvest"].call_args.kwargs["spec_holder"].spec
    assert isinstance(spec, RegisterSpec)
    assert spec.model_id == "deye_sg04lp3"
    # The relay inverter reads its Home Assistant entities.
    assert m["readings"].call_count == 1
    # The local event scheduler runs, because cloud copy is off.
    assert m["scheduler"].call_count == 1
    m["register_views"].assert_called_once()

    state = hass.data[DOMAIN][entry.entry_id]
    assert state["cancel_rollup"] is not None
    assert state["command_task"] is None
    assert state["mqtt_wake_task"] is None
    assert state["settings_sync_task"] is None
    assert state["sender_task"] is None


@pytest.mark.asyncio
async def test_the_pairing_grant_lands_in_the_keystore(hass, enable_custom_integrations):
    entry, app_key_id, app_pub = _local_entry()
    await _setup(hass, entry)

    ks = SvitgridKeystore(hass)
    state = await ks.load()
    assert state.island_keys[DEVICE_ID] == {
        "key": ISLAND_KEY,
        "label": "Pixel 8",
        "pairedAt": "2026-10-10T10:00:00Z",
    }
    assert state.trusted_public_keys_hex[app_key_id] == app_pub


@pytest.mark.asyncio
async def test_the_grant_is_adopted_once_and_leaves_the_entry(hass, enable_custom_integrations):
    """Setup runs on every reload. A grant left in entry.data would re-add the
    key after the owner revoked it."""
    entry, _, _ = _local_entry()
    await _setup(hass, entry)

    assert "local_pairing_grant" not in entry.data
    assert entry.data[ENTRY_ISLAND_DEVICE_IDS] == [DEVICE_ID]


@pytest.mark.asyncio
async def test_a_grant_keeps_keys_trusted_earlier(hass, enable_custom_integrations):
    """A second entry on the same install must not drop the trust the first
    one built up."""
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="",
        public_key_hex="04" + "b" * 128,
        private_key_pem="pem",
        signing_key_id="ha-old",
        trusted_key_ids=["old-key"],
        trusted_public_keys_hex={"old-key": "04" + "c" * 128},
    )
    entry, app_key_id, _ = _local_entry()
    await _setup(hass, entry)

    state = await ks.load()
    assert set(state.trusted_public_keys_hex) == {"old-key", app_key_id}


@pytest.mark.asyncio
async def test_removing_the_entry_untrusts_the_apps_signing_key(hass, enable_custom_integrations):
    """The signing key was trusted for this station only. Left behind, it
    could still sign commands for whatever is paired here next."""
    from custom_components.svitgrid import async_remove_entry

    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="",
        public_key_hex="04" + "b" * 128,
        private_key_pem="pem",
        signing_key_id="ha-old",
        trusted_key_ids=["other-key"],
        trusted_public_keys_hex={"other-key": "04" + "c" * 128},
    )
    entry, app_key_id, _ = _local_entry()
    await _setup(hass, entry)
    assert app_key_id in (await ks.load()).trusted_public_keys_hex

    await async_remove_entry(hass, entry)

    # The instance LAN auth reads; a second Store instance can serve stale data.
    state = await hass.data[DOMAIN]["keystore"].load()
    assert app_key_id not in state.trusted_public_keys_hex
    assert "other-key" in state.trusted_public_keys_hex
    assert DEVICE_ID not in state.island_keys


@pytest.mark.asyncio
async def test_a_local_entry_never_overwrites_an_existing_identity(
    hass, enable_custom_integrations
):
    """One install, one owner: pair-local refuses when an entry exists. Should
    a local entry still meet a keystore holding a cloud identity, setup keeps
    that identity rather than replace its api_key and keypair."""
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="cloud-api-key",
        public_key_hex="04" + "b" * 128,
        private_key_pem="cloud-pem",
        signing_key_id="ha-cloud",
        trusted_key_ids=[],
    )
    entry, _, _ = _local_entry()
    await _setup(hass, entry)

    state = await ks.load()
    assert state.api_key == "cloud-api-key"
    assert state.private_key_pem == "cloud-pem"
    assert state.signing_key_id == "ha-cloud"
