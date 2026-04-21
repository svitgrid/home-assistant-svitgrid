"""Factory for constructing executors from YAML config."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .base import BaseExecutor

_LOGGER = logging.getLogger(__name__)


def create_executor(config: dict[str, Any], hass: HomeAssistant) -> BaseExecutor | None:
    """Build an executor instance from the svitgrid.executor YAML block.

    Returns None for `type: "read_only"` or absent config.
    Raises ValueError for unknown types.
    """
    exec_type = config.get("type", "read_only")

    if exec_type == "read_only":
        _LOGGER.info("Executor: read_only (commands will be rejected as unsupported)")
        return None

    if exec_type == "smg_ii":
        from .smg_ii import SmgIiExecutor

        hub = config["modbus_hub"]
        slave = config.get("modbus_slave", 1)
        battery_voltage = config.get("battery_nominal_voltage", 48.0)
        _LOGGER.info(
            "Executor: smg_ii (hub=%s, slave=%d, battery=%gV)",
            hub,
            slave,
            battery_voltage,
        )
        return SmgIiExecutor(
            hass=hass,
            hub=hub,
            slave=slave,
            battery_nominal_voltage=battery_voltage,
        )

    raise ValueError(f"Unknown executor type: {exec_type!r}")
