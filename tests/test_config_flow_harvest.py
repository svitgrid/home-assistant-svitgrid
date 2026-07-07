"""Tests for the Svitgrid config flow `harvest_config` step (SP-B Task 10).

The harvest_config step collects the direct-Modbus harvest spec
(protocol / ip / port / slave_id / model_id / logger_serial) and stores it on
the flow so async_step_pair_finalize threads it into the created inverter dict.
SP-D will later replace this manual entry with a phone handoff.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.svitgrid.config_flow import SvitgridConfigFlow


@pytest.mark.asyncio
async def test_harvest_config_builds_dict(hass: HomeAssistant) -> None:
    """A complete submit stores a snake_case harvest_config dict on the flow."""
    flow = SvitgridConfigFlow()
    flow.hass = hass
    # Isolate the step: don't actually kick off pairing (network) here.
    with patch.object(
        SvitgridConfigFlow,
        "async_step_pair",
        new=AsyncMock(return_value={"type": "progress"}),
    ):
        await flow.async_step_harvest_config(
            {
                "protocol": "solarman_v5",
                "ip": "192.168.1.50",
                "port": 8899,
                "slave_id": 1,
                "model_id": "deye_sg04lp3",
                "logger_serial": "1234567890",
            }
        )

    assert flow._harvest_config == {
        "protocol": "solarman_v5",
        "ip": "192.168.1.50",
        "port": 8899,
        "slave_id": 1,
        "model_id": "deye_sg04lp3",
        "logger_serial": "1234567890",
    }


@pytest.mark.asyncio
async def test_harvest_config_shows_form_without_input(hass: HomeAssistant) -> None:
    """No user_input → render the harvest_config form."""
    flow = SvitgridConfigFlow()
    flow.hass = hass
    result = await flow.async_step_harvest_config(None)
    assert result["type"] == "form"
    assert result["step_id"] == "harvest_config"


@pytest.mark.asyncio
async def test_solarman_requires_logger_serial(hass: HomeAssistant) -> None:
    """solarman_v5 without a logger_serial re-shows the form with an error."""
    flow = SvitgridConfigFlow()
    flow.hass = hass
    result = await flow.async_step_harvest_config(
        {
            "protocol": "solarman_v5",
            "ip": "192.168.1.50",
            "port": 8899,
            "slave_id": 1,
            "model_id": "deye_sg04lp3",
            # no logger_serial
        }
    )
    assert result["type"] == "form"
    assert result.get("errors")
    assert flow._harvest_config is None


@pytest.mark.asyncio
async def test_modbus_tcp_does_not_require_logger_serial(hass: HomeAssistant) -> None:
    """modbus_tcp omits the data-logger serial; logger_serial defaults to None."""
    flow = SvitgridConfigFlow()
    flow.hass = hass
    with patch.object(
        SvitgridConfigFlow,
        "async_step_pair",
        new=AsyncMock(return_value={"type": "progress"}),
    ):
        await flow.async_step_harvest_config(
            {
                "protocol": "modbus_tcp",
                "ip": "192.168.1.80",
                "port": 502,
                "slave_id": 100,
                "model_id": "victron_multiplus_ii_gx_6k5",
            }
        )
    assert flow._harvest_config == {
        "protocol": "modbus_tcp",
        "ip": "192.168.1.80",
        "port": 502,
        "slave_id": 100,
        "model_id": "victron_multiplus_ii_gx_6k5",
        "logger_serial": None,
    }


@pytest.mark.asyncio
async def test_harvest_config_threads_into_finalized_inverter(hass: HomeAssistant) -> None:
    """The stored harvest_config reaches the created inverter dict in
    entry.data['inverters'] via async_step_pair_finalize (Task 11 reads it there)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from homeassistant.data_entry_flow import FlowResultType

    fake_priv = ec.generate_private_key(ec.SECP256R1())

    flow = SvitgridConfigFlow()
    flow.hass = hass
    flow._signing_key_id = "ha-sk"
    flow._private_key = fake_priv
    flow._public_key_hex = "04" + "a" * 128
    flow._harvest_config = {
        "protocol": "solarman_v5",
        "ip": "192.168.1.50",
        "port": 8899,
        "slave_id": 1,
        "model_id": "deye_sg04lp3",
        "logger_serial": "1234567890",
    }
    flow._final_payload = {
        "edgeDeviceId": "ed-h",
        "hardwareId": "ha-h",
        "apiKey": "k",
        "householdId": "h",
        "presetId": None,
        "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
        "entityMap": {"batterySoc": "sensor.soc"},
        "brand": "Deye",
        "model": "SG04LP3",
        "phases": 3,
        "hasBattery": True,
        "pvStrings": 2,
        "commands": [],
    }

    result = await flow.async_step_pair_finalize()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    invs = result["data"]["inverters"]
    assert len(invs) == 1
    assert invs[0]["harvest_config"] == flow._harvest_config
