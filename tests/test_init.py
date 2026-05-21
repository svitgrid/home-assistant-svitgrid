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
