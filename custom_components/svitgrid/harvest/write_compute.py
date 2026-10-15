"""Pure Python port of the Dart reference write-applier (SP-C Task 6).

Turns a ``WriteCommand`` + command payload + the inverter's prior register
values into the exact list of ``(unit_id, address, value)`` writes.

No I/O.  Raise ``ValueError`` for an out-of-range slot index, for a
malformed ``prevSlots`` program, or for a payload that sets none of the
command's fields.

A field the payload omits (absent or ``None``) is SKIPPED: its register keeps
the value the user set.  This is what both other executors of the same
command do — the firmware's ``execute_battery_charge`` (``power_limit >= 0``,
``s_voltage > 0``) and the mobile harvester's ``BatteryChargeExecutor``
("skip if absent") — and what the API relies on when it sends a
``set_battery_charge`` without ``powerLimit``.  Before this, an omitted field
raised ``KeyError`` and the whole command failed.
"""

from __future__ import annotations

from .register_spec import FieldWrite, SlotSpec, WriteCommand

# Fields where 0 means "do not write", not "write 0".  The API sends
# ``chargeVoltage: 0`` when it cannot work one out from the battery chemistry
# and cell count (``calculateChargeVoltage``, "0 = skip the voltage write"),
# and the firmware and the mobile harvester both skip a voltage of 0.  A power
# limit of 0 W, by contrast, is a real value and is written.
_ZERO_MEANS_SKIP = frozenset({"chargeVoltage"})

# The whole-program form of a slot command: a ``prevSlots`` array with one
# entry per slot. The API sends it for ``set_battery_charge`` whenever the
# inverter's settings mirror is fresh enough to rebuild the six slots from
# (``buildBatteryChargePayload``), and for every ``restore_battery_charge``
# that has a captured table. It carries no ``slotIndex``.
PROGRAM_FIELD = "prevSlots"

# A single-slot charge that also turns grid charge off on the other five slots,
# so only the event's window charges from the grid. The API's battery charge
# and the island's charge and maintenance fallback send it; the firmware
# honours it only on its single-slot branch, never on a program.
DISABLE_OTHERS_FIELD = "disableOtherSlots"

# One ``prevSlots`` entry key per slot field, in the order the edge firmware's
# ``tou_restore_all_slots`` (tou_writer.c) writes them. ``end`` is written only
# to a dedicated end register: on Deye and Sunsynk a slot ends where the next
# one starts, so the six starts already define every boundary.
_PROGRAM_KEYS = (
    ("start", "slotStart"),
    ("end", "slotEnd"),
    ("power", "powerLimit"),
    ("voltage", "chargeVoltage"),
    ("soc", "gridChargeSoc"),
    ("enable", "gridChargeEnabled"),
)

# Bits of the per-slot enable word this project understands: bit 0 is grid
# charge, bit 1 generator charge. A program takes these two from the payload
# and keeps every other bit as the inverter holds it. The firmware writes the
# payload word verbatim; the API built that word from a mirror up to 48 h old,
# so a bit the owner changed on the panel since would be overwritten. The phone
# harvester's BatteryChargeExecutor composes the word this way for that reason.
# With the mirror in step, both give the same word.
_ENABLE_KNOWN_BITS = 0x03


def is_set(f: FieldWrite, payload: dict) -> bool:
    """Return True when *payload* asks for field *f* to be written."""
    raw = payload.get(f.payload_field)
    if raw is None:
        return False
    zero_skips = f.payload_field in _ZERO_MEANS_SKIP and not isinstance(raw, bool)
    return not (zero_skips and raw == 0)


def _is_on(raw: object, f: FieldWrite) -> bool:
    """Return True when the payload value means 'set the bit'."""
    if isinstance(raw, bool):
        return raw
    return int(raw) == (f.on_value if f.on_value is not None else 1)


def _encode_value(
    f: FieldWrite,
    payload: dict,
    prior: dict[int, int],
    address: int,
) -> int:
    """Encode a single field value according to its encoding rule."""
    raw = payload[f.payload_field]

    if f.encoding.startswith("bit:"):
        bit = int(f.encoding.split(":", 1)[1])
        base = prior.get(address, 0)
        if f.clear_mask is not None:
            base &= ~f.clear_mask
        truthy = _is_on(raw, f)
        return ((base | (1 << bit)) if truthy else (base & ~(1 << bit))) & 0xFFFF

    # full_word
    if f.on_value is not None and isinstance(raw, bool):
        v: int = f.on_value if raw else (f.off_value if f.off_value is not None else 0)
    else:
        v = round(float(raw) / f.value_scale)

    if f.limits:
        lo = f.limits.get("min") if hasattr(f.limits, "get") else None
        hi = f.limits.get("max") if hasattr(f.limits, "get") else None
        if lo is not None:
            v = max(int(lo), v)
        if hi is not None:
            v = min(int(hi), v)

    if f.clear_mask is not None:
        base = prior.get(address, 0) & ~f.clear_mask
        v = base | v

    return v & 0xFFFF


def compute_register_writes(
    cmd: WriteCommand,
    payload: dict,
    prior: dict[int, int],
    unit_id: int = 1,
) -> list[tuple[int, int, int]]:
    """Return the ordered list of ``(unit_id, address, value)`` register writes.

    Parameters
    ----------
    cmd:
        The parsed ``WriteCommand`` (fields + optional slot spec).
    payload:
        Command payload dict keyed by payload-field names.
    prior:
        Most-recent raw register values keyed by address (used for RMW).
    unit_id:
        Modbus unit ID to embed in every tuple (default 1).

    Raises
    ------
    ValueError
        If a slot command carries an index outside ``0 .. count-1``, or the
        payload sets none of the command's fields.
    """
    out: list[tuple[int, int, int]] = []

    for f in cmd.fields:
        if not is_set(f, payload):
            continue
        out.append((unit_id, f.address, _encode_value(f, payload, prior, f.address)))

    if cmd.slot is not None and is_program(cmd, payload):
        out.extend(_program_writes(cmd.slot, payload[PROGRAM_FIELD], prior, unit_id))
    elif cmd.slot is not None:
        s = cmd.slot
        if payload.get(s.index_field) is None:
            raise ValueError(
                f"{cmd.command}: payload carries neither {s.index_field} nor {PROGRAM_FIELD}"
            )
        idx = int(payload[s.index_field])
        if not (0 <= idx < s.count):
            raise ValueError(f"slot index {idx} out of range 0..{s.count - 1}")
        for f in s.fields:
            if not is_set(f, payload):
                continue
            slot_idx = ((idx + 1) % s.count) if f.via_next_slot else idx
            addr = f.base + slot_idx * s.stride
            out.append((unit_id, addr, _encode_value(f, payload, prior, addr)))
        if payload.get(DISABLE_OTHERS_FIELD) is True:
            out.extend(_disable_other_slots(s, idx, prior, unit_id))

    if not out:
        fields = [f.payload_field for f in cmd.fields]
        if cmd.slot is not None:
            fields += [f.payload_field for f in cmd.slot.fields]
        raise ValueError(f"{cmd.command}: payload sets none of {', '.join(fields)}")

    return out


def _disable_other_slots(
    slot: SlotSpec, target: int, prior: dict[int, int], unit_id: int
) -> list[tuple[int, int, int]]:
    """Clear grid charge (bit 0) on every slot but *target*, as the firmware does.

    ``execute_battery_charge`` (zigbee_poller.c, calendar mode) skips a slot
    whose bit 0 is already clear and keeps every other bit of the word. It
    reads the words from the slot table it pre-read, which is *prior* here.
    The bits come back with the restore, from the table the ACK reports.
    """
    enable = program_field(slot, "enable")
    if enable is None:
        return []
    out = []
    for i in range(slot.count):
        addr = enable.base + i * slot.stride  # type: ignore[operator]
        word = prior.get(addr, 0)
        if i != target and word & 0x01:
            out.append((unit_id, addr, word & ~0x01 & 0xFFFF))
    return out


def is_program(cmd: WriteCommand, payload: dict) -> bool:
    """Return True when *payload* is the whole-program form of slot command *cmd*.

    ``prevSlots`` wins over ``slotIndex`` when a payload carries both, as the
    custom-action restore does: the firmware takes its per-slot branch
    whenever the array is present.
    """
    return cmd.slot is not None and payload.get(PROGRAM_FIELD) is not None


def program_field(slot: SlotSpec, key: str) -> FieldWrite | None:
    """Return the slot field a ``prevSlots`` entry *key* writes, or None.

    ``end`` maps only to a dedicated end register, never to the next slot's
    start.
    """
    name = dict(_PROGRAM_KEYS)[key]
    for f in slot.fields:
        if f.payload_field == name and not f.via_next_slot:
            return f
    return None


def _number(raw: object) -> int | float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return raw


def _program_writes(
    slot: SlotSpec,
    program: object,
    prior: dict[int, int],
    unit_id: int,
) -> list[tuple[int, int, int]]:
    """Return the writes for a ``prevSlots`` program, slot by slot.

    Mirrors ``tou_restore_all_slots``: per slot, start, end (dedicated end
    register only), power, voltage (skipped at 0), SOC and enable. A key an
    entry leaves out is skipped. The enable word needs the live register in
    *prior* (see ``_ENABLE_KNOWN_BITS``).
    """
    if not isinstance(program, list) or len(program) != slot.count:
        size = len(program) if isinstance(program, list | dict | str) else "?"
        raise ValueError(
            f"{PROGRAM_FIELD} must hold exactly {slot.count} slots, got {size}; "
            "refusing to write a partial program"
        )
    out: list[tuple[int, int, int]] = []
    for i, entry in enumerate(program):
        if not isinstance(entry, dict):
            raise ValueError(f"{PROGRAM_FIELD}[{i}] is not an object")
        for key, _name in _PROGRAM_KEYS:
            f = program_field(slot, key)
            raw = _number(entry.get(key))
            if f is None or raw is None:
                continue
            if key == "voltage" and raw == 0:
                continue
            addr = f.base + i * slot.stride  # type: ignore[operator]
            if key == "enable":
                value = (prior.get(addr, 0) & ~_ENABLE_KNOWN_BITS) | (int(raw) & _ENABLE_KNOWN_BITS)
                out.append((unit_id, addr, value & 0xFFFF))
            else:
                plain = FieldWrite(
                    payload_field=f.payload_field,
                    address=f.address,
                    base=f.base,
                    value_scale=f.value_scale,
                    limits=f.limits,
                )
                out.append(
                    (unit_id, addr, _encode_value(plain, {f.payload_field: raw}, prior, addr))
                )
    return out
