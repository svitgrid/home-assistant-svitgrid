"""Tests for the SP-D finalize harvestConfig handoff + blocking reachability.

When the cloud /finalize response carries a camelCase ``harvestConfig`` block,
``async_step_pair_finalize`` snake-cases it into ``self._harvest_config``, runs a
BLOCKING reachability check, and only then creates the entry (threading the spec
into the inverter dict so the dormant SP-B reads + SP-C writes activate).

A relay pairing (no ``harvestConfig``) must skip the reachability check entirely
and create an entry with NO ``harvest_config`` key (regression guard).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.svitgrid.config_flow import SvitgridConfigFlow

_HARVEST_CONFIG_CAMEL = {
    "protocol": "solarman_v5",
    "ip": "192.168.1.50",
    "port": 8899,
    "slaveId": 1,
    "modelId": "deye_sg04lp3",
    "loggerSerial": "1234567890",
}
_HARVEST_CONFIG_SNAKE = {
    "protocol": "solarman_v5",
    "ip": "192.168.1.50",
    "port": 8899,
    "slave_id": 1,
    "model_id": "deye_sg04lp3",
    "logger_serial": "1234567890",
}


def _make_flow(hass: HomeAssistant, *, harvest_config: dict | None) -> SvitgridConfigFlow:
    flow = SvitgridConfigFlow()
    flow.hass = hass
    flow._signing_key_id = "ha-sk"
    flow._private_key = ec.generate_private_key(ec.SECP256R1())
    flow._public_key_hex = "04" + "a" * 128
    payload: dict = {
        "edgeDeviceId": "ed-h", "hardwareId": "ha-h",
        "apiKey": "k", "householdId": "h", "presetId": None,
        "trustedKeys": [{"keyId": "ha-home-01", "publicKeyHex": "04" + "a" * 128}],
        "entityMap": {"batterySoc": "sensor.soc"},
        "brand": "Deye", "model": "SG04LP3", "phases": 3,
        "hasBattery": True, "pvStrings": 2, "commands": [],
    }
    if harvest_config is not None:
        payload["harvestConfig"] = harvest_config
    flow._final_payload = payload
    return flow


@pytest.mark.asyncio
async def test_finalize_with_harvest_config_reachable_creates_entry(
    hass: HomeAssistant,
) -> None:
    """harvestConfig present + reachable True → entry; snake-cased harvest_config."""
    flow = _make_flow(hass, harvest_config=_HARVEST_CONFIG_CAMEL)
    checker = AsyncMock(return_value=True)
    with patch(
        "custom_components.svitgrid.harvest.reachability.check_inverter_reachable",
        new=checker,
    ):
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    invs = result["data"]["inverters"]
    assert len(invs) == 1
    assert invs[0]["harvest_config"] == _HARVEST_CONFIG_SNAKE
    # The blocking check ran exactly once, against the snake-cased config.
    checker.assert_awaited_once()
    passed_config = checker.await_args.args[1]
    assert passed_config == _HARVEST_CONFIG_SNAKE


@pytest.mark.asyncio
async def test_finalize_with_harvest_config_unreachable_shows_error(
    hass: HomeAssistant,
) -> None:
    """harvestConfig present + reachable False → form error, NO entry created."""
    flow = _make_flow(hass, harvest_config=_HARVEST_CONFIG_CAMEL)
    checker = AsyncMock(return_value=False)
    with patch(
        "custom_components.svitgrid.harvest.reachability.check_inverter_reachable",
        new=checker,
    ):
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "pair_finalize"
    assert result["errors"] == {"base": "cannot_reach_inverter"}
    checker.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_relay_skips_reachability_and_has_no_harvest_config(
    hass: HomeAssistant,
) -> None:
    """No harvestConfig (relay) → reachability NOT called; no harvest_config key."""
    flow = _make_flow(hass, harvest_config=None)
    checker = AsyncMock(return_value=True)
    with patch(
        "custom_components.svitgrid.harvest.reachability.check_inverter_reachable",
        new=checker,
    ):
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    invs = result["data"]["inverters"]
    assert len(invs) == 1
    assert "harvest_config" not in invs[0]
    checker.assert_not_awaited()
