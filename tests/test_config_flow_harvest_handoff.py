"""Tests for the SP-D finalize harvestConfig handoff.

When the cloud /finalize response carries a camelCase ``harvestConfig`` block,
``async_step_pair_finalize`` snake-cases it into ``self._harvest_config`` and
creates the entry.

It does NOT probe the inverter first (issue #6). By the time /finalize has
answered, the cloud has built the station and the pairing code is spent, so a
failed probe that re-showed the form left a station in the cloud and nothing in
Home Assistant. An unreachable inverter is reported by the harvest loop instead.

A relay pairing (no ``harvestConfig``) creates an entry with NO
``harvest_config`` key (regression guard).
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
    return (
        patch(
            "custom_components.svitgrid.config_flow.SvitgridApiClient",
            new=mock_cls,
        ),
        mock_cls,
        mock_instance,
    )


def _no_probe_patches():
    """Patch every way pairing could touch the inverter, so a test can assert
    that none of them ran."""
    checker = AsyncMock(return_value=False)
    read_word = AsyncMock(return_value=None)
    return (
        checker,
        read_word,
        patch(
            "custom_components.svitgrid.harvest.reachability.check_inverter_reachable",
            new=checker,
        ),
        patch("custom_components.svitgrid.harvest.transport.read_word", new=read_word),
    )


@pytest.mark.asyncio
async def test_finalize_with_harvest_config_creates_entry_without_probing(
    hass: HomeAssistant,
) -> None:
    """harvestConfig present → entry created, and the inverter is not probed."""
    flow = _make_flow(hass, harvest_config=_HARVEST_CONFIG_CAMEL)
    api_patch, _, _ = _mock_api_client()
    checker, read_word, checker_patch, read_word_patch = _no_probe_patches()

    with api_patch, checker_patch, read_word_patch:
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    invs = result["data"]["inverters"]
    assert len(invs) == 1
    assert invs[0]["harvest_config"] == _HARVEST_CONFIG_SNAKE
    checker.assert_not_awaited()
    read_word.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_with_flat_and_array_harvest_config_stores_snake_case(
    hass: HomeAssistant,
) -> None:
    """The shape prod `/finalize` returns: a flat `harvestConfig` AND the same
    config inside `inverters[0]`. The array copy used to be stored verbatim in
    camelCase and shadow the snake-cased flat copy, so setup read no `model_id`
    and the harvest loop idled forever (issue #5)."""
    flow = _make_flow(hass, harvest_config=_HARVEST_CONFIG_CAMEL)
    flow._final_payload["inverters"] = [
        {
            "inverterId": "ha-h",
            "presetId": None,
            "entityMap": {},
            "brand": "Deye",
            "model": "SG04LP3",
            "phases": 3,
            "hasBattery": True,
            "pvStrings": 2,
            "commands": [],
            "harvestConfig": dict(_HARVEST_CONFIG_CAMEL),
        }
    ]
    api_patch, _, _ = _mock_api_client()

    with api_patch:
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["inverters"][0]["harvest_config"] == _HARVEST_CONFIG_SNAKE


@pytest.mark.asyncio
async def test_finalize_with_an_unreachable_logger_still_creates_entry(
    hass: HomeAssistant,
) -> None:
    """The cloud has already built the station: an inverter that does not answer
    must not stop the entry from being created (issue #6)."""
    flow = _make_flow(hass, harvest_config=_HARVEST_CONFIG_CAMEL)
    api_patch, _, _ = _mock_api_client()
    checker, read_word, checker_patch, read_word_patch = _no_probe_patches()
    # Every read fails, as it would against a logger that is off the network.
    read_word.side_effect = ConnectionError("no route to host")

    with api_patch, checker_patch, read_word_patch:
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result.get("errors") is None
    assert result["data"]["inverters"][0]["harvest_config"] == _HARVEST_CONFIG_SNAKE
    assert result["data"]["api_key"] == "k"


@pytest.mark.asyncio
async def test_finalize_relay_has_no_harvest_config(
    hass: HomeAssistant,
) -> None:
    """No harvestConfig (relay) → no probe, no spec fetch, no harvest_config key."""
    flow = _make_flow(hass, harvest_config=None)
    api_patch, _, mock_instance = _mock_api_client()
    checker, _, checker_patch, read_word_patch = _no_probe_patches()

    with api_patch, checker_patch, read_word_patch:
        result = await flow.async_step_pair_finalize()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    invs = result["data"]["inverters"]
    assert len(invs) == 1
    assert "harvest_config" not in invs[0]
    checker.assert_not_awaited()
    mock_instance.get_register_spec.assert_not_awaited()
