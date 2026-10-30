"""Executor for an inverter read by an adopted edge box.

The integration has no write path of its own to such an inverter: the box
does. A dispatch becomes a command in the edge command queue, the box drains
it on its next poll (`GET /api/v3/executors/commands`, `edge_api.py`) and acks
it, and the executor answers with that ack. The phone's signature travels
untouched: the box checks it, and the integration holds no key the box trusts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import BaseExecutor

if TYPE_CHECKING:
    from ..edge_command_queue import EdgeCommandQueue

# The commands the box refuses without a phone signature
# (`command_requires_signature`, firmware `command_auth.c`), vendored from the
# monorepo's `packages/inverter_protocol/fixtures/firmware_signed_commands.json`.
EDGE_SIGNED_COMMANDS = frozenset(
    {
        "apply_priority_profile",
        "clear_island_endpoint",
        "factory_reset",
        "permit_join",
        "restore_battery_charge",
        "restore_energy_pattern",
        "restore_gen_force",
        "restore_grid_charge_current",
        "restore_grid_charge_master",
        "restore_grid_charge_toggle",
        "restore_must_thresholds",
        "restore_peak_shaving",
        "restore_pi30_thresholds",
        "restore_priority_profile",
        "restore_sell_power_cap",
        "restore_solar_sell",
        "restore_work_mode",
        "set_battery_charge",
        "set_energy_pattern",
        "set_gen_force",
        "set_grid_charge_current",
        "set_grid_charge_master",
        "set_grid_charge_toggle",
        "set_inverter_setting",
        "set_island_endpoint",
        "set_must_thresholds",
        "set_peak_shaving",
        "set_pi30_thresholds",
        "set_sell_power_cap",
        "set_solar_sell",
        "set_state",
        "set_work_mode",
    }
)


class EdgeBoxExecutor(BaseExecutor):
    """Turns dispatches for one inverter into queued commands for its box."""

    relays_signature = True

    def __init__(
        self,
        queue: EdgeCommandQueue,
        device_id: str,
        inverter_id: str,
        *,
        ack_timeout_s: float = 90,
    ) -> None:
        self._queue = queue
        self._device_id = device_id
        self._inverter_id = inverter_id
        self._ack_timeout_s = ack_timeout_s

    async def set_battery_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        # The legacy entry point carries no signature, so the box would refuse it.
        return await self.dispatch("set_battery_charge", payload)

    async def dispatch(
        self,
        command_name: str,
        payload: dict[str, Any],
        *,
        signing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queue the command and wait for the box's ack.

        Returns the ack's `result`. Raises `PermissionError("unsigned_write")`
        for a signed-only command without a signature (the box would refuse it,
        so it is never queued), `NotImplementedError(reason)` when the box
        rejects it as unsupported, `RuntimeError(reason)` on any other failure
        and `TimeoutError("edge_ack_timeout")` when no ack comes in time.
        """
        if signing is None and command_name in EDGE_SIGNED_COMMANDS:
            raise PermissionError("unsigned_write")
        command_id = await self._queue.async_enqueue(
            self._device_id,
            command_name,
            {**payload, "inverterId": self._inverter_id},
            signing=signing,
            inverter_id=self._inverter_id,
            source="executor",
        )
        row = await self._queue.async_wait(command_id, self._ack_timeout_s)
        if row is None:
            # The box was forgotten while the command waited.
            raise RuntimeError("edge_box_forgotten")
        if row["state"] == "success":
            return row["result"] or {}
        if row["state"] == "failed":
            reason = row["reason"] or "edge_command_failed"
            if row["rejected"] and reason.startswith("unsupported"):
                raise NotImplementedError(reason)
            raise RuntimeError(reason)
        raise TimeoutError("edge_ack_timeout")
