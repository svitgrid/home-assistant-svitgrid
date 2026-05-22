"""Base class for inverter executors. Each executor translates Svitgrid
commands into HA service calls appropriate for the specific inverter
brand + integration."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseExecutor(ABC):
    """Abstract executor. set_battery_charge is the legacy v0.2.0 entry point;
    `dispatch` is the v0.3.0+ generic entry point used by command_poller and
    YamlDispatcher. Default `dispatch` routes set_battery_charge to the
    legacy method so subclasses (like SmgIiExecutor) that only implement
    set_battery_charge keep working unchanged. Subclasses with a true
    generic dispatcher (YamlDispatcher) override `dispatch`."""

    @abstractmethod
    async def set_battery_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Legacy method. Apply a set_battery_charge command's payload to
        the inverter. Returns a result dict that's included in the ACK
        payload. Raises on transport / write failures so the caller can
        surface as success=false, reason='executor_error: ...' in the ACK."""

    async def dispatch(
        self, command_name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Generic dispatch entry point. Default impl supports only
        set_battery_charge (routes to the legacy method). Subclasses
        override for full multi-command support."""
        if command_name == "set_battery_charge":
            return await self.set_battery_charge(payload)
        raise NotImplementedError(
            f"Executor does not support command {command_name!r}"
        )
