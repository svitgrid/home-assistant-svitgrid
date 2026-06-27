"""Tests for harvest.reachability — SP-D Task 3.

TDD RED-first: these tests are written before the implementation exists.

Coverage:
  1. True  when transport.read_word returns a non-None int.
  2. False when transport.read_word returns None (timeout / no data).
  3. False when transport.read_word raises any exception (never propagates).
  4. transport.read_word is called with cfg derived from harvest_config
     (ip, port) and unit_id == harvest_config["slave_id"].
  5. When a RegisterSpec is supplied, probe address = spec.reads[0].address.
  6. When spec=None, probe address falls back to the module constant _PROBE_ADDRESS.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from custom_components.svitgrid.harvest.reachability import (
    _PROBE_ADDRESS,
    check_inverter_reachable,
)
from custom_components.svitgrid.harvest.register_spec import RegisterSpec

MODULE = "custom_components.svitgrid.harvest.reachability"

_HARVEST_CFG = {
    "protocol": "solarman_v5",
    "ip": "192.168.1.100",
    "port": 8899,
    "slave_id": 1,
    "model_id": "deye_sg04lp3",
    "logger_serial": "1234567890",
}

_HASS = object()  # transport is fully patched; hass is not exercised directly


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------


async def test_returns_true_when_read_word_returns_int():
    """True when transport returns a register value."""
    mock_rw = AsyncMock(return_value=42)
    with patch(f"{MODULE}.transport.read_word", mock_rw):
        result = await check_inverter_reachable(_HASS, _HARVEST_CFG)
    assert result is True


async def test_returns_false_when_read_word_returns_none():
    """False when transport returns None (timeout / missing register)."""
    mock_rw = AsyncMock(return_value=None)
    with patch(f"{MODULE}.transport.read_word", mock_rw):
        result = await check_inverter_reachable(_HASS, _HARVEST_CFG)
    assert result is False


async def test_returns_false_when_read_word_raises():
    """False when transport raises — exception must never propagate."""
    mock_rw = AsyncMock(side_effect=ConnectionError("connection refused"))
    with patch(f"{MODULE}.transport.read_word", mock_rw):
        result = await check_inverter_reachable(_HASS, _HARVEST_CFG)
    assert result is False


# ---------------------------------------------------------------------------
# Transport call shape
# ---------------------------------------------------------------------------


async def test_cfg_contains_ip_port_from_harvest_config():
    """transport.read_word is called with cfg carrying ip and port from harvest_config."""
    mock_rw = AsyncMock(return_value=99)
    with patch(f"{MODULE}.transport.read_word", mock_rw):
        await check_inverter_reachable(_HASS, _HARVEST_CFG)

    # Signature: read_word(hass, spec, cfg, unit_id, address)
    _hass_arg, _spec_arg, cfg_arg, unit_id_arg, _addr_arg = mock_rw.call_args.args
    assert cfg_arg["ip"] == _HARVEST_CFG["ip"]
    assert cfg_arg["port"] == _HARVEST_CFG["port"]
    assert unit_id_arg == _HARVEST_CFG["slave_id"]


# ---------------------------------------------------------------------------
# Address selection
# ---------------------------------------------------------------------------


async def test_uses_spec_first_read_address_when_spec_provided():
    """When a spec with reads is given, probe address = spec.reads[0].address."""
    spec = RegisterSpec.from_dict({
        "modelId": "deye_sg04lp3",
        "version": 1,
        "protocol": "solarman_v5",
        "port": 8899,
        "defaultSlaveId": 1,
        "flags": {},
        "reads": [{"field": "batterySoc", "address": 588}],
        "derivations": [],
        "writes": [],
    })
    mock_rw = AsyncMock(return_value=80)
    with patch(f"{MODULE}.transport.read_word", mock_rw):
        result = await check_inverter_reachable(_HASS, _HARVEST_CFG, spec=spec)

    assert result is True
    _hass_arg, _spec_arg, _cfg_arg, _unit_id_arg, address_arg = mock_rw.call_args.args
    assert address_arg == 588


async def test_uses_probe_address_when_no_spec():
    """When spec=None, the module-level _PROBE_ADDRESS constant is used."""
    mock_rw = AsyncMock(return_value=0)
    with patch(f"{MODULE}.transport.read_word", mock_rw):
        await check_inverter_reachable(_HASS, _HARVEST_CFG, spec=None)

    _hass_arg, _spec_arg, _cfg_arg, _unit_id_arg, address_arg = mock_rw.call_args.args
    assert address_arg == _PROBE_ADDRESS
