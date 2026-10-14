"""Pure Python port of the Dart reference write-applier (SP-C Task 6).

Turns a ``WriteCommand`` + command payload + the inverter's prior register
values into the exact list of ``(unit_id, address, value)`` writes.

No I/O.  Raise ``ValueError`` for an out-of-range slot index, or for a
payload that sets none of the command's fields.

A field the payload omits (absent or ``None``) is SKIPPED: its register keeps
the value the user set.  This is what both other executors of the same
command do — the firmware's ``execute_battery_charge`` (``power_limit >= 0``,
``s_voltage > 0``) and the mobile harvester's ``BatteryChargeExecutor``
("skip if absent") — and what the API relies on when it sends a
``set_battery_charge`` without ``powerLimit``.  Before this, an omitted field
raised ``KeyError`` and the whole command failed.
"""

from __future__ import annotations

from .register_spec import FieldWrite, WriteCommand

# Fields where 0 means "do not write", not "write 0".  The API sends
# ``chargeVoltage: 0`` when it cannot work one out from the battery chemistry
# and cell count (``calculateChargeVoltage``, "0 = skip the voltage write"),
# and the firmware and the mobile harvester both skip a voltage of 0.  A power
# limit of 0 W, by contrast, is a real value and is written.
_ZERO_MEANS_SKIP = frozenset({"chargeVoltage"})


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

    if cmd.slot is not None:
        s = cmd.slot
        idx = int(payload[s.index_field])
        if not (0 <= idx < s.count):
            raise ValueError(f"slot index {idx} out of range 0..{s.count - 1}")
        for f in s.fields:
            if not is_set(f, payload):
                continue
            slot_idx = ((idx + 1) % s.count) if f.via_next_slot else idx
            addr = f.base + slot_idx * s.stride
            out.append((unit_id, addr, _encode_value(f, payload, prior, addr)))

    if not out:
        fields = [f.payload_field for f in cmd.fields]
        if cmd.slot is not None:
            fields += [f.payload_field for f in cmd.slot.fields]
        raise ValueError(f"{cmd.command}: payload sets none of {', '.join(fields)}")

    return out
