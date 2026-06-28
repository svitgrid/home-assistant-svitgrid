"""Tests for the SP-D finalize harvestConfig handoff + blocking reachability.

When the cloud /finalize response carries a camelCase ``harvestConfig`` block,
``async_step_pair_finalize`` snake-cases it into ``self._harvest_config``, fetches
the model's register-spec via the public API, and runs a BLOCKING reachability
check against a REAL register (spec.reads[0].address) rather than the generic
fallback register 1 that Deye inverters don't implement.

A relay pairing (no ``harvestConfig``) must skip the reachability check entirely
and create an entry with NO ``harvest_config`` key (regression guard).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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

# Minimal valid spec dict that RegisterSpec.from_dict accepts.
# Mirrors the shape of GET /api/v1/register-specs/:modelId, using a real
# battery-SOC register (address 588) as the probe target.
_MINIMAL_SPEC_DICT = {
    "modelId": "deye_sg04lp3",
    "version": 1,
    "protocol": "solarman_v5",
    "port": 8899,
    "defaultSlaveId": 1,
    "reads": [{"field": "batterySoc", "address": 588}],
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


def _mock_api_client(spec_dict: dict | None = _MINIMAL_SPEC_DICT):
    """Return a context-manager patch for SvitgridApiClient whose
    get_register_spec() coroutine returns spec_dict."""
    mock_instance = MagicMock()
    mock_instance.get_register_spec = AsyncMock(return_value=spec_dict)
    mock_cls = MagicMock(return_value=mock_instance)
    return patch(
        "custom_components.svitgrid.config_flow.SvitgridApiClient",
        new=mock_cls,
    ), mock_cls, mock_instance


@pytest.mark.asyncio
async def test_finalize_with_harvest_config_reachable_creates_entry(
    hass: HomeAssistant,
) -> None:
    """harvestConfig present + spec fetched + reachable True → entry with spec passed."""
    flow = _make_flow(hass, harvest_config=_HARVEST_CONFIG_CAMEL)
    checker = AsyncMock(return_value=True)
    api_patch, mock_cls, mock_instance = _mock_api_client()

    with api_patch, patch(
        "custom_components.svitgrid.harvest.reachability.check_inverter_reachable",
        new=checker,
    ):
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    invs = result["data"]["inverters"]
    assert len(invs) == 1
    assert invs[0]["harvest_config"] == _HARVEST_CONFIG_SNAKE

    # get_register_spec was called with the right model_id.
    mock_instance.get_register_spec.assert_awaited_once_with("deye_sg04lp3")

    # check_inverter_reachable was called exactly once, with the snake-cased
    # config AND a non-None spec (the real register spec).
    checker.assert_awaited_once()
    passed_config = checker.await_args.args[1]
    assert passed_config == _HARVEST_CONFIG_SNAKE
    passed_spec = checker.await_args.kwargs.get("spec")
    assert passed_spec is not None, "spec must be passed so a real register is probed"
    assert passed_spec.reads[0].address == 588  # battery SOC — not the fallback reg 1


@pytest.mark.asyncio
async def test_finalize_with_harvest_config_unreachable_shows_error(
    hass: HomeAssistant,
) -> None:
    """harvestConfig present + reachable False → form error, NO entry created."""
    flow = _make_flow(hass, harvest_config=_HARVEST_CONFIG_CAMEL)
    checker = AsyncMock(return_value=False)
    api_patch, _, _ = _mock_api_client()

    with api_patch, patch(
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
    api_patch, _, mock_instance = _mock_api_client()

    with api_patch, patch(
        "custom_components.svitgrid.harvest.reachability.check_inverter_reachable",
        new=checker,
    ):
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    invs = result["data"]["inverters"]
    assert len(invs) == 1
    assert "harvest_config" not in invs[0]
    checker.assert_not_awaited()
    # No spec fetch for relay pairings.
    mock_instance.get_register_spec.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_spec_fetch_returns_none_falls_back_to_no_spec(
    hass: HomeAssistant,
) -> None:
    """get_register_spec returns None → spec=None passed; entry still created."""
    flow = _make_flow(hass, harvest_config=_HARVEST_CONFIG_CAMEL)
    checker = AsyncMock(return_value=True)
    api_patch, _, mock_instance = _mock_api_client(spec_dict=None)

    with api_patch, patch(
        "custom_components.svitgrid.harvest.reachability.check_inverter_reachable",
        new=checker,
    ):
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    mock_instance.get_register_spec.assert_awaited_once_with("deye_sg04lp3")
    # Fallback: spec=None means the generic probe register is used.
    passed_spec = checker.await_args.kwargs.get("spec")
    assert passed_spec is None


@pytest.mark.asyncio
async def test_finalize_spec_fetch_raises_falls_back_to_no_spec(
    hass: HomeAssistant,
) -> None:
    """get_register_spec raises → spec=None; no regression, entry still created."""
    flow = _make_flow(hass, harvest_config=_HARVEST_CONFIG_CAMEL)
    checker = AsyncMock(return_value=True)

    mock_instance = MagicMock()
    mock_instance.get_register_spec = AsyncMock(side_effect=Exception("network error"))
    mock_cls = MagicMock(return_value=mock_instance)

    with patch("custom_components.svitgrid.config_flow.SvitgridApiClient", new=mock_cls), \
         patch("custom_components.svitgrid.harvest.reachability.check_inverter_reachable", new=checker):
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # Even though spec fetch raised, the reachability check still ran.
    checker.assert_awaited_once()
    passed_spec = checker.await_args.kwargs.get("spec")
    assert passed_spec is None  # graceful fallback
