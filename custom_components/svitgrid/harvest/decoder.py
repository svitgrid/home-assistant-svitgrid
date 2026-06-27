"""Reference-decoder port — must match the Dart ReferenceDecoder exactly.

decode() mirrors reference_decoder.dart; sanitize() (Task 3) adds the
spec-derivable reader clamps. Pinned by the golden-vector contract test."""
from __future__ import annotations

from .register_spec import Derivation, ReadDef, RegisterSpec

RawRegisters = dict[int, dict[int, int]]  # unitId -> address -> raw word


def _raw_of(raw: RawRegisters, unit_id: int, address: int) -> int | None:
    return (raw.get(unit_id) or {}).get(address)


def _convert(read: ReadDef, raw_value: int) -> float:
    # RegisterDef.convert: sentinel, sign, scale, offset.
    if not read.signed and raw_value == 0xFFFF:
        return 0.0
    if read.signed and raw_value == 0x7FFF:
        return 0.0
    value = float(raw_value)
    if read.signed and raw_value >= 32768:
        value = float(raw_value - 65536)
    return value * read.scale + read.offset


def decode(spec: RegisterSpec, raw: RawRegisters) -> dict[str, float | None]:
    out: dict[str, float | None] = {}

    # 1. raw reads
    for d in spec.reads:
        if d.words == 2:
            hi = _raw_of(raw, d.unit_id, d.address)
            lo = _raw_of(raw, d.unit_id, d.address + 1)
            if hi is None or lo is None:
                out[d.field] = None
                continue
            v = (hi << 16) | lo
            if d.signed and v >= 0x80000000:
                v -= 0x100000000
            out[d.field] = v * d.scale + d.offset
        else:
            v = _raw_of(raw, d.unit_id, d.address)
            out[d.field] = None if v is None else _convert(d, v)

    # 2. derivations (declared order)
    for x in spec.derivations:
        if x.op == "sum":
            out[x.field] = sum((out.get(f) or 0.0) for f in x.inputs)
        elif x.op == "product":
            p = 1.0
            for f in x.inputs:
                p *= (out.get(f) or 0.0)
            out[x.field] = p
        elif x.op == "negate":
            v = out.get(x.inputs[0])
            out[x.field] = None if v is None else -v
        elif x.op == "scale":
            v = out.get(x.inputs[0])
            out[x.field] = None if v is None else v * (x.scale if x.scale is not None else 1.0)
        elif x.op == "passthrough":
            out[x.field] = out.get(x.inputs[0])
        elif x.op == "builtin":
            _apply_builtin(x, out, spec)
        else:
            raise ValueError(f"unknown op: {x.op}")
    return out


# Non-nullable double fields in Dart's InverterReading that default to 0.0.
# When a model's spec does not read a field (e.g. no-battery grid-tie inverters
# skip batterySoc/batteryPower/batteryVoltage/loadPower), the field is absent
# from decode()'s output dict entirely. sanitize() inserts 0.0 for those absent
# fields to match the Dart reader's behaviour. Explicitly-None entries (field key
# present with value None — meaning the register was in reads but data was missing)
# are intentionally left untouched.
_STANDARD_ZERO_FIELDS: frozenset[str] = frozenset({
    "batterySoc",
    "batteryPower",
    "batteryVoltage",
    "gridPower",
    "loadPower",
    "totalPvPower",
    "dailyPvEnergy",
    "dailyGridImportEnergy",
    "dailyGridExportEnergy",
    "dailyLoadEnergy",
})


def sanitize(fields: dict[str, float | None], spec: RegisterSpec) -> dict[str, float | None]:
    """Re-apply the spec-derivable reader clamps (spec §3.2).

    Steps:
    1. Insert 0.0 for standard non-nullable InverterReading fields that are
       entirely absent from the decode output (models that don't read them).
    2. Clamp batterySoc to [0, 100].
    batteryPower>50000 and batteryTemp[-20,80] are inside the builtins.
    batteryVoltage (HV/LV) and Huawei pvPower>=0 are NOT reproduced
    (model-property-dependent) — the cloud validator backstops."""
    out = dict(fields)
    for f in _STANDARD_ZERO_FIELDS:
        if f not in out:
            out[f] = 0.0
    soc = out.get("batterySoc")
    if soc is not None:
        out["batterySoc"] = max(0.0, min(100.0, soc))
    return out


def _apply_builtin(d: Derivation, out: dict[str, float | None], spec: RegisterSpec) -> None:
    b = d.builtin
    if b == "battery_temp_clamp":
        t = out.get(d.inputs[0])
        out[d.field] = t if (t is not None and -20 <= t <= 80) else None
    elif b == "battery_sign_normalize":
        raw = out.get(d.inputs[0])
        if raw is None:
            out[d.field] = None
            return
        p = raw
        if spec.flags.battery_positive_is_discharge:
            p = -p
        if abs(p) > 50000:
            p = 0.0
        out[d.field] = p
    elif b == "pv_power_from_vi":
        v = out.get(d.inputs[0]) or 0.0
        i = out.get(d.inputs[1]) or 0.0
        out[d.field] = v * i
    elif b == "phase_voltage_grid_or_load":
        _phase_select(d, out, threshold=100, sum_gate=False)
    elif b == "phase_load_ct_or_inverter":
        _phase_select(d, out, threshold=50, sum_gate=True)
    elif b == "grid_relay_bit":
        addr = spec.flags.grid_relay_address
        mask = spec.flags.grid_relay_mask
        if addr is None or mask is None:
            out[d.field] = None
            return
        raw_val = out.get(d.inputs[0])
        out[d.field] = None if raw_val is None else (1.0 if (int(raw_val) & mask) != 0 else 0.0)
    elif b == "daily_grid_unavailable":
        for f in d.inputs:
            out[f] = None
    else:
        raise ValueError(f"unhandled builtin: {b}")


def _phase_select(d: Derivation, out: dict[str, float | None], *, threshold: float, sum_gate: bool) -> None:
    inputs = list(d.inputs)
    marker = inputs.index("|") if "|" in inputs else -1
    group_a = inputs if marker < 0 else inputs[:marker]
    group_b = [] if marker < 0 else inputs[marker + 1:]
    a = [(out.get(f) or 0.0) for f in group_a]
    b = [(out.get(f) or 0.0) for f in group_b]
    a_gate = (sum(a) > threshold) if sum_gate else (len(a) > 0 and a[0] > threshold)
    chosen = a if a_gate else (b if b else a)
    for k, val in enumerate(chosen):
        out[f"{d.field}_{k + 1}"] = val
    out[d.field] = sum(chosen)
