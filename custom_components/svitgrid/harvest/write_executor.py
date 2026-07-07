"""WriteExecutor — compute → write → verify (SP-C Task 9).

Handles signed control commands by:
  1. Looking up the WriteCommand in the spec.
  2. Reading prior register values for bit:N / clear_mask fields (RMW).
  3. Computing the exact (unit, address, value) writes via compute_register_writes.
  4. Writing them via write_registers.
  5. Reading each written address back and verifying the value matches.
  6. Returning a result dict for the ACK payload.

Plugs into the signed-command path: the command poller calls
``executor.dispatch(command_name, payload)`` — signature is already verified
upstream.
"""

from __future__ import annotations

import logging
from typing import Any

from ..executors.base import BaseExecutor
from .register_spec import FieldWrite, RegisterSpec, WriteCommand
from .transport import read_word, write_registers
from .write_compute import compute_register_writes

_LOGGER = logging.getLogger(__name__)


def _needs_prior(f: FieldWrite) -> bool:
    """Return True when this FieldWrite requires a prior register read.

    A prior read is needed when:
    - The encoding is a bit-field (``bit:N``) — we need the current word for RMW.
    - The field has ``clear_mask`` set — same reason, regardless of encoding.
    """
    return f.encoding.startswith("bit:") or f.clear_mask is not None


def _collect_prior_addresses(
    cmd: WriteCommand,
    payload: dict[str, Any],
) -> set[int]:
    """Return the set of register addresses that need a prior read.

    For top-level fields the address is stored directly in ``f.address``.
    For slot fields the address is ``f.base + slot_idx * stride``, where
    ``slot_idx`` is read from the payload (or the via_next_slot variant).
    """
    addrs: set[int] = set()

    for f in cmd.fields:
        if _needs_prior(f) and f.address is not None:
            addrs.add(f.address)

    if cmd.slot is not None:
        s = cmd.slot
        idx = int(payload[s.index_field])
        # Range-check the slot index BEFORE deriving an address, mirroring
        # compute_register_writes — otherwise a bad slotIndex would read a
        # bogus prior address before the later ValueError fires.
        if not (0 <= idx < s.count):
            raise ValueError(f"slot index {idx} out of range 0..{s.count - 1}")
        for f in s.fields:
            if _needs_prior(f):
                slot_idx = ((idx + 1) % s.count) if f.via_next_slot else idx
                addr = f.base + slot_idx * s.stride  # type: ignore[operator]
                addrs.add(addr)

    return addrs


class WriteExecutor(BaseExecutor):
    """Executor that turns Svitgrid control commands into register writes.

    Workflow per dispatch call:
      spec lookup → prior reads (bit/RMW fields) → compute writes →
      write_registers → verify read-back → ACK result.
    """

    def __init__(self, hass: Any, spec_holder: Any, cfg: dict[str, Any]) -> None:
        self._hass = hass
        self._spec_holder = spec_holder
        self._cfg = cfg

    # ------------------------------------------------------------------
    # Legacy abstract method — routes to dispatch so BaseExecutor is happy
    # ------------------------------------------------------------------

    async def set_battery_charge(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Legacy entry point; delegates to dispatch."""
        return await self.dispatch("set_battery_charge", payload)

    # ------------------------------------------------------------------
    # Generic dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, command_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Execute *command_name* against the inverter and return the ACK result.

        Steps
        -----
        1. Validate spec is loaded.
        2. Find the WriteCommand by name (→ NotImplementedError if absent).
        3. Read prior values for all bit/RMW addresses.
        4. Compute the (unit, address, value) writes.
        5. Write them.
        6. Verify each written address reads back the expected value.
        7. Return ``{"written": [...], "verified": True}``.
        """
        # 1. Spec guard
        spec: RegisterSpec | None = self._spec_holder.spec
        if spec is None:
            raise RuntimeError("spec_not_loaded")

        # 2. Find command
        cmd: WriteCommand | None = next((c for c in spec.writes if c.command == command_name), None)
        if cmd is None:
            raise NotImplementedError(command_name)

        # 3. Gather prior values for bit/RMW fields
        prior_addrs = _collect_prior_addresses(cmd, payload)
        prior: dict[int, int] = {}
        for addr in prior_addrs:
            val = await read_word(self._hass, spec, self._cfg, spec.default_slave_id, addr)
            if val is None:
                raise RuntimeError(f"prior_read_failed:{addr}")
            prior[addr] = val

        # 4. Compute writes
        writes = compute_register_writes(cmd, payload, prior, unit_id=spec.default_slave_id)

        # 5. Write
        await write_registers(self._hass, spec, self._cfg, writes)

        # 6. Verify read-back
        for unit, addr, expected in writes:
            read_back = await read_word(self._hass, spec, self._cfg, unit, addr)
            if read_back is None:
                raise RuntimeError(f"verify_read_failed:{addr}")
            if read_back != expected:
                raise RuntimeError(f"verify_failed:{addr}")

        # 7. Return result
        return {"written": [[u, a, v] for u, a, v in writes], "verified": True}
