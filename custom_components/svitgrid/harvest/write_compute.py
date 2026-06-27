"""Pure Python port of the Dart reference write-applier (SP-C Task 6).

Turns a ``WriteCommand`` + command payload + the inverter's prior register
values into the exact list of ``(unit_id, address, value)`` writes.

No I/O.  Raise ``ValueError`` for an out-of-range slot index or a missing
required payload field (the latter surfaces naturally as a ``KeyError`` from
the payload dict).
"""
from __future__ import annotations

from .register_spec import FieldWrite, WriteCommand


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
        If a slot command carries an index outside ``0 .. count-1``.
    """
    out: list[tuple[int, int, int]] = []

    for f in cmd.fields:
        out.append((unit_id, f.address, _encode_value(f, payload, prior, f.address)))

    if cmd.slot is not None:
        s = cmd.slot
        idx = int(payload[s.index_field])
        if not (0 <= idx < s.count):
            raise ValueError(
                f"slot index {idx} out of range 0..{s.count - 1}"
            )
        for f in s.fields:
            slot_idx = ((idx + 1) % s.count) if f.via_next_slot else idx
            addr = f.base + slot_idx * s.stride
            out.append((unit_id, addr, _encode_value(f, payload, prior, addr)))

    return out
