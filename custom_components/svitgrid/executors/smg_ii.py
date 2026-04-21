"""SMG-II executor — writes to Modbus register 233 (inverter-side charging
current, scale 0.1 A) via HA's modbus.write_register service.

Register map reference: syssi/esphome-smg-ii. Covers EASUN SMG-II,
ISolar SMG-II, POW-HVM, Anenji 4kW/6kW (pilot inverter)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .base import BaseExecutor

_LOGGER = logging.getLogger(__name__)

# Writable register: inverter-side charging current limit, 0.1 A units
_REGISTER_CHARGE_CURRENT = 233
_CURRENT_REGISTER_SCALE = 0.1  # raw value 417 = 41.7 A

# Payload fields we handle today vs those that'd need additional registers.
_HANDLED_FIELDS = frozenset({"chargePowerLimitW"})
_UNHANDLED_FIELDS = frozenset(
    {
        "slotStart",
        "slotEnd",
        "chargeVoltage",
        "gridChargeSoc",
        "gridChargeEnable",
    }
)


class SmgIiExecutor(BaseExecutor):
    def __init__(
        self,
        *,
        hass: HomeAssistant,
        hub: str,
        slave: int,
        battery_nominal_voltage: float,
    ) -> None:
        if battery_nominal_voltage <= 0:
            raise ValueError(
                f"battery_nominal_voltage must be positive, got {battery_nominal_voltage}"
            )
        self._hass = hass
        self._hub = hub
        self._slave = int(slave)
        self._battery_voltage = float(battery_nominal_voltage)

    async def set_battery_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "chargePowerLimitW" not in payload:
            raise ValueError("set_battery_charge payload missing required chargePowerLimitW field")
        power_w = float(payload["chargePowerLimitW"])
        current_amps = power_w / self._battery_voltage
        register_value = round(current_amps / _CURRENT_REGISTER_SCALE)

        # Warn on payload fields we don't yet translate — still execute the
        # power-limit write so the pilot's slider works; B2.3+ adds handlers
        # for the others.
        unhandled = [k for k in payload if k not in _HANDLED_FIELDS and k in _UNHANDLED_FIELDS]
        if unhandled:
            _LOGGER.warning(
                "SMG-II executor: unhandled payload fields %s — applied "
                "chargePowerLimitW only (other fields ignored)",
                unhandled,
            )

        await self._hass.services.async_call(
            "modbus",
            "write_register",
            {
                "hub": self._hub,
                "slave": self._slave,
                "address": _REGISTER_CHARGE_CURRENT,
                "value": register_value,
            },
            blocking=True,
        )

        return {"appliedPowerW": int(power_w), "registerValue": register_value}
