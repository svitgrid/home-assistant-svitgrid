"""Unit tests for the SMG-II executor. Mocks hass.services.async_call —
no real Modbus traffic. Hardware validation happens in Tier 3 pilot."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.svitgrid.executors.smg_ii import SmgIiExecutor


def _mock_hass():
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    return hass


@pytest.mark.asyncio
class TestSmgIiSetBatteryCharge:
    async def test_writes_register_233_with_converted_current(self):
        """2000W at 48V = 41.67A = reg value 417 (scale 0.1 A)."""
        hass = _mock_hass()
        executor = SmgIiExecutor(hass=hass, hub="my_hub", slave=1, battery_nominal_voltage=48.0)

        result = await executor.set_battery_charge({"chargePowerLimitW": 2000})

        hass.services.async_call.assert_called_once()
        args = hass.services.async_call.call_args
        assert args.args[0] == "modbus"
        assert args.args[1] == "write_register"
        service_data = args.args[2]
        assert service_data["hub"] == "my_hub"
        assert service_data["slave"] == 1
        assert service_data["address"] == 233
        assert service_data["value"] == 417
        assert args.kwargs.get("blocking") is True

        assert result == {"appliedPowerW": 2000, "registerValue": 417}

    async def test_conversion_math_for_common_values(self):
        """Pin the power→current conversion for realistic SMG-II scenarios."""
        hass = _mock_hass()
        executor = SmgIiExecutor(hass=hass, hub="h", slave=1, battery_nominal_voltage=48.0)

        # 4000W @ 48V = 83.33A → reg 833
        result = await executor.set_battery_charge({"chargePowerLimitW": 4000})
        assert result["registerValue"] == 833

        # 1000W @ 48V = 20.83A → reg 208
        result = await executor.set_battery_charge({"chargePowerLimitW": 1000})
        assert result["registerValue"] == 208

    async def test_uses_configured_slave_id(self):
        hass = _mock_hass()
        executor = SmgIiExecutor(hass=hass, hub="h", slave=7, battery_nominal_voltage=48.0)
        await executor.set_battery_charge({"chargePowerLimitW": 1000})
        service_data = hass.services.async_call.call_args.args[2]
        assert service_data["slave"] == 7

    async def test_uses_configured_battery_voltage(self):
        """51.2V (LiFePO4 nominal) — 2000W → 39.06A → reg 391."""
        hass = _mock_hass()
        executor = SmgIiExecutor(hass=hass, hub="h", slave=1, battery_nominal_voltage=51.2)
        result = await executor.set_battery_charge({"chargePowerLimitW": 2000})
        assert result["registerValue"] == 391

    async def test_missing_chargePowerLimitW_raises_value_error(self):
        hass = _mock_hass()
        executor = SmgIiExecutor(hass=hass, hub="h", slave=1, battery_nominal_voltage=48.0)
        with pytest.raises(ValueError, match="chargePowerLimitW"):
            await executor.set_battery_charge({"slotStart": 0, "slotEnd": 240})

    async def test_non_positive_battery_voltage_raises_at_construct(self):
        hass = _mock_hass()
        with pytest.raises(ValueError, match="battery_nominal_voltage"):
            SmgIiExecutor(hass=hass, hub="h", slave=1, battery_nominal_voltage=0)
        with pytest.raises(ValueError, match="battery_nominal_voltage"):
            SmgIiExecutor(hass=hass, hub="h", slave=1, battery_nominal_voltage=-5)

    async def test_modbus_call_failure_propagates(self):
        hass = _mock_hass()
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("Modbus hub not found"))
        executor = SmgIiExecutor(hass=hass, hub="h", slave=1, battery_nominal_voltage=48.0)
        with pytest.raises(RuntimeError, match="Modbus hub not found"):
            await executor.set_battery_charge({"chargePowerLimitW": 2000})

    async def test_logs_unhandled_payload_fields(self, caplog):
        import logging

        caplog.set_level(logging.WARNING)
        hass = _mock_hass()
        executor = SmgIiExecutor(hass=hass, hub="h", slave=1, battery_nominal_voltage=48.0)
        await executor.set_battery_charge(
            {
                "chargePowerLimitW": 2000,
                "slotStart": 600,
                "slotEnd": 960,
                "chargeVoltage": 55.2,
            }
        )
        assert "unhandled" in caplog.text.lower() or "ignored" in caplog.text.lower()
