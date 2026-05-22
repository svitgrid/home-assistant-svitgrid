"""Generic preset-driven write-command executor.

Replaces the hardcoded SmgIiExecutor for the config-entry path. Reads
the preset's `commands[]` (delivered by /finalize when the user pairs
HA against a brand preset) and dispatches each incoming command by id.

YAML preset shape (illustrative):

    commands:
      - id: set_battery_charge
        service: modbus.write_register
        args:
          hub: "config.hub_name"
          slave: "config.slave_id"
          address: 152
          value: "round(payload.chargePowerLimitW / config.battery_voltage / 0.1)"

Every string under `args` is run through the DSL evaluator
(custom_components.svitgrid.dsl). Non-string values pass through as-is.

Why one class, not one-per-brand: every brand differs only in its
register addresses + scaling — both expressed in YAML. The execution
mechanics (compute args → call HA service) are identical. Anything more
exotic (multi-register writes, conditional logic) is out of scope here;
we add it when a real brand needs it.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from ..dsl import DslEvalError, evaluate
from .base import BaseExecutor

_LOGGER = logging.getLogger(__name__)


class UnsupportedCommandError(Exception):
    """Raised when the preset doesn't have a recipe for the requested command.
    Caller (command_poller) ACKs as rejected='unsupported'."""


class YamlDispatcher(BaseExecutor):
    def __init__(
        self,
        *,
        hass: HomeAssistant,
        commands: list[dict[str, Any]],
        config: dict[str, Any],
    ) -> None:
        self._hass = hass
        # Index by command id for O(1) lookup. Skip malformed entries — CI
        # validation should catch these before they ship, but defense in
        # depth means we don't crash at dispatch time.
        self._by_id: dict[str, dict[str, Any]] = {}
        for cmd in commands:
            cmd_id = cmd.get("id")
            if isinstance(cmd_id, str):
                self._by_id[cmd_id] = cmd
            else:
                _LOGGER.warning("Preset command missing id; skipping: %s", cmd)
        self._config = dict(config)

    async def dispatch(
        self, command_name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Look up the command recipe, evaluate args, call the HA service.

        Returns a result dict the caller includes in the signed ACK so the
        server (and user, via the activity sensor) can see what landed.

        Raises UnsupportedCommandError for unknown command_names; raises
        DslEvalError (or others) for evaluation / service-call failures."""
        recipe = self._by_id.get(command_name)
        if recipe is None:
            raise UnsupportedCommandError(
                f"Preset has no recipe for command {command_name!r}"
            )

        service = recipe.get("service")
        if not isinstance(service, str) or "." not in service:
            raise ValueError(
                f"Command {command_name!r} has malformed service {service!r}"
            )
        domain, service_name = service.split(".", 1)

        raw_args = recipe.get("args") or {}
        resolved: dict[str, Any] = {}
        for key, expression in raw_args.items():
            try:
                resolved[key] = evaluate(
                    expression, payload=payload, config=self._config,
                )
            except DslEvalError:
                _LOGGER.warning(
                    "DSL evaluation failed for %s.%s: %r", command_name, key, expression,
                )
                raise

        _LOGGER.info(
            "YamlDispatcher: %s → %s with %s", command_name, service, resolved,
        )
        await self._hass.services.async_call(
            domain, service_name, resolved, blocking=True,
        )
        return {"service": service, "args": resolved}

    # Legacy compatibility: command_poller currently calls
    # executor.set_battery_charge(payload). Route that to dispatch so old
    # callers continue to work unchanged.
    async def set_battery_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.dispatch("set_battery_charge", payload)
