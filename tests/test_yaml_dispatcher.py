"""Tests for YamlDispatcher — the generic write-command executor that
reads recipes from the preset's commands[] and calls HA services.

Replaces the hardcoded SmgIiExecutor for the config-entry path. The
SMG-II YAML / configuration.yaml path still uses SmgIiExecutor (no
regression for the pilot)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.executors.yaml_dispatcher import (
    UnsupportedCommandError,
    YamlDispatcher,
)


@pytest.fixture
def hass():
    h = MagicMock()
    h.services.async_call = AsyncMock(return_value=None)
    return h


# Mirror of how /finalize ships preset commands today.
DEYE_COMMANDS = [
    {
        "id": "set_battery_charge",
        "service": "modbus.write_register",
        "args": {
            "hub": "config.hub_name",
            "slave": "config.slave_id",
            "address": 152,  # TOU slot 1 charge current
            "value": "round(payload.chargePowerLimitW / config.battery_voltage / 0.1)",
        },
    },
    {
        "id": "set_work_mode",
        "service": "modbus.write_register",
        "args": {
            "hub": "config.hub_name",
            "slave": "config.slave_id",
            "address": 142,
            "value": "int(payload.workMode)",
        },
    },
]

DEYE_CONFIG = {
    "hub_name": "solarman",
    "slave_id": 1,
    "battery_voltage": 52.8,
}


@pytest.mark.asyncio
async def test_dispatches_set_battery_charge(hass):
    """Recipe-driven dispatch: compute args, call HA service."""
    d = YamlDispatcher(hass=hass, commands=DEYE_COMMANDS, config=DEYE_CONFIG)
    result = await d.dispatch("set_battery_charge", {"chargePowerLimitW": 2640})

    hass.services.async_call.assert_awaited_once_with(
        "modbus", "write_register",
        {
            "hub": "solarman",
            "slave": 1,
            "address": 152,
            "value": 500,  # 2640 / 52.8 / 0.1 = 500.0 → round 500
        },
        blocking=True,
    )
    assert result["service"] == "modbus.write_register"
    assert result["args"]["value"] == 500


@pytest.mark.asyncio
async def test_dispatches_set_work_mode(hass):
    d = YamlDispatcher(hass=hass, commands=DEYE_COMMANDS, config=DEYE_CONFIG)
    result = await d.dispatch("set_work_mode", {"workMode": 2})
    hass.services.async_call.assert_awaited_once_with(
        "modbus", "write_register",
        {"hub": "solarman", "slave": 1, "address": 142, "value": 2},
        blocking=True,
    )
    assert result["args"]["value"] == 2


@pytest.mark.asyncio
async def test_unknown_command_raises_unsupported(hass):
    d = YamlDispatcher(hass=hass, commands=DEYE_COMMANDS, config=DEYE_CONFIG)
    with pytest.raises(UnsupportedCommandError, match="set_grid_charge_toggle"):
        await d.dispatch("set_grid_charge_toggle", {"enabled": True})
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_payload_field_raises_clean_error(hass):
    d = YamlDispatcher(hass=hass, commands=DEYE_COMMANDS, config=DEYE_CONFIG)
    with pytest.raises(Exception, match="chargePowerLimitW"):
        await d.dispatch("set_battery_charge", {})  # missing payload field
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsafe_dsl_in_preset_is_rejected_at_dispatch(hass):
    """Defense-in-depth: even if a preset slipped past CI validation,
    the dispatcher refuses to call dangerous DSL at runtime."""
    malicious = [{
        "id": "set_battery_charge",
        "service": "modbus.write_register",
        "args": {"value": "__import__('os').system('rm -rf /')"},
    }]
    d = YamlDispatcher(hass=hass, commands=malicious, config={})
    with pytest.raises(Exception):  # DslEvalError or wrapped
        await d.dispatch("set_battery_charge", {})
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_string_split_into_domain_and_name(hass):
    """'modbus.write_register' → domain='modbus', service='write_register'."""
    d = YamlDispatcher(hass=hass, commands=DEYE_COMMANDS, config=DEYE_CONFIG)
    await d.dispatch("set_battery_charge", {"chargePowerLimitW": 1000})
    call = hass.services.async_call.await_args_list[0]
    assert call.args[0] == "modbus"
    assert call.args[1] == "write_register"


@pytest.mark.asyncio
async def test_dispatch_returns_descriptive_result(hass):
    """Result dict goes back to /commands/:id/ack as `result` — should
    name the service + the resolved args for postmortem visibility."""
    d = YamlDispatcher(hass=hass, commands=DEYE_COMMANDS, config=DEYE_CONFIG)
    result = await d.dispatch("set_battery_charge", {"chargePowerLimitW": 2640})
    assert result == {
        "service": "modbus.write_register",
        "args": {"hub": "solarman", "slave": 1, "address": 152, "value": 500},
    }


# Backward-compat: BaseExecutor's legacy set_battery_charge method should
# still work for the SMG-II path. Verify YamlDispatcher implements it as
# a thin wrapper around dispatch() so command_poller (which still calls
# set_battery_charge today) doesn't need an immediate refactor.

@pytest.mark.asyncio
async def test_legacy_set_battery_charge_method_routes_to_dispatch(hass):
    d = YamlDispatcher(hass=hass, commands=DEYE_COMMANDS, config=DEYE_CONFIG)
    result = await d.set_battery_charge({"chargePowerLimitW": 2640})
    hass.services.async_call.assert_awaited_once()
    assert result["args"]["value"] == 500
