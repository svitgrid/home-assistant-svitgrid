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


# ── edge boxes (edge island plan, Task 8) ───────────────────────────────────


async def _adopt_box(ks: SvitgridKeystore, device_id: str, *inverter_ids: str) -> None:
    from custom_components.svitgrid.keystore import EdgeBoxEntry

    await ks.async_add_edge_box(
        EdgeBoxEntry(
            device_id=device_id,
            api_key=None,
            inverter_ids=list(inverter_ids),
            inverters={i: {"inverterId": i, "modelId": "deye_sg04lp3"} for i in inverter_ids},
            added_at="2026-10-29T10:00:00+00:00",
            pair_code_hash=None,
            pair_code_expires_at=None,
        )
    )


@pytest.mark.asyncio
async def test_entry_with_edge_box_has_queue_and_executors(hass, enable_custom_integrations):
    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue
    from custom_components.svitgrid.executors.edge_box import EdgeBoxExecutor

    ks = await _seed_keystore(hass)
    await _adopt_box(ks, "EC-1", "inv-edge-1", "inv-edge-2")
    await _adopt_box(ks, "EC-2", "inv-edge-3")
    entry = _cloud_entry(cloud_ingest=False)
    _, m = await _setup(hass, entry)

    state = hass.data[DOMAIN][entry.entry_id]
    assert isinstance(state["edge_queue"], EdgeCommandQueue)
    assert sorted(state["edge_boxes"]) == ["EC-1", "EC-2"]
    executors = state["executors_by_inverter"]
    for inverter_id, device_id in (
        ("inv-edge-1", "EC-1"),
        ("inv-edge-2", "EC-1"),
        ("inv-edge-3", "EC-2"),
    ):
        executor = executors[inverter_id]
        assert isinstance(executor, EdgeBoxExecutor)
        assert executor._device_id == device_id
        assert executor._inverter_id == inverter_id
        assert executor._queue is state["edge_queue"]
    # The entry's own inverter is untouched, and the scheduler sees the box's.
    assert not isinstance(executors["inv-direct"], EdgeBoxExecutor)
    assert m["harvest"].call_count == 1
    executor_for = m["scheduler"].call_args.kwargs["executor_for"]
    assert isinstance(executor_for("inv-edge-3"), EdgeBoxExecutor)


@pytest.mark.asyncio
async def test_an_inverter_held_by_an_edge_box_is_not_also_harvested(
    hass, enable_custom_integrations
):
    """The box reads it and takes its commands; a second reader on the same
    logger would collide with the box."""
    from custom_components.svitgrid.executors.edge_box import EdgeBoxExecutor

    ks = await _seed_keystore(hass)
    await _adopt_box(ks, "EC-1", "inv-direct")
    entry = _cloud_entry(cloud_ingest=False)
    _, m = await _setup(hass, entry)

    m["harvest"].assert_not_called()
    executor = hass.data[DOMAIN][entry.entry_id]["executors_by_inverter"]["inv-direct"]
    assert isinstance(executor, EdgeBoxExecutor)


@pytest.mark.asyncio
async def test_entry_without_edge_box_sets_up_as_before(hass, enable_custom_integrations):
    """No adopted box: no queue and no edge executors."""
    from custom_components.svitgrid.executors.edge_box import EdgeBoxExecutor

    await _seed_keystore(hass)
    entry = _cloud_entry(cloud_ingest=False)
    _, m = await _setup(hass, entry)

    state = hass.data[DOMAIN][entry.entry_id]
    assert state.get("edge_queue") is None
    assert not state.get("edge_boxes")
    assert list(state["executors_by_inverter"]) == ["inv-direct"]
    assert not any(isinstance(e, EdgeBoxExecutor) for e in state["executors_by_inverter"].values())
    assert m["harvest"].call_count == 1


@pytest.mark.asyncio
async def test_after_a_reload_the_adopted_box_has_a_queue_and_an_executor(
    hass, enable_custom_integrations
):
    """svitgrid#1252: adopting the first box over the LAN reloads the entry, and
    the reloaded entry drains the box's commands and leaves its inverter to it."""
    import asyncio

    from homeassistant.config_entries import ConfigEntryState

    from custom_components.svitgrid.edge_command_queue import EdgeCommandQueue
    from custom_components.svitgrid.executors.edge_box import EdgeBoxExecutor
    from custom_components.svitgrid.island_lan import SvitgridEdgeAdoptView

    await _seed_keystore(hass)
    entry = _cloud_entry(cloud_ingest=False)
    await _setup(hass, entry)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert hass.data[DOMAIN][entry.entry_id].get("edge_queue") is None

    signed = {
        "action": "edge_adopt",
        "deviceId": "EC-1",
        "inverters": [{"inverterId": "inv-direct", "modelId": "deye_sg04lp3"}],
    }
    request = MagicMock()
    request.app = {"hass": hass}
    with (
        patch(
            "custom_components.svitgrid.island_lan.signed_lan_request",
            AsyncMock(return_value=(signed, None)),
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as schedule_reload,
    ):
        resp = await SvitgridEdgeAdoptView().post(request)
        await asyncio.sleep(0)
    assert resp.status == 200
    schedule_reload.assert_called_once_with(entry.entry_id)

    # The reload: the entry's state goes, and setup runs again.
    hass.data[DOMAIN].pop(entry.entry_id)
    with patch.object(entry, "add_to_hass"):
        _, m = await _setup(hass, entry)

    state = hass.data[DOMAIN][entry.entry_id]
    assert isinstance(state["edge_queue"], EdgeCommandQueue)
    assert state["edge_boxes"] == ["EC-1"]
    executor = state["executors_by_inverter"]["inv-direct"]
    assert isinstance(executor, EdgeBoxExecutor)
    assert executor._queue is state["edge_queue"]
    m["harvest"].assert_not_called()


def _edge_move_entry() -> MockConfigEntry:
    """The local entry an edge move's pairing creates (svitgrid#1223): no
    inverters of its own, because the boxes read them."""
    priv, pub = generate_keypair()
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Дім",
        data={
            "local_only": True,
            "cloud_ingest_enabled": False,
            "edge_move": True,
            "signing_key_id": "ha-local01",
            "private_key_pem": serialize_private_key(priv),
            "public_key_hex": pub,
            "trusted_keys": [],
            "inverters": [],
        },
        entry_id="e-edge",
    )


@pytest.mark.asyncio
async def test_an_edge_move_entry_without_a_box_starts_its_store_and_views(
    hass, enable_custom_integrations
):
    """Bench 2026-10-30: the entry set up with 0 inverters, so nothing
    registered the views and the app's /edge/adopt got Home Assistant's 404."""
    await _seed_keystore(hass)
    entry = _edge_move_entry()
    await _setup(hass, entry)

    assert hass.data[DOMAIN].get("_views_registered") is True
    assert hass.data[DOMAIN][entry.entry_id]["store"] is not None


@pytest.mark.asyncio
async def test_an_edge_move_entry_reads_its_boxes_inverters_and_keeps_their_readings(
    hass, enable_custom_integrations
):
    from custom_components.svitgrid.executors.edge_box import EdgeBoxExecutor
    from custom_components.svitgrid.reading_store import ReadingStore

    ks = await _seed_keystore(hass)
    await _adopt_box(ks, "EC-1", "inv-edge-1")
    entry = _edge_move_entry()
    with patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)) as prune:
        _, m = await _setup(hass, entry)

    state = hass.data[DOMAIN][entry.entry_id]
    assert isinstance(state["executors_by_inverter"]["inv-edge-1"], EdgeBoxExecutor)
    # A prune that did not know the box's inverter would delete its readings.
    assert "inv-edge-1" in prune.call_args.args[0]
    assert m["harvest"].call_count == 0
