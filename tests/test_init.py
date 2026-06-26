"""Full-wiring tests: YAML config → async_setup → both loops scheduled."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.setup import async_setup_component

from custom_components.svitgrid.const import DOMAIN
from custom_components.svitgrid.reading_store import ReadingStore

# Default lifecycle the unset-meta store returns; the HA test harness blocks the
# store's real SQLite open, so we patch get_lifecycle to this by default.
_ACTIVE_LIFECYCLE = {"state": "active", "reason": None, "since": None}


@pytest.fixture(autouse=True)
def _stub_local_store_side_effects():
    """The local-store wiring (Task 9) starts a real sender loop and registers
    HTTP views during setup. The YAML-path `hass` fixture has no `hass.http`,
    and a live sender against a mock client is noise — stub both. Tests that
    care assert on their OWN explicit patches (nested patch wins in-scope).
    Also stubs register_panel/remove_panel (SP2 Task 2) since the YAML-path
    hass fixture has no hass.http and panel_custom is a real HA subsystem.

    SP2 Task 9 also seeds the shared lifecycle from store.get_lifecycle(), which
    opens the SQLite file — blocked by the HA harness — so default it to active.
    The deprovisioned test overrides this with its own in-scope patch."""
    with patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock), \
         patch("custom_components.svitgrid.register_views"), \
         patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock), \
         patch("custom_components.svitgrid.remove_panel"), \
         patch.object(ReadingStore, "get_lifecycle", AsyncMock(return_value=_ACTIVE_LIFECYCLE)), \
         patch.object(ReadingStore, "prune_inverters_not_in", AsyncMock(return_value=0)):
        yield


@pytest.mark.asyncio
async def test_setup_with_no_saved_state_runs_bootstrap(hass, enable_custom_integrations):
    config = {
        DOMAIN: {
            "api_base": "https://api.example",
            "device_id": "dev-1",
            "signing_key_id": "key-1",
            "entity_map": {
                "batterySoc": "sensor.soc",
                "batteryPower": "sensor.batt_power",
                "batteryVoltage": "sensor.bv",
                "pv1Power": "sensor.pv1",
                "gridPower": "sensor.grid",
                "loadPower": "sensor.load",
            },
        }
    }

    with patch("custom_components.svitgrid.SvitgridApiClient", autospec=True) as mock_client_cls:
        client = mock_client_cls.return_value
        client.bootstrap = AsyncMock(
            return_value={"apiKey": "k" * 64, "trustedKeyIds": [], "inverters": []}
        )
        client.push_reading = AsyncMock()
        client.poll_commands = AsyncMock(return_value={"commands": []})

        result = await async_setup_component(hass, DOMAIN, config)
        assert result is True
        client.bootstrap.assert_called_once()


@pytest.mark.asyncio
async def test_setup_rejects_missing_required_field(hass, enable_custom_integrations):
    config = {
        DOMAIN: {
            "api_base": "https://api.example",
            "device_id": "dev-1",
            "signing_key_id": "key-1",
            "entity_map": {
                # Missing loadPower
                "batterySoc": "sensor.soc",
                "batteryPower": "sensor.batt_power",
                "batteryVoltage": "sensor.bv",
                "pv1Power": "sensor.pv1",
                "gridPower": "sensor.grid",
            },
        }
    }
    result = await async_setup_component(hass, DOMAIN, config)
    assert result is False


@pytest.mark.asyncio
async def test_setup_skips_bootstrap_when_state_already_saved(hass, enable_custom_integrations):
    """If keystore already has saved state, don't re-bootstrap."""
    from cryptography.hazmat.primitives import serialization

    from custom_components.svitgrid.keystore import SvitgridKeystore
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="existing-key",
        public_key_hex=pub_hex,
        private_key_pem=pem,
        signing_key_id="sk",
        trusted_key_ids=["sk"],
    )

    config = {
        DOMAIN: {
            "api_base": "https://api.example",
            "device_id": "dev-1",
            "signing_key_id": "sk",
            "entity_map": {
                "batterySoc": "sensor.soc",
                "batteryPower": "sensor.bp",
                "batteryVoltage": "sensor.bv",
                "pv1Power": "sensor.pv1",
                "gridPower": "sensor.gp",
                "loadPower": "sensor.lp",
            },
        }
    }
    with patch("custom_components.svitgrid.SvitgridApiClient", autospec=True) as mock_client_cls:
        client = mock_client_cls.return_value
        client.bootstrap = AsyncMock()
        client.push_reading = AsyncMock()
        client.poll_commands = AsyncMock(return_value={"commands": []})

        result = await async_setup_component(hass, DOMAIN, config)
        assert result is True
        client.bootstrap.assert_not_called()


@pytest.mark.asyncio
async def test_setup_with_smg_ii_executor(hass, enable_custom_integrations):
    """svitgrid.executor: {type: smg_ii, modbus_hub, modbus_slave,
    battery_nominal_voltage} instantiates SmgIiExecutor and threads it
    through to hass.data."""
    from cryptography.hazmat.primitives import serialization

    from custom_components.svitgrid.keystore import SvitgridKeystore
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="existing-key",
        public_key_hex=pub_hex,
        private_key_pem=pem,
        signing_key_id="sk",
        trusted_key_ids=[],
        trusted_public_keys_hex={},
    )

    config = {
        DOMAIN: {
            "api_base": "https://api.example",
            "device_id": "dev-1",
            "signing_key_id": "sk",
            "entity_map": {
                "batterySoc": "sensor.soc",
                "batteryPower": "sensor.bp",
                "batteryVoltage": "sensor.bv",
                "pv1Power": "sensor.pv1",
                "gridPower": "sensor.gp",
                "loadPower": "sensor.lp",
            },
            "executor": {
                "type": "smg_ii",
                "modbus_hub": "my_hub",
                "modbus_slave": 1,
                "battery_nominal_voltage": 48,
            },
        }
    }

    with patch("custom_components.svitgrid.SvitgridApiClient", autospec=True) as mock_client_cls:
        client = mock_client_cls.return_value
        client.bootstrap = AsyncMock()
        client.push_reading = AsyncMock()
        client.poll_commands = AsyncMock(return_value={"commands": []})

        from homeassistant.setup import async_setup_component

        result = await async_setup_component(hass, DOMAIN, config)
        assert result is True

        from custom_components.svitgrid.executors.smg_ii import SmgIiExecutor

        assert isinstance(hass.data[DOMAIN]["executor"], SmgIiExecutor)


@pytest.mark.asyncio
async def test_setup_without_executor_defaults_to_read_only(hass, enable_custom_integrations):
    """Missing executor: block → no executor, read-only (v0.1.0 behavior)."""
    from cryptography.hazmat.primitives import serialization

    from custom_components.svitgrid.keystore import SvitgridKeystore
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    ks = SvitgridKeystore(hass)
    await ks.save(
        api_key="k",
        public_key_hex=pub_hex,
        private_key_pem=pem,
        signing_key_id="sk",
        trusted_key_ids=[],
        trusted_public_keys_hex={},
    )

    config = {
        DOMAIN: {
            "api_base": "https://api.example",
            "device_id": "dev-1",
            "signing_key_id": "sk",
            "entity_map": {
                "batterySoc": "sensor.soc",
                "batteryPower": "sensor.bp",
                "batteryVoltage": "sensor.bv",
                "pv1Power": "sensor.pv1",
                "gridPower": "sensor.gp",
                "loadPower": "sensor.lp",
            },
            # NOTE: no executor: block
        }
    }

    with patch("custom_components.svitgrid.SvitgridApiClient", autospec=True) as mock_client_cls:
        client = mock_client_cls.return_value
        client.bootstrap = AsyncMock()
        client.push_reading = AsyncMock()
        client.poll_commands = AsyncMock(return_value={"commands": []})

        from homeassistant.setup import async_setup_component

        result = await async_setup_component(hass, DOMAIN, config)
        assert result is True
        assert hass.data[DOMAIN]["executor"] is None


@pytest.mark.asyncio
async def test_setup_loads_trusted_keys_from_keystore_state(hass, enable_custom_integrations):
    """On restart, trusted_public_keys_hex should be loaded from keystore
    (populated earlier by bootstrap + add_trusted_key commands), not
    initialized to empty. Validates the Task 5 → Task 8 handoff."""
    from cryptography.hazmat.primitives import serialization

    from custom_components.svitgrid.keystore import SvitgridKeystore
    from custom_components.svitgrid.signing import generate_keypair

    priv, pub_hex = generate_keypair()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    ks = SvitgridKeystore(hass)
    stored_keys = {
        "admin-1": "04" + "11" * 64,
        "admin-2": "04" + "22" * 64,
    }
    await ks.save(
        api_key="k",
        public_key_hex=pub_hex,
        private_key_pem=pem,
        signing_key_id="sk",
        trusted_key_ids=["admin-1", "admin-2"],
        trusted_public_keys_hex=stored_keys,
    )

    config = {
        DOMAIN: {
            "api_base": "https://api.example",
            "device_id": "dev-1",
            "signing_key_id": "sk",
            "entity_map": {
                "batterySoc": "sensor.soc",
                "batteryPower": "sensor.bp",
                "batteryVoltage": "sensor.bv",
                "pv1Power": "sensor.pv1",
                "gridPower": "sensor.gp",
                "loadPower": "sensor.lp",
            },
        }
    }

    with patch("custom_components.svitgrid.SvitgridApiClient", autospec=True) as mock_client_cls:
        client = mock_client_cls.return_value
        client.bootstrap = AsyncMock()
        client.push_reading = AsyncMock()
        client.poll_commands = AsyncMock(return_value={"commands": []})

        from homeassistant.setup import async_setup_component

        result = await async_setup_component(hass, DOMAIN, config)
        assert result is True

        # The cache dict is stored alongside the executor in hass.data
        assert hass.data[DOMAIN]["trusted_public_keys_hex"] == stored_keys


# ---------------------------------------------------------------------------
# Config-entry path (Tasks 10-14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_starts_publisher_and_poller(hass, enable_custom_integrations):
    """Setting up from a config entry boots both background loops (v2 multi-inverter)."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from unittest.mock import AsyncMock, patch

    from custom_components.svitgrid import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (h-abc)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-abc",
            "signing_key_id": "ha-home-01",
            "private_key_pem": (
                "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
            ),
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
                }
            ],
        },
        entry_id="test-entry-id",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock
        ) as rp,
        patch(
            "custom_components.svitgrid.run_command_loop", new_callable=AsyncMock
        ) as cp,
        patch(
            "custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock
        ),
        patch(
            "custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock
        ) as sender,
        patch("custom_components.svitgrid.register_views") as reg_views,
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)),
    ):
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True

    entry_state = hass.data[DOMAIN][entry.entry_id]
    # v2: readings are a dict keyed by inverter_id, not a single task
    assert "readings_tasks" in entry_state
    assert "ha-xyz" in entry_state["readings_tasks"]
    assert entry_state["readings_tasks"]["ha-xyz"] is not None
    assert entry_state.get("command_task") is not None
    assert entry_state.get("mqtt_wake_task") is not None

    # local store wiring: store created, sender loop + rollup timer started,
    # read views registered.
    from custom_components.svitgrid.reading_store import ReadingStore

    assert isinstance(entry_state.get("store"), ReadingStore)
    assert entry_state.get("sender_task") is not None
    assert callable(entry_state.get("cancel_rollup"))
    assert sender.call_count == 1
    assert reg_views.call_count == 1

    # run_loop coroutines were scheduled (called once each to get the coroutine)
    assert rp.call_count == 1
    assert cp.call_count == 1

    # readings loop received the right inverter_id, store, and cadence
    # (api_key/api_client moved to the sender; the publisher no longer
    # talks to the cloud).
    rp_kwargs = rp.call_args.kwargs
    assert rp_kwargs["inverter_id"] == "ha-xyz"
    assert "api_key" not in rp_kwargs
    assert "api_client" not in rp_kwargs
    assert rp_kwargs["store"] is entry_state["store"]
    assert "cadence" in rp_kwargs

    # the sender received the api_key + api_client instead
    sender_kwargs = sender.call_args.kwargs
    assert sender_kwargs["api_key"] == "test-key"

    # command loop received keystore=None and entry_data with the key material
    cp_kwargs = cp.call_args.kwargs
    assert cp_kwargs["keystore"] is None
    assert cp_kwargs["entry_data"]["signing_key_id"] == "ha-home-01"


@pytest.mark.asyncio
async def test_async_unload_entry_cancels_tasks(hass, enable_custom_integrations):
    """async_unload_entry cancels all running background tasks (v2 multi-inverter)."""
    import asyncio
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from unittest.mock import AsyncMock, patch

    from custom_components.svitgrid import async_setup_entry, async_unload_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (h-abc)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-abc",
            "signing_key_id": "ha-home-01",
            "private_key_pem": (
                "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
            ),
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
                }
            ],
        },
        entry_id="test-entry-id-2",
    )
    entry.add_to_hass(hass)

    async def _never_return(**kwargs):
        await asyncio.Event().wait()  # blocks until cancelled

    with (
        patch(
            "custom_components.svitgrid.run_readings_loop", side_effect=_never_return
        ),
        patch(
            "custom_components.svitgrid.run_command_loop", side_effect=_never_return
        ),
        patch(
            "custom_components.svitgrid.run_mqtt_wake_loop", side_effect=_never_return
        ),
        patch(
            "custom_components.svitgrid.run_sender_loop", side_effect=_never_return
        ),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)),
        patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)),
    ):
        await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

        assert entry.entry_id in hass.data[DOMAIN]

        # Capture the tasks before unload so we can assert they were cancelled
        state_before = hass.data[DOMAIN][entry.entry_id]
        readings_tasks = list(state_before["readings_tasks"].values())
        command_task = state_before["command_task"]
        mqtt_wake_task = state_before["mqtt_wake_task"]
        sender_task = state_before["sender_task"]

        # Replace the cancel_rollup callback with a MagicMock so we can
        # assert it was called during unload.
        from unittest.mock import MagicMock
        cancel_rollup_mock = MagicMock()
        hass.data[DOMAIN][entry.entry_id]["cancel_rollup"] = cancel_rollup_mock

        ok = await async_unload_entry(hass, entry)
        # Let the event loop process the CancelledError injections
        await hass.async_block_till_done()

    assert ok is True
    # entry is removed from hass.data after unload
    assert entry.entry_id not in hass.data[DOMAIN]
    # all per-inverter readings tasks were cancelled
    for task in readings_tasks:
        assert task.cancelled()
    # shared command and mqtt-wake tasks were cancelled
    assert command_task.cancelled()
    assert mqtt_wake_task.cancelled()
    # the local-store sender task was also cancelled
    assert sender_task.cancelled()
    # the rollup timer was cancelled via its cancel callback
    cancel_rollup_mock.assert_called_once()


@pytest.mark.asyncio
async def test_async_setup_entry_passes_preset_entity_map_to_publisher(hass, enable_custom_integrations):
    """Phase 2 / v2 shape: when the config entry has an inverters list with an
    entity_map (from a preset carried through /finalize), the readings publisher
    must be called with that map and the correct inverter_id. Otherwise readings
    would post empty payloads and the API would 400 them all."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from unittest.mock import AsyncMock, patch

    from custom_components.svitgrid import async_setup_entry

    preset_map = {
        "batterySoc": "sensor.inverter_battery",
        "loadPower": "sensor.inverter_load_power",
    }

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid — Deye SG04LP3",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-deye",
            "signing_key_id": "ha-home-01",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-deye-001",
                    "entity_map": preset_map,
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": "deye-sg04lp3-solarman-v1",
                }
            ],
        },
        entry_id="entry-with-preset",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock
        ) as rp,
        patch(
            "custom_components.svitgrid.run_command_loop", new_callable=AsyncMock
        ),
        patch(
            "custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock
        ),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)),
    ):
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True

    # The publisher must have been called with the preset's entity_map verbatim.
    assert rp.call_count == 1
    assert rp.call_args.kwargs["entity_map"] == preset_map
    assert rp.call_args.kwargs["inverter_id"] == "ha-deye-001"


@pytest.mark.asyncio
async def test_setup_prefers_options_entity_map(hass, enable_custom_integrations):
    """async_setup_entry uses entry.options['entity_map'] over the pairing-time
    entity_map stored in inverters[0]['entity_map'] (legacy options override
    via _inverters_from_entry back-compat path)."""
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid import async_setup_entry

    entry = MockConfigEntry(
        domain="svitgrid",
        version=2,
        data={
            "api_base": "https://example.test",
            "api_key": "k",
            "edge_device_id": "dev1",
            "household_id": "hh1",
            "signing_key_id": "sk1",
            "private_key_pem": "pem",
            "public_key_hex": "ff",
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "hw1",
                    "entity_map": {"batterySoc": "sensor.from_data"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": None,
                    "model": None,
                    "phases": None,
                    "has_battery": None,
                    "pv_strings": None,
                    "preset_id": None,
                }
            ],
        },
        options={"entity_map": {"batterySoc": "sensor.from_options"}},
    )
    entry.add_to_hass(hass)

    captured = {}

    async def _fake_loop(**kwargs):
        captured["entity_map"] = kwargs.get("entity_map")

    with patch("custom_components.svitgrid.run_readings_loop", _fake_loop), \
         patch("custom_components.svitgrid.run_command_loop", AsyncMock()), \
         patch("custom_components.svitgrid.run_mqtt_wake_loop", AsyncMock()), \
         patch("custom_components.svitgrid.run_sender_loop", AsyncMock()), \
         patch("custom_components.svitgrid.register_views"), \
         patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock), \
         patch("custom_components.svitgrid.remove_panel"), \
         patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()):
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    assert captured["entity_map"] == {"batterySoc": "sensor.from_options"}


@pytest.mark.asyncio
async def test_options_change_reloads_entry(hass, enable_custom_integrations):
    """Updating entry.options fires the update listener, reloading the entry."""
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid import async_setup_entry

    entry = MockConfigEntry(
        domain="svitgrid",
        data={
            "api_base": "https://example.test",
            "api_key": "k",
            "edge_device_id": "dev1",
            "hardware_id": "hw1",
            "household_id": "hh1",
            "signing_key_id": "sk1",
            "private_key_pem": "pem",
            "public_key_hex": "ff",
            "trusted_keys": [],
            "preset_id": None,
            "entity_map": {"batterySoc": "sensor.soc"},
        },
    )
    entry.add_to_hass(hass)

    with patch("custom_components.svitgrid.run_readings_loop", AsyncMock()), \
         patch("custom_components.svitgrid.run_command_loop", AsyncMock()), \
         patch("custom_components.svitgrid.run_mqtt_wake_loop", AsyncMock()), \
         patch("custom_components.svitgrid.run_sender_loop", AsyncMock()), \
         patch("custom_components.svitgrid.register_views"), \
         patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock), \
         patch("custom_components.svitgrid.remove_panel"), \
         patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()):
        await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

        with patch.object(
            hass.config_entries, "async_reload", AsyncMock()
        ) as mock_reload:
            hass.config_entries.async_update_entry(
                entry, options={"entity_map": {"batterySoc": "sensor.new"}}
            )
            await hass.async_block_till_done()

    mock_reload.assert_called_once_with(entry.entry_id)


# ---------------------------------------------------------------------------
# Panel wiring assertions (Task 2 / Sub-project 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_calls_register_panel(hass, enable_custom_integrations):
    """register_panel must be awaited exactly once during async_setup_entry
    (after the store stack starts, guarded by panel.py's idempotency flag)."""
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (h-panel-test)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-panel",
            "signing_key_id": "ha-home-01",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-panel-inv",
                    "entity_map": {"batterySoc": "sensor.soc"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                }
            ],
        },
        entry_id="entry-panel-wiring",
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock) as mock_register_panel,
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)),
    ):
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    mock_register_panel.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_unload_entry_calls_remove_panel(hass, enable_custom_integrations):
    """remove_panel must be called once during async_unload_entry."""
    import asyncio
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid import async_setup_entry, async_unload_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (h-unload-panel)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-unload",
            "signing_key_id": "ha-home-01",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-unload-inv",
                    "entity_map": {"batterySoc": "sensor.soc"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                }
            ],
        },
        entry_id="entry-panel-unload",
    )
    entry.add_to_hass(hass)

    async def _never_return(**kwargs):
        await asyncio.Event().wait()

    with (
        patch("custom_components.svitgrid.run_readings_loop", side_effect=_never_return),
        patch("custom_components.svitgrid.run_command_loop", side_effect=_never_return),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", side_effect=_never_return),
        patch("custom_components.svitgrid.run_sender_loop", side_effect=_never_return),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel") as mock_remove_panel,
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)),
        patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)),
    ):
        await async_setup_entry(hass, entry)
        await hass.async_block_till_done()
        ok = await async_unload_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    mock_remove_panel.assert_called_once()


@pytest.mark.asyncio
async def test_deprovisioned_at_startup_skips_loops(hass, enable_custom_integrations):
    """When the persisted lifecycle is 'deprovisioned', no background loops are
    started, but the panel/views/sensors are still set up."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from unittest.mock import AsyncMock, patch

    from custom_components.svitgrid import async_setup_entry
    from custom_components.svitgrid.reading_store import ReadingStore

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (h-abc)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-abc",
            "signing_key_id": "ha-home-01",
            "private_key_pem": (
                "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
            ),
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
                }
            ],
        },
        entry_id="test-entry-id-depro",
    )
    entry.add_to_hass(hass)

    deprovisioned = {
        "state": "deprovisioned",
        "reason": "revoked",
        "since": "2026-06-25T10:00:00Z",
    }

    with (
        patch.object(
            ReadingStore, "get_lifecycle", AsyncMock(return_value=deprovisioned)
        ),
        patch(
            "custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock
        ) as rp,
        patch(
            "custom_components.svitgrid.run_command_loop", new_callable=AsyncMock
        ) as cp,
        patch(
            "custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock
        ),
        patch(
            "custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock
        ) as sender,
        patch("custom_components.svitgrid.register_views") as reg_views,
        patch(
            "custom_components.svitgrid.register_panel", new_callable=AsyncMock
        ) as reg_panel,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ) as fwd,
    ):
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True

    # No background loops started while deprovisioned.
    assert rp.await_count == 0
    assert sender.await_count == 0
    assert cp.await_count == 0

    # Panel / views / sensors still set up.
    assert reg_views.call_count == 1
    reg_panel.assert_awaited_once()
    fwd.assert_awaited_once()

    entry_state = hass.data[DOMAIN][entry.entry_id]
    assert entry_state.get("readings_tasks") == {}
    assert entry_state.get("command_task") is None
    assert entry_state.get("sender_task") is None
    assert entry_state.get("lifecycle") is not None
    assert entry_state["lifecycle"].state == "deprovisioned"

    # C3: verify the ActivityTracker surfaces also reflect deprovisioned so
    # the status sensor and binary_sensor show the real state after restart.
    activity = entry_state.get("activity")
    assert activity is not None
    assert activity.lifecycle_state == "deprovisioned"
    assert activity.status == "deprovisioned"


@pytest.mark.asyncio
async def test_async_setup_entry_prunes_orphaned_inverter_rows(hass, enable_custom_integrations):
    """prune_inverters_not_in must be awaited once with the entry's active inverter-id
    set BEFORE the sender loop starts. This guards against head-of-line blocking
    when the inverter id changes after re-pairing."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from unittest.mock import AsyncMock, patch, call

    from custom_components.svitgrid import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (h-prune-test)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-prune",
            "signing_key_id": "ha-home-01",
            "private_key_pem": (
                "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
            ),
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-prune-inv",
                    "entity_map": {"batterySoc": "sensor.soc"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": None,
                }
            ],
        },
        entry_id="entry-prune-test",
    )
    entry.add_to_hass(hass)

    sender_call_order = []

    async def _track_sender(**kwargs):
        sender_call_order.append("sender")

    prune_mock = AsyncMock(return_value=0)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", side_effect=_track_sender),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(ReadingStore, "prune_inverters_not_in", prune_mock),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)),
    ):
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True
    # prune must be called exactly once with the entry's active inverter-id set
    prune_mock.assert_awaited_once_with({"ha-prune-inv"})


@pytest.mark.asyncio
async def test_paused_at_startup_starts_loops(hass, enable_custom_integrations):
    """C2: When the persisted lifecycle is 'paused' (not deprovisioned), all
    background loops ARE started so the command poller can detect an operator
    re-enable. The binary_sensor surfaces reflect paused via the ActivityTracker."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from unittest.mock import AsyncMock, patch

    from custom_components.svitgrid import async_setup_entry
    from custom_components.svitgrid.reading_store import ReadingStore

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (h-paused)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-paused",
            "signing_key_id": "ha-home-01",
            "private_key_pem": (
                "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
            ),
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
                }
            ],
        },
        entry_id="test-entry-id-paused",
    )
    entry.add_to_hass(hass)

    paused = {
        "state": "paused",
        "reason": "disabled",
        "since": "2026-06-25T10:00:00Z",
    }

    with (
        patch.object(
            ReadingStore, "get_lifecycle", AsyncMock(return_value=paused)
        ),
        patch(
            "custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock
        ) as rp,
        patch(
            "custom_components.svitgrid.run_command_loop", new_callable=AsyncMock
        ) as cp,
        patch(
            "custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock
        ),
        patch(
            "custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock
        ) as sender,
        patch("custom_components.svitgrid.register_views"),
        patch(
            "custom_components.svitgrid.register_panel", new_callable=AsyncMock
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            AsyncMock(return_value=True),
        ),
    ):
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True

    # Paused is NOT terminal — loops must start so the command poller can detect
    # an operator re-enable.
    assert rp.await_count == 1, "readings loop must start when paused"
    assert cp.await_count == 1, "command loop must start when paused"
    assert sender.await_count == 1, "sender loop must start when paused"

    entry_state = hass.data[DOMAIN][entry.entry_id]
    assert entry_state["lifecycle"].state == "paused"

    # ActivityTracker must reflect paused immediately (C1 seed mirror).
    activity = entry_state.get("activity")
    assert activity is not None
    assert activity.lifecycle_state == "paused"
    assert activity.status == "paused"


# ---------------------------------------------------------------------------
# E2: apply_cloud_endpoint_change helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cloud_endpoint_change_updates_entry_and_schedules_reload(
    hass,
):
    """The helper mutates ConfigEntry.data and schedules (not directly
    invokes) async_reload, so the calling task can finish cleanly before
    the reload tears it down."""
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid import apply_cloud_endpoint_change
    from custom_components.svitgrid.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="test",
        data={
            "api_base": "https://api-staging.svitgrid.app",
            "api_key": "k",
            "edge_device_id": "d",
            "household_id": "h",
        },
        entry_id="e1",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.svitgrid.probe_endpoint_auth",
        new_callable=AsyncMock,
        return_value=True,
    ), patch.object(
        hass.config_entries, "async_reload"
    ) as mock_reload:
        result = await apply_cloud_endpoint_change(
            hass, entry, "https://api.svitgrid.app"
        )

    assert result is True
    # Entry data updated immediately + atomically:
    assert entry.data["api_base"] == "https://api.svitgrid.app"
    # Reload scheduled, not directly awaited (the test patched async_reload
    # — if it had been awaited, the patch.assert_awaited would pass too;
    # we just need it to have been called at least once on the event loop):
    await hass.async_block_till_done()
    mock_reload.assert_called_once_with("e1")


@pytest.mark.asyncio
async def test_apply_cloud_endpoint_change_is_noop_when_unchanged(hass):
    """If the new URL equals the current one, skip update + reload —
    a redundant migration command shouldn't bounce a healthy integration."""
    from unittest.mock import patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid import apply_cloud_endpoint_change
    from custom_components.svitgrid.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="test",
        data={
            "api_base": "https://api-staging.svitgrid.app",
            "api_key": "k",
            "edge_device_id": "d",
            "household_id": "h",
        },
        entry_id="e2",
    )
    entry.add_to_hass(hass)

    with patch.object(
        hass.config_entries, "async_reload"
    ) as mock_reload, patch.object(
        hass.config_entries, "async_update_entry"
    ) as mock_update:
        result = await apply_cloud_endpoint_change(
            hass, entry, "https://api-staging.svitgrid.app"
        )

    assert result is True
    mock_reload.assert_not_called()
    mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# E-fix: pre-flight probe on apply_cloud_endpoint_change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_cloud_endpoint_change_returns_true_on_probe_success(hass):
    """When the pre-flight probe to the new endpoint returns 200, the helper
    must mutate ConfigEntry, schedule reload, and return True."""
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid import apply_cloud_endpoint_change
    from custom_components.svitgrid.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="test",
        data={
            "api_base": "https://api-staging.svitgrid.app",
            "api_key": "test-api-key",
            "edge_device_id": "d",
            "household_id": "h",
        },
        entry_id="probe-ok",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.svitgrid.probe_endpoint_auth",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock_probe, patch.object(
        hass.config_entries, "async_reload"
    ) as mock_reload:
        result = await apply_cloud_endpoint_change(
            hass, entry, "https://api.svitgrid.app"
        )

    assert result is True
    # Entry data must have been mutated
    assert entry.data["api_base"] == "https://api.svitgrid.app"
    # Reload must have been scheduled
    await hass.async_block_till_done()
    mock_reload.assert_called_once_with("probe-ok")
    # Probe must have been called with the new URL and our api_key
    mock_probe.assert_awaited_once()
    call_args = mock_probe.await_args
    assert call_args.kwargs.get("api_key") == "test-api-key" or call_args.args[1] == "test-api-key"
    # new_api_base should be the new URL
    new_base_arg = call_args.kwargs.get("new_api_base") or call_args.args[-1]
    assert new_base_arg == "https://api.svitgrid.app"


# ---------------------------------------------------------------------------
# Task 3: preset entity-map refresh wired into async_setup_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preset_refresh_at_setup_merges_new_fields(hass, enable_custom_integrations):
    """When get_preset returns a newer-version preset with extra fields, setup
    persists the merged entity_map and merged_preset_version into the entry,
    and the publisher receives the refreshed entity_map."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from unittest.mock import AsyncMock, patch

    from custom_components.svitgrid import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (preset-refresh)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-refresh",
            "signing_key_id": "ha-home-01",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [
                {
                    "inverter_id": "ha-refresh-inv",
                    "entity_map": {"batterySoc": "sensor.soc"},
                    "command_recipes": [],
                    "command_config": {},
                    "brand": "Deye",
                    "model": "SG04LP3",
                    "phases": 3,
                    "has_battery": True,
                    "pv_strings": 2,
                    "preset_id": "deye-sg04lp3-v1",
                    # no merged_preset_version → first-time merge
                }
            ],
        },
        entry_id="entry-preset-refresh",
    )
    entry.add_to_hass(hass)

    # Preset returns version 2 with an extra field the entry doesn't have yet.
    newer_preset = {
        "version": 2,
        "entityMap": {
            "batterySoc": "sensor.soc",      # already present — should not overwrite
            "loadPower": "sensor.load_power", # new field
        },
    }

    captured_entity_map = {}

    async def _fake_readings_loop(**kwargs):
        captured_entity_map["entity_map"] = kwargs.get("entity_map")

    with (
        patch("custom_components.svitgrid.run_readings_loop", _fake_readings_loop),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)),
    ):
        # Patch api_client.get_preset AFTER the client is constructed inside setup.
        # We do it by patching SvitgridApiClient so the instance returned has
        # get_preset mocked to return our newer preset.
        with patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls:
            mock_instance = mock_cls.return_value
            mock_instance.get_preset = AsyncMock(return_value=newer_preset)

            ok = await async_setup_entry(hass, entry)
            await hass.async_block_till_done()

    assert ok is True

    # Entry's stored inverter must have the new field + merged_preset_version set.
    stored_inverters = entry.data["inverters"]
    assert len(stored_inverters) == 1
    inv = stored_inverters[0]
    assert "loadPower" in inv["entity_map"], "new field must be merged into entity_map"
    assert inv["entity_map"]["batterySoc"] == "sensor.soc", "existing field must be preserved"
    assert inv.get("merged_preset_version") == 2, "merged_preset_version must be persisted"

    # Publisher must have received the refreshed entity_map (with the new field).
    assert "loadPower" in captured_entity_map.get("entity_map", {}), \
        "publisher must see the refreshed entity_map"


@pytest.mark.asyncio
async def test_preset_refresh_fail_open_at_setup(hass, enable_custom_integrations):
    """When get_preset raises, setup still completes and the entry is unchanged."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from unittest.mock import AsyncMock, patch

    from custom_components.svitgrid import async_setup_entry

    original_inverter = {
        "inverter_id": "ha-failopen-inv",
        "entity_map": {"batterySoc": "sensor.soc"},
        "command_recipes": [],
        "command_config": {},
        "brand": "Deye",
        "model": "SG04LP3",
        "phases": 3,
        "has_battery": True,
        "pv_strings": 2,
        "preset_id": "deye-sg04lp3-v1",
    }

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Svitgrid (preset-failopen)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "household_id": "h-failopen",
            "signing_key_id": "ha-home-01",
            "private_key_pem": "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n",
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "inverters": [original_inverter],
        },
        entry_id="entry-preset-failopen",
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_command_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_mqtt_wake_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.run_sender_loop", new_callable=AsyncMock),
        patch("custom_components.svitgrid.register_views"),
        patch("custom_components.svitgrid.register_panel", new_callable=AsyncMock),
        patch("custom_components.svitgrid.remove_panel"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True)),
    ):
        with patch("custom_components.svitgrid.SvitgridApiClient") as mock_cls:
            mock_instance = mock_cls.return_value
            mock_instance.get_preset = AsyncMock(side_effect=RuntimeError("network error"))

            ok = await async_setup_entry(hass, entry)
            await hass.async_block_till_done()

    assert ok is True, "setup must succeed even when get_preset raises"
    # Entry must be unchanged — fail-open, no update_entry.
    assert entry.data["inverters"][0]["entity_map"] == {"batterySoc": "sensor.soc"}
    assert "merged_preset_version" not in entry.data["inverters"][0]


@pytest.mark.asyncio
async def test_apply_cloud_endpoint_change_returns_false_on_probe_failure(
    hass, caplog,
):
    """When the pre-flight probe returns False (non-200), the helper must NOT
    mutate ConfigEntry, NOT schedule reload, log a distinctive ERROR, and
    return False."""
    import logging
    from unittest.mock import AsyncMock, patch
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.svitgrid import apply_cloud_endpoint_change
    from custom_components.svitgrid.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        title="test",
        data={
            "api_base": "https://api-staging.svitgrid.app",
            "api_key": "test-api-key",
            "edge_device_id": "d",
            "household_id": "h",
        },
        entry_id="probe-fail",
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.svitgrid.probe_endpoint_auth",
        new_callable=AsyncMock,
        return_value=False,
    ), patch.object(
        hass.config_entries, "async_reload"
    ) as mock_reload, patch.object(
        hass.config_entries, "async_update_entry"
    ) as mock_update, caplog.at_level(
        logging.ERROR, logger="custom_components.svitgrid"
    ):
        result = await apply_cloud_endpoint_change(
            hass, entry, "https://api.svitgrid.app"
        )

    assert result is False
    # ConfigEntry must NOT have been mutated
    assert entry.data["api_base"] == "https://api-staging.svitgrid.app"
    mock_update.assert_not_called()
    mock_reload.assert_not_called()
    # Distinctive grep-friendly error log
    assert any(
        "set_cloud_endpoint probe failed" in record.message
        and record.levelno == logging.ERROR
        for record in caplog.records
    ), f"Expected distinctive error log. Records: {[r.message for r in caplog.records]}"
