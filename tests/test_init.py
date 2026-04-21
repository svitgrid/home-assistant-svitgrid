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
