"""Full-wiring tests: YAML config → async_setup → both loops scheduled."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.setup import async_setup_component

from custom_components.svitgrid.const import DOMAIN


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
    """Setting up from a config entry boots both background loops."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from unittest.mock import AsyncMock, patch

    from custom_components.svitgrid import async_setup_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Svitgrid (h-abc)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "hardware_id": "ha-xyz",
            "household_id": "h-abc",
            "signing_key_id": "ha-home-01",
            "private_key_pem": (
                "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
            ),
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "preset_id": None,
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
    ):
        ok = await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert ok is True

    entry_state = hass.data[DOMAIN][entry.entry_id]
    assert entry_state.get("readings_task") is not None
    assert entry_state.get("command_task") is not None

    # run_loop coroutines were scheduled (called once each to get the coroutine)
    assert rp.call_count == 1
    assert cp.call_count == 1

    # readings loop received the right api_key and inverter_id
    rp_kwargs = rp.call_args.kwargs
    assert rp_kwargs["api_key"] == "test-key"
    assert rp_kwargs["inverter_id"] == "ha-xyz"

    # command loop received keystore=None and entry_data with the key material
    cp_kwargs = cp.call_args.kwargs
    assert cp_kwargs["keystore"] is None
    assert cp_kwargs["entry_data"]["signing_key_id"] == "ha-home-01"


@pytest.mark.asyncio
async def test_async_unload_entry_cancels_tasks(hass, enable_custom_integrations):
    """async_unload_entry cancels the running background tasks."""
    import asyncio
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from unittest.mock import AsyncMock, patch

    from custom_components.svitgrid import async_setup_entry, async_unload_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Svitgrid (h-abc)",
        data={
            "api_base": "https://api.example.com",
            "api_key": "test-key",
            "edge_device_id": "ed-1",
            "hardware_id": "ha-xyz",
            "household_id": "h-abc",
            "signing_key_id": "ha-home-01",
            "private_key_pem": (
                "-----BEGIN PRIVATE KEY-----\nFAKE\n-----END PRIVATE KEY-----\n"
            ),
            "public_key_hex": "04" + "a" * 128,
            "trusted_keys": [],
            "preset_id": None,
        },
        entry_id="test-entry-id-2",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.svitgrid.run_readings_loop", new_callable=AsyncMock
        ),
        patch(
            "custom_components.svitgrid.run_command_loop", new_callable=AsyncMock
        ),
    ):
        await async_setup_entry(hass, entry)
        await hass.async_block_till_done()

    assert entry.entry_id in hass.data[DOMAIN]

    ok = await async_unload_entry(hass, entry)
    assert ok is True
    assert entry.entry_id not in hass.data[DOMAIN]


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
