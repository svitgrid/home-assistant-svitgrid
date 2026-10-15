"""WriteExecutor — compute → write → verify (SP-C Task 9).

Handles signed control commands by:
  1. Looking up the WriteCommand in the spec.
  2. Reading the prior value of every top-level field the payload sets (the
     read-modify-write base of a bit:N / clear_mask field, and the value the
     ACK reports as ``prev<Field>``), and for a slot command the whole slot
     table, which the ACK reports back as ``prevSlots``.
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
from dataclasses import replace
from typing import Any

from ..executors.base import BaseExecutor
from .register_spec import FieldWrite, RegisterSpec, SlotSpec, WriteCommand
from .transport import read_word, read_words, write_registers
from .write_compute import compute_register_writes, is_set, program_field

# Commands that run on another command's recipe. The API ends an event with a
# restore_* command whose payload has the same shape as the set_* one, and the
# edge firmware serves both names with one handler (``execute_battery_charge``,
# ``execute_work_mode`` and the rest, zigbee_poller.c). No register spec
# carries a restore recipe.
_RECIPE_ALIASES = {
    "restore_battery_charge": "set_battery_charge",
    "restore_work_mode": "set_work_mode",
    "restore_solar_sell": "set_solar_sell",
    "restore_grid_charge_toggle": "set_grid_charge_toggle",
    "restore_sell_power_cap": "set_sell_power_cap",
    "restore_gen_force": "set_gen_force",
    "restore_gen_port_mode": "set_gen_port_mode",
    "restore_energy_pattern": "set_energy_pattern",
}

# Restores that write back a value the inverter itself reported, so the set
# recipe's limit does not apply. The API restores a sell cap above the 15000 W
# set limit on purpose (dispatchSellPowerCap, #52): a Deye accepts 24000 on
# register 143, and clamping the restore cut an owner's export limit. Only the
# 16-bit register bounds it.
_UNCLAMPED_RESTORES = frozenset({"restore_sell_power_cap"})
_REGISTER_MAX = 0xFFFF

# A payload field one recipe lacks and another recipe writes, written first.
# The API's sell_to_grid floor hold sends set_solar_sell {solarSell: 1,
# workMode: 1}, and its restore restore_solar_sell {solarSell, workMode}; the
# firmware's execute_solar_sell writes the work mode, then the solar-sell bit.
# The solar-sell recipe has no workMode field, so dropping it left the inverter
# in Selling First.
_COMPANIONS = {"set_solar_sell": ("workMode", "set_work_mode")}

_LOGGER = logging.getLogger(__name__)


def _collect_prior_addresses(
    cmd: WriteCommand,
    payload: dict[str, Any],
) -> set[int]:
    """Return the top-level register addresses to read before the write.

    Every field the payload sets is read: a bit:N or clear_mask field needs
    the live word for its read-modify-write, and every field's prior value
    goes in the ACK (``_prev_field_values``), where the island scheduler and
    the API take the value a restore puts back. The edge firmware reads its
    work-mode, solar-sell and TOU-master registers before writing them for the
    same reason. A field the payload omits is not written, so it is not read
    either. Slot fields need none of their own: a slot command reads the whole
    slot table first (see ``_slot_table_addresses``).
    """
    return {f.address for f in cmd.fields if is_set(f, payload) and f.address is not None}


def _prev_key(payload_field: str) -> str:
    """``workMode`` -> ``prevWorkMode``: the firmware's ACK key for a prior value."""
    return "prev" + payload_field[:1].upper() + payload_field[1:]


def _decode_prior(f: FieldWrite, word: int) -> int | float:
    """The payload-unit value field *f* held in register word *word*.

    A bit field reports 0 or 1, as the firmware's ``prevSolarSell`` and
    ``prevGridChargeEnabled`` do; a full word reports the word in payload
    units, keeping only the clear_mask bits when the field owns a mask.
    """
    if f.encoding.startswith("bit:"):
        return (word >> int(f.encoding.split(":", 1)[1])) & 1
    if f.clear_mask is not None:
        word &= f.clear_mask
    value = word * f.value_scale
    return int(value) if float(value).is_integer() else value


def _prev_field_values(
    cmd: WriteCommand, payload: dict[str, Any], prior: dict[int, int]
) -> dict[str, int | float]:
    """``prev<Field>`` for every top-level field written, from the pre-read."""
    return {
        _prev_key(f.payload_field): _decode_prior(f, prior[f.address])
        for f in cmd.fields
        if is_set(f, payload) and f.address in prior
    }


def _slot_table_addresses(slot: SlotSpec) -> list[int]:
    """Every register of every slot: the pre-read a slot command makes.

    The edge firmware reads the whole table before it writes, and aborts when
    it cannot (``execute_battery_charge``). The table is the enable word's
    read-modify-write base and the ``prevSlots`` the ACK reports back, which
    the API hands to the window-end restore.
    """
    return sorted(
        {f.base + i * slot.stride for f in slot.fields for i in range(slot.count)}  # type: ignore[operator]
    )


def _prev_slot_fields(slot: SlotSpec, table: dict[int, int]) -> dict[str, Any]:
    """The prior-state fields of the firmware's ``set_battery_charge`` result.

    ``prevSlots`` is the table in the firmware's entry shape; the scalars
    describe slot 1, as the firmware's do. A register the recipe does not map
    reports 0, as in the firmware. ``end`` is the next slot's start unless the
    recipe has an end register (the firmware reports 0 there; the phone
    harvester reports the next start, as here).
    """

    def value(key: str, i: int) -> int:
        f = program_field(slot, key)
        if f is None:
            return 0
        return table.get(f.base + i * slot.stride, 0)  # type: ignore[operator]

    entries = []
    for i in range(slot.count):
        has_end = program_field(slot, "end") is not None
        end = value("end", i) if has_end else value("start", (i + 1) % slot.count)
        entries.append(
            {
                "start": value("start", i),
                "end": end,
                "power": value("power", i),
                "voltage": value("voltage", i),
                "soc": value("soc", i),
                "enable": value("enable", i),
            }
        )
    first = entries[0]
    return {
        "prevSlotStart": first["start"],
        "prevSlotEnd": first["end"],
        "prevPowerLimit": first["power"],
        "prevChargeVoltage": first["voltage"],
        "prevGridChargeSoc": first["soc"],
        "prevGridChargeEnable": first["enable"],
        "prevSlots": entries,
    }


def _unclamped(cmd: WriteCommand) -> WriteCommand:
    """*cmd* with each field's upper limit raised to the 16-bit register's."""
    fields = tuple(
        replace(f, limits={**f.limits, "max": _REGISTER_MAX}) if isinstance(f.limits, dict) else f
        for f in cmd.fields
    )
    return replace(cmd, fields=fields)


def _find_command(spec: RegisterSpec, command_name: str) -> WriteCommand | None:
    """Return the recipe for *command_name*, or for the command it aliases."""
    for name in (command_name, _RECIPE_ALIASES.get(command_name)):
        cmd = next((c for c in spec.writes if c.command == name), None)
        if cmd is not None:
            return cmd
    return None


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
        3. Read prior values for all bit/RMW addresses and any slot table.
        4. Compute the (unit, address, value) writes.
        5. Write them.
        6. Verify each written address reads back the expected value.
        7. Return ``{"written": [...], "verified": True}``, plus a
           ``prev<Field>`` for each top-level field written, and the
           firmware's ``prev*`` fields and ``prevSlots`` for a slot command.
        """
        # 1. Spec guard
        spec: RegisterSpec | None = self._spec_holder.spec
        if spec is None:
            raise RuntimeError("spec_not_loaded")

        # 2. Find command
        cmd = _find_command(spec, command_name)
        if cmd is None:
            raise NotImplementedError(command_name)
        if command_name in _UNCLAMPED_RESTORES:
            cmd = _unclamped(cmd)

        # A field another recipe writes goes first, as in the firmware. When
        # that recipe is missing nothing is written: half of the command would
        # leave the inverter in the mode the other half undoes.
        companion = _COMPANIONS.get(cmd.command)
        if companion is None or payload.get(companion[0]) is None:
            return await self._execute(spec, cmd, payload)
        field, recipe = companion
        first = _find_command(spec, recipe)
        if first is None:
            raise NotImplementedError(f"{command_name}: {field} needs a {recipe} recipe")
        head = await self._execute(spec, first, {field: payload[field]})
        tail = await self._execute(spec, cmd, payload)
        result = {**head, **tail}
        result["written"] = head["written"] + tail["written"]
        return result

    async def _execute(
        self, spec: RegisterSpec, cmd: WriteCommand, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Read, compute, write and verify one recipe; return its ACK result."""
        # 3. Gather the prior value of every field written, and a slot command's table
        unit_id = spec.default_slave_id
        prior: dict[int, int] = {}
        for addr in _collect_prior_addresses(cmd, payload):
            val = await read_word(self._hass, spec, self._cfg, unit_id, addr)
            if val is None:
                raise RuntimeError(f"prior_read_failed:{addr}")
            prior[addr] = val
        if cmd.slot is not None:
            wanted = _slot_table_addresses(cmd.slot)
            table = await read_words(self._hass, spec, self._cfg, unit_id, wanted)
            missing = [a for a in wanted if a not in table]
            if missing:
                raise RuntimeError(f"prior_read_failed:{missing[0]}")
            prior.update(table)

        # 4. Compute writes
        writes = compute_register_writes(cmd, payload, prior, unit_id=unit_id)

        # 5. Write
        await write_registers(self._hass, spec, self._cfg, writes)

        # 6. Verify read-back, one connection per unit
        by_unit: dict[int, list[int]] = {}
        for unit, addr, _expected in writes:
            by_unit.setdefault(unit, []).append(addr)
        read_back: dict[tuple[int, int], int] = {}
        for unit, addrs in by_unit.items():
            got = await read_words(self._hass, spec, self._cfg, unit, addrs)
            read_back.update({(unit, a): v for a, v in got.items()})
        for unit, addr, expected in writes:
            if (unit, addr) not in read_back:
                raise RuntimeError(f"verify_read_failed:{addr}")
            if read_back[(unit, addr)] != expected:
                raise RuntimeError(f"verify_failed:{addr}")

        # 7. Return result
        result: dict[str, Any] = {"written": [[u, a, v] for u, a, v in writes], "verified": True}
        result.update(_prev_field_values(cmd, payload, prior))
        if cmd.slot is not None:
            result.update(_prev_slot_fields(cmd.slot, prior))
        return result
