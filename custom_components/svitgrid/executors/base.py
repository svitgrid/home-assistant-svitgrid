"""Base class for inverter executors. Each executor translates Svitgrid
commands into HA service calls appropriate for the specific inverter
brand + integration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseExecutor(ABC):
    """Abstract executor. v0.2.0 supports one command; more methods in v0.3.0+."""

    @abstractmethod
    async def set_battery_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply a set_battery_charge command's payload to the inverter.

        In v0.2.0 we only handle the chargePowerLimitW field. Other payload
        fields (slotStart, slotEnd, chargeVoltage, gridChargeSoc,
        gridChargeEnable) are logged as unhandled but don't cause failure.

        Returns a result dict that's included in the ACK payload (e.g.,
        {"appliedPowerW": 2000, "registerValue": 417}).

        Raises on transport / write failures so the caller can surface as
        success=false, reason='executor_error: ...' in the ACK.
        """
