# HA Direct Harvester — SP-B: Python Harvest Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Svitgrid HA add-on read the inverter directly over Modbus (Solarman-V5 / Modbus-TCP), decode registers via a Python port of SP-A's reference decoder, and feed the same reading payload into the existing store→drain→cloud pipeline — pinned to the Dart reader's output by shared golden vectors.

**Architecture:** A new `custom_components/svitgrid/harvest/` package: `register_spec.py` (spec model), `decoder.py` (reference-decoder port + `sanitize`), `transport.py` (`pysolarmanv5`/`pymodbus` clients + range batching, run in executors), `engine.py` (`poll_once` + `run_direct_harvest_loop`). `build_reading_payload` is refactored so the payload-assembly takes a decoded `{field: value}` dict; the HA-entity path and the harvest path are two producers of it. A Dart tool in the monorepo emits golden vectors `{spec, raw, expectedFields}` from the real reader; a vendored copy + a Python contract test pin the Python decoder to it.

**Tech Stack:** Python 3.11+, Home Assistant custom component, `pysolarmanv5`, `pymodbus`, `aiohttp`; `pytest` + `pytest-asyncio` + `pytest-homeassistant-custom-component`. Dart (monorepo, golden-vector tool only).

## Global Constraints

- **TDD always** — failing test first, watch it fail, implement, watch it pass, commit. No exceptions.
- **Two repos.** Tasks 1-3, 5-11 are in `home-assistant-svitgrid` (worktree `.worktrees/ha-direct-harvester-sp-b`, branch `feat/ha-direct-harvester-sp-b`). **Task 4 only** is in the `svitgrid` monorepo (its own worktree/branch) — the golden-vector Dart tool.
- **Register-spec API endpoint is `GET /api/v1/register-specs/:modelId`** (NOT `ha-register-specs`). The list is `GET /api/v1/register-specs`.
- **`RawRegisters` shape = `dict[int, dict[int, int]]`** (unitId → address → raw 16-bit word). Mirrors the Dart `RawRegisters = Map<int, Map<int,int>>`.
- **16-bit decode (canonical `RegisterDef.convert`)**: `if not signed and raw == 0xFFFF: return 0`; `if signed and raw == 0x7FFF: return 0`; if `signed and raw >= 32768: raw -= 65536`; return `raw * scale + offset`.
- **32-bit decode (`words==2`)**: hi = raw[addr], lo = raw[addr+1]; `v = (hi<<16)|lo`; if `signed and v >= 0x80000000: v -= 0x100000000`; return `v * scale + offset`. Missing hi or lo → `None`.
- **Built-in catalog (exactly 7)**: `pv_power_from_vi, battery_sign_normalize, battery_temp_clamp, phase_voltage_grid_or_load, phase_load_ct_or_inverter, grid_relay_bit, daily_grid_unavailable`. Logic must match the Dart `_applyBuiltin` / `_phaseSelect` verbatim (see Task 2).
- **`sanitize()` scope** (per spec §3.2): apply `batterySoc = clamp(0,100)` only; `batteryPower>50000→0` and `batteryTemperature[-20,80]` are already inside the builtins; `batteryVoltage` HV/LV and Huawei `pvPower≥0` are NOT reproduced (model-property-dependent) — golden vectors keep those in-range.
- **`null`/`None` means absent** — the payload builder omits absent fields; nothing fabricated ships.
- **Reading source stays `"edge"`** (`const.READING_SOURCE`). Payload contract unchanged.
- **Blocking socket I/O MUST run via `hass.async_add_executor_job`** — never call `pysolarmanv5`/`pymodbus` directly on the event loop.
- **Fail-soft loop** — a poll failure logs, backs off to the default interval, retries; never tears down the task (mirror `readings_publisher.run_loop`).
- **Worktree deps**: in the HA worktree run `pip install -r requirements-dev.txt` once; run tests with `python -m pytest <path> -v` from the worktree root.

---

## File Structure

**New (this repo, `custom_components/svitgrid/harvest/`):**
- `__init__.py` — package marker.
- `register_spec.py` — `RegisterSpec`, `ReadDef`, `Derivation`, `SpecFlags` dataclasses + `from_dict` + `BUILTIN_CATALOG` + `validate`.
- `decoder.py` — `decode(spec, raw) -> dict[str, float|None]` + `sanitize(fields, spec) -> dict`.
- `transport.py` — `plan_ranges(spec)`, `read_raw(spec, harvest_config, executor) -> RawRegisters`, Solarman + Modbus clients.
- `engine.py` — `assemble_from_decoded(...)` glue, `poll_once(...)`, `run_direct_harvest_loop(...)`.
- `spec_cache.py` — version-gated register-spec cache (mirror `preset_refresh.py`).

**Modified (this repo):**
- `custom_components/svitgrid/readings_publisher.py` — extract `assemble_payload(inverter_id, fields)`.
- `custom_components/svitgrid/api_client.py` — add `get_register_spec(model_id)`.
- `custom_components/svitgrid/config_flow.py` — add `async_step_harvest_config`.
- `custom_components/svitgrid/__init__.py` — spawn `run_direct_harvest_loop` when `harvest_config` present.
- `manifest.json` — add `pysolarmanv5`, `pymodbus` to `requirements`.
- `requirements.txt` — same.

**New (this repo, tests + fixtures):**
- `tests/harvest/test_register_spec.py`, `test_decoder.py`, `test_sanitize.py`, `test_transport.py`, `test_engine.py`, `test_golden_vectors.py`, `test_spec_cache.py`.
- `tests/test_assemble_payload.py`, `tests/test_get_register_spec.py`, `tests/test_config_flow_harvest.py`.
- `tests/fixtures/golden-vectors.json` (vendored).
- `scripts/sync-golden-vectors.sh`.

**New (monorepo, Task 4):**
- `packages/inverter_protocol/tool/export_golden_vectors.dart`
- `packages/inverter_protocol/golden-vectors.json`
- `packages/inverter_protocol/test/spec/golden_vectors_staleness_test.dart`

---

## Task 1: Register-spec model (`register_spec.py`)

**Files:**
- Create: `custom_components/svitgrid/harvest/__init__.py` (empty)
- Create: `custom_components/svitgrid/harvest/register_spec.py`
- Test: `tests/harvest/test_register_spec.py`

**Interfaces:**
- Produces:
  - `BUILTIN_CATALOG: frozenset[str]` (the 7 names).
  - `@dataclass(frozen=True) class ReadDef: field:str; address:int; words:int=1; signed:bool=False; scale:float=1.0; offset:float=0.0; unit_id:int=1; sentinel:int|None=None; function_code:str="FC03"`
  - `@dataclass(frozen=True) class Derivation: field:str; op:str; inputs:tuple[str,...]; scale:float|None=None; builtin:str|None=None`
  - `@dataclass(frozen=True) class SpecFlags: pv_power_from_voltage_current:bool=False; battery_positive_is_discharge:bool=False; daily_grid_unavailable:bool=False; uses_input_registers:bool=False; grid_relay_address:int|None=None; grid_relay_mask:int|None=None`
  - `@dataclass(frozen=True) class RegisterSpec: model_id:str; version:int; protocol:str; port:int; default_slave_id:int; flags:SpecFlags; reads:tuple[ReadDef,...]; derivations:tuple[Derivation,...]`
  - `RegisterSpec.from_dict(d: dict) -> RegisterSpec` — parses the JSON the API serves (camelCase keys: `modelId, defaultSlaveId, unitId, functionCode, pvPowerFromVoltageCurrent, batteryPositiveIsDischarge, dailyGridUnavailable, usesInputRegisters, gridRelayAddress, gridRelayMask`). `writes` is ignored (SP-C). `flags` is the JSON `flags` object (a dict of camelCase bool/number).
  - `RegisterSpec.validate(self) -> list[str]` — returns problems: a derivation `op == "builtin"` whose `builtin` ∉ `BUILTIN_CATALOG`; a derivation `input` naming no read/derivation field. (Defence in depth; the API already validated.)

- [ ] **Step 1: Write the failing test**

```python
# tests/harvest/test_register_spec.py
from custom_components.svitgrid.harvest.register_spec import (
    RegisterSpec, BUILTIN_CATALOG,
)

DEYE = {
    "modelId": "deye_sg04lp3", "version": 1, "source": "generated",
    "verified": True, "protocol": "solarman_v5", "port": 8899,
    "defaultSlaveId": 1,
    "flags": {"batteryPositiveIsDischarge": True},
    "reads": [
        {"field": "batterySoc", "address": 588},
        {"field": "batteryPower", "address": 590, "signed": True},
        {"field": "dailyPvEnergy", "address": 529, "scale": 0.1},
    ],
    "derivations": [
        {"field": "batteryPower", "op": "builtin",
         "builtin": "battery_sign_normalize", "inputs": ["batteryPower"]},
        {"field": "totalPvPower", "op": "sum", "inputs": ["pv1Power", "pv2Power"]},
    ],
    "writes": [],
}

def test_from_dict_parses_reads_and_flags():
    spec = RegisterSpec.from_dict(DEYE)
    assert spec.model_id == "deye_sg04lp3"
    assert spec.flags.battery_positive_is_discharge is True
    bp = next(r for r in spec.reads if r.field == "batteryPower")
    assert bp.signed is True and bp.scale == 1.0  # default
    soc = next(r for r in spec.reads if r.field == "batterySoc")
    assert soc.words == 1 and soc.unit_id == 1 and soc.function_code == "FC03"
    daily = next(r for r in spec.reads if r.field == "dailyPvEnergy")
    assert daily.scale == 0.1

def test_validate_rejects_unknown_builtin():
    d = {**DEYE, "derivations": [
        {"field": "x", "op": "builtin", "builtin": "nope", "inputs": ["batterySoc"]},
    ]}
    problems = RegisterSpec.from_dict(d).validate()
    assert any("nope" in p for p in problems)

def test_validate_rejects_dangling_input():
    d = {**DEYE, "derivations": [
        {"field": "x", "op": "sum", "inputs": ["batterySoc", "missing"]},
    ]}
    problems = RegisterSpec.from_dict(d).validate()
    assert any("missing" in p for p in problems)

def test_builtin_catalog_has_seven():
    assert BUILTIN_CATALOG == frozenset({
        "pv_power_from_vi", "battery_sign_normalize", "battery_temp_clamp",
        "phase_voltage_grid_or_load", "phase_load_ct_or_inverter",
        "grid_relay_bit", "daily_grid_unavailable",
    })
```

- [ ] **Step 2: Run it — expect FAIL**

Run: `python -m pytest tests/harvest/test_register_spec.py -v`
Expected: FAIL — module/classes don't exist (ImportError).

- [ ] **Step 3: Implement `register_spec.py`**

```python
# custom_components/svitgrid/harvest/register_spec.py
"""Register-spec data model — Python mirror of the Dart RegisterSpec.

Parses the JSON served by GET /api/v1/register-specs/:modelId. `writes` are
ignored here (SP-C consumes them)."""
from __future__ import annotations

from dataclasses import dataclass, field as _field

BUILTIN_CATALOG = frozenset({
    "pv_power_from_vi", "battery_sign_normalize", "battery_temp_clamp",
    "phase_voltage_grid_or_load", "phase_load_ct_or_inverter",
    "grid_relay_bit", "daily_grid_unavailable",
})


@dataclass(frozen=True)
class ReadDef:
    field: str
    address: int
    words: int = 1
    signed: bool = False
    scale: float = 1.0
    offset: float = 0.0
    unit_id: int = 1
    sentinel: int | None = None
    function_code: str = "FC03"

    @staticmethod
    def from_dict(d: dict) -> "ReadDef":
        return ReadDef(
            field=d["field"],
            address=int(d["address"]),
            words=int(d.get("words", 1)),
            signed=bool(d.get("signed", False)),
            scale=float(d.get("scale", 1.0)),
            offset=float(d.get("offset", 0.0)),
            unit_id=int(d.get("unitId", 1)),
            sentinel=d.get("sentinel"),
            function_code=d.get("functionCode", "FC03"),
        )


@dataclass(frozen=True)
class Derivation:
    field: str
    op: str
    inputs: tuple[str, ...]
    scale: float | None = None
    builtin: str | None = None

    @staticmethod
    def from_dict(d: dict) -> "Derivation":
        return Derivation(
            field=d["field"],
            op=d["op"],
            inputs=tuple(d.get("inputs", [])),
            scale=(float(d["scale"]) if d.get("scale") is not None else None),
            builtin=d.get("builtin"),
        )


@dataclass(frozen=True)
class SpecFlags:
    pv_power_from_voltage_current: bool = False
    battery_positive_is_discharge: bool = False
    daily_grid_unavailable: bool = False
    uses_input_registers: bool = False
    grid_relay_address: int | None = None
    grid_relay_mask: int | None = None

    @staticmethod
    def from_dict(d: dict) -> "SpecFlags":
        d = d or {}
        return SpecFlags(
            pv_power_from_voltage_current=bool(d.get("pvPowerFromVoltageCurrent", False)),
            battery_positive_is_discharge=bool(d.get("batteryPositiveIsDischarge", False)),
            daily_grid_unavailable=bool(d.get("dailyGridUnavailable", False)),
            uses_input_registers=bool(d.get("usesInputRegisters", False)),
            grid_relay_address=d.get("gridRelayAddress"),
            grid_relay_mask=d.get("gridRelayMask"),
        )


@dataclass(frozen=True)
class RegisterSpec:
    model_id: str
    version: int
    protocol: str
    port: int
    default_slave_id: int
    flags: SpecFlags
    reads: tuple[ReadDef, ...]
    derivations: tuple[Derivation, ...]

    @staticmethod
    def from_dict(d: dict) -> "RegisterSpec":
        return RegisterSpec(
            model_id=d["modelId"],
            version=int(d["version"]),
            protocol=d["protocol"],
            port=int(d["port"]),
            default_slave_id=int(d.get("defaultSlaveId", 1)),
            flags=SpecFlags.from_dict(d.get("flags") or {}),
            reads=tuple(ReadDef.from_dict(r) for r in d.get("reads", [])),
            derivations=tuple(Derivation.from_dict(x) for x in d.get("derivations", [])),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        known = {r.field for r in self.reads} | {x.field for x in self.derivations}
        for x in self.derivations:
            if x.op == "builtin" and (x.builtin not in BUILTIN_CATALOG):
                problems.append(f"unknown builtin: {x.builtin}")
            for inp in x.inputs:
                if inp == "|":  # phase-select group separator, not a field
                    continue
                if inp not in known:
                    problems.append(f"derivation {x.field} references missing field: {inp}")
        return problems
```

- [ ] **Step 4: Run it — expect PASS**

Run: `python -m pytest tests/harvest/test_register_spec.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/svitgrid/harvest/__init__.py \
        custom_components/svitgrid/harvest/register_spec.py \
        tests/harvest/test_register_spec.py
git commit -m "feat(harvest): register-spec model (Python mirror of Dart RegisterSpec) (SP-B)"
```

---

## Task 2: Decoder port (`decoder.py` — `decode`)

**Files:**
- Create: `custom_components/svitgrid/harvest/decoder.py`
- Test: `tests/harvest/test_decoder.py`

**Interfaces:**
- Consumes: `RegisterSpec`, `ReadDef`, `Derivation`, `SpecFlags` (Task 1).
- Produces:
  - `RawRegisters = dict[int, dict[int, int]]` (type alias).
  - `decode(spec: RegisterSpec, raw: RawRegisters) -> dict[str, float | None]` — a verbatim port of the Dart `ReferenceDecoder.decode`: 16-bit `_convert`, 32-bit `words==2`, the 6 derivation ops, and `_apply_builtin` for all 7 builtins including `_phase_select` (the `"|"` group-marker convention). `None` = absent.
- Port the exact Dart logic (Global Constraints + the Dart source below). `_convert(read, raw)` implements `RegisterDef.convert`.

- [ ] **Step 1: Write the failing test** (port of the Dart reference_decoder unit tests + builtins)

```python
# tests/harvest/test_decoder.py
import math
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.decoder import decode

def _spec(**over):
    base = {
        "modelId": "m", "version": 1, "protocol": "solarman_v5", "port": 8899,
        "defaultSlaveId": 1, "flags": {}, "reads": [], "derivations": [], "writes": [],
    }
    base.update(over)
    return RegisterSpec.from_dict(base)

def test_scaled_unsigned_read():
    spec = _spec(reads=[{"field": "batteryVoltage", "address": 587, "scale": 0.01}])
    assert math.isclose(decode(spec, {1: {587: 5230}})["batteryVoltage"], 52.30)

def test_signed_negative_read():
    spec = _spec(reads=[{"field": "gridPower", "address": 625, "signed": True}])
    assert decode(spec, {1: {625: 64536}})["gridPower"] == -1000.0

def test_unsigned_sentinel_0xffff_is_zero():
    spec = _spec(reads=[{"field": "ev", "address": 260}])
    assert decode(spec, {1: {260: 0xFFFF}})["ev"] == 0.0

def test_signed_sentinel_0x7fff_is_zero():
    spec = _spec(reads=[{"field": "x", "address": 1, "signed": True}])
    assert decode(spec, {1: {1: 0x7FFF}})["x"] == 0.0

def test_offset_after_scale():
    spec = _spec(reads=[{"field": "t", "address": 586, "scale": 0.1, "offset": -100.0}])
    assert math.isclose(decode(spec, {1: {586: 1290}})["t"], 29.0)

def test_32bit_unsigned():
    spec = _spec(reads=[{"field": "e", "address": 100, "words": 2, "scale": 0.01}])
    # hi=1, lo=0x86A0 → 0x000186A0 = 100000 → *0.01 = 1000.0
    assert math.isclose(decode(spec, {1: {100: 0x0001, 101: 0x86A0}})["e"], 1000.0)

def test_32bit_signed_negative():
    spec = _spec(reads=[{"field": "p", "address": 200, "words": 2, "signed": True}])
    # 0xFFFFFFFF → -1
    assert decode(spec, {1: {200: 0xFFFF, 201: 0xFFFF}})["p"] == -1.0

def test_32bit_missing_low_word_is_none():
    spec = _spec(reads=[{"field": "p", "address": 200, "words": 2}])
    assert decode(spec, {1: {200: 0x0001}})["p"] is None

def test_sum_and_product():
    spec = _spec(
        reads=[{"field": "v", "address": 1, "scale": 0.1},
               {"field": "i", "address": 2, "scale": 0.1},
               {"field": "a", "address": 3}, {"field": "b", "address": 4}],
        derivations=[
            {"field": "p", "op": "product", "inputs": ["v", "i"]},
            {"field": "s", "op": "sum", "inputs": ["a", "b"]},
        ])
    out = decode(spec, {1: {1: 3000, 2: 50, 3: 100, 4: 200}})
    assert math.isclose(out["p"], 1500.0) and out["s"] == 300.0

def test_battery_sign_normalize_flips_and_clamps():
    spec = _spec(
        flags={"batteryPositiveIsDischarge": True},
        reads=[{"field": "batteryPower", "address": 590, "signed": True}],
        derivations=[{"field": "batteryPower", "op": "builtin",
                      "builtin": "battery_sign_normalize", "inputs": ["batteryPower"]}])
    assert decode(spec, {1: {590: 1500}})["batteryPower"] == -1500.0

def test_battery_temp_clamp_out_of_range_is_none():
    spec = _spec(
        reads=[{"field": "t", "address": 586, "scale": 0.1, "offset": -100.0}],
        derivations=[{"field": "t", "op": "builtin",
                      "builtin": "battery_temp_clamp", "inputs": ["t"]}])
    # raw 500 → -50°C → out of [-20,80] → None
    out = decode(spec, {1: {586: 500}})
    assert "t" in out and out["t"] is None

def test_per_unit_id_read():
    spec = _spec(protocol="modbus_tcp",
                 reads=[{"field": "soc", "address": 843, "unitId": 100}])
    assert decode(spec, {100: {843: 87}})["soc"] == 87.0
```

- [ ] **Step 2: Run it — expect FAIL** (`decode` missing).

Run: `python -m pytest tests/harvest/test_decoder.py -v`

- [ ] **Step 3: Implement `decoder.py` `decode` (port the Dart verbatim)**

Reference — Dart `ReferenceDecoder.decode` / `_applyBuiltin` / `_phaseSelect`
(`packages/inverter_protocol/lib/src/spec/reference_decoder.dart`). Python port:

```python
# custom_components/svitgrid/harvest/decoder.py
"""Reference-decoder port — must match the Dart ReferenceDecoder exactly.

decode() mirrors reference_decoder.dart; sanitize() (Task 3) adds the
spec-derivable reader clamps. Pinned by the golden-vector contract test."""
from __future__ import annotations

from .register_spec import RegisterSpec, ReadDef, Derivation

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
```

- [ ] **Step 4: Run it — expect PASS** (12 tests).

Run: `python -m pytest tests/harvest/test_decoder.py -v`

- [ ] **Step 5: Commit**

```bash
git add custom_components/svitgrid/harvest/decoder.py tests/harvest/test_decoder.py
git commit -m "feat(harvest): decoder port — 16/32-bit + derivations + builtins (SP-B)"
```

---

## Task 3: `sanitize()` (the spec-derivable reader clamps)

**Files:**
- Modify: `custom_components/svitgrid/harvest/decoder.py` (add `sanitize`)
- Test: `tests/harvest/test_sanitize.py`

**Interfaces:**
- Consumes: `decode` output + `RegisterSpec`.
- Produces: `sanitize(fields: dict[str, float|None], spec: RegisterSpec) -> dict[str, float|None]` — applies `batterySoc = clamp(0,100)` when present and non-None. Does NOT touch `batteryPower`/`batteryTemperature` (handled in builtins) nor `batteryVoltage`/`pvPower` (model-property-dependent, per spec §3.2). Returns a new dict (pure).

- [ ] **Step 1: Write the failing test**

```python
# tests/harvest/test_sanitize.py
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.decoder import sanitize

def _spec():
    return RegisterSpec.from_dict({
        "modelId": "m", "version": 1, "protocol": "solarman_v5", "port": 8899,
        "defaultSlaveId": 1, "flags": {}, "reads": [], "derivations": [], "writes": [],
    })

def test_battery_soc_clamped_high():
    assert sanitize({"batterySoc": 120.0}, _spec())["batterySoc"] == 100.0

def test_battery_soc_clamped_low():
    assert sanitize({"batterySoc": -5.0}, _spec())["batterySoc"] == 0.0

def test_battery_soc_in_range_untouched():
    assert sanitize({"batterySoc": 73.0}, _spec())["batterySoc"] == 73.0

def test_none_and_absent_untouched():
    out = sanitize({"batterySoc": None, "gridPower": 500.0}, _spec())
    assert out["batterySoc"] is None and out["gridPower"] == 500.0

def test_pure_does_not_mutate_input():
    src = {"batterySoc": 120.0}
    sanitize(src, _spec())
    assert src["batterySoc"] == 120.0
```

- [ ] **Step 2: Run — expect FAIL** (`sanitize` missing).

- [ ] **Step 3: Implement `sanitize` in `decoder.py`**

```python
def sanitize(fields: dict[str, float | None], spec: RegisterSpec) -> dict[str, float | None]:
    """Re-apply the spec-derivable reader clamps (spec §3.2).

    Only batterySoc clamp lives here; batteryPower>50000 and batteryTemp[-20,80]
    are inside the builtins. batteryVoltage (HV/LV) and Huawei pvPower>=0 are
    NOT reproduced (model-property-dependent) — the cloud validator backstops."""
    out = dict(fields)
    soc = out.get("batterySoc")
    if soc is not None:
        out["batterySoc"] = max(0.0, min(100.0, soc))
    return out
```

- [ ] **Step 4: Run — expect PASS** (5 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/svitgrid/harvest/decoder.py tests/harvest/test_sanitize.py
git commit -m "feat(harvest): sanitize() — batterySoc clamp (spec-derivable reader clamp) (SP-B)"
```

---

## Task 4: Golden-vector export tool (MONOREPO)

**Repo:** `svitgrid` monorepo. Create a worktree: `cd ~/git/svitgrid && git worktree add .worktrees/ha-golden-vectors -b feat/ha-golden-vectors && cd .worktrees/ha-golden-vectors && (cd packages/inverter_protocol && dart pub get)`.

**Files:**
- Create: `packages/inverter_protocol/tool/export_golden_vectors.dart`
- Create: `packages/inverter_protocol/golden-vectors.json` (generated)
- Test: `packages/inverter_protocol/test/spec/golden_vectors_staleness_test.dart`

**Interfaces:**
- Consumes: `buildSpecsForModels`, `kGeneratedModelIds` (`tool/export_register_specs.dart`); the contract test's `modelFixture` map, `comparedFields`, `readingField`, `loadRaw`, and the per-protocol reader calls (`SolarmanReader.decodeFromRaw(regs, flattenUnit1(raw))`, `VictronModbusTcpReader.decodeFromRaw(regs, raw)`, `HuaweiModbusTcpReader.decodeFromRaw(regs, flattenUnit1(raw))`).
- Produces: `golden-vectors.json` = `{ "sourceCommit": "<git short sha, passed via env or left 'dev'>", "vectors": [ {modelId, spec: <spec.toJson()>, rawRegisters: {unitId: {addr: word}}, expectedFields: {field: value|null}} ] }`, one vector per `kGeneratedModelIds` model, `expectedFields` = the 7 `comparedFields` from the real reader. Pretty-printed (`JsonEncoder.withIndent('  ')`) + trailing newline.

The tool factors the contract test's reader-dispatch + fixture-load into reusable helpers (extract them into a small library the tool and the contract test share, OR duplicate the tiny dispatch — prefer extracting `lib/src/spec/golden_support.dart` with `RawRegisters loadFixtureRaw(modelId)` and `Map<String,double?> readerExpected(modelId, regs, raw)` and have BOTH the contract test and this tool call it, to avoid drift).

- [ ] **Step 1: Write the failing staleness test**

```dart
// packages/inverter_protocol/test/spec/golden_vectors_staleness_test.dart
import 'dart:convert';
import 'dart:io';
import 'package:test/test.dart';
import '../../tool/export_golden_vectors.dart' as gv;

void main() {
  test('committed golden-vectors.json is up to date', () {
    final built = gv.buildGoldenVectorsJson(); // String (pretty + trailing \n)
    final file = File('golden-vectors.json');
    expect(file.existsSync(), isTrue,
        reason: 'missing — run: dart run tool/export_golden_vectors.dart');
    expect(file.readAsStringSync(), built,
        reason: 'golden-vectors.json is STALE — run the tool and commit');
  });

  test('every generated model has a vector with the 7 compared fields', () {
    final parsed = jsonDecode(gv.buildGoldenVectorsJson()) as Map<String, dynamic>;
    final vectors = (parsed['vectors'] as List).cast<Map<String, dynamic>>();
    expect(vectors, isNotEmpty);
    for (final v in vectors) {
      final ef = (v['expectedFields'] as Map).keys.toSet();
      expect(ef.containsAll(<String>{
        'batterySoc', 'batteryPower', 'batteryVoltage', 'gridPower',
        'loadPower', 'totalPvPower', 'dailyPvEnergy',
      }), isTrue, reason: 'vector ${v['modelId']} missing compared fields');
    }
  });
}
```

- [ ] **Step 2: Run — expect FAIL** (tool missing).

Run: `cd packages/inverter_protocol && dart test test/spec/golden_vectors_staleness_test.dart`

- [ ] **Step 3: Implement the tool** (and extract `golden_support.dart`; refactor the contract test to use it)

```dart
// packages/inverter_protocol/tool/export_golden_vectors.dart
import 'dart:convert';
import 'dart:io';
import 'package:inverter_protocol/inverter_protocol.dart';
import 'package:inverter_protocol/src/spec/golden_support.dart';
import 'export_register_specs.dart' show buildSpecsForModels, kGeneratedModelIds;

const _comparedFields = [
  'batterySoc', 'batteryPower', 'batteryVoltage', 'gridPower',
  'loadPower', 'totalPvPower', 'dailyPvEnergy',
];

String buildGoldenVectorsJson() {
  final specByModel = {
    for (final s in buildSpecsForModels(kGeneratedModelIds)) s.modelId: s,
  };
  final vectors = <Map<String, dynamic>>[];
  for (final modelId in kGeneratedModelIds) {
    final spec = specByModel[modelId]!;
    final raw = loadFixtureRaw(modelId); // RawRegisters
    final reading = readerDecode(modelId, raw); // InverterReading via the right reader
    final expected = <String, double?>{
      for (final f in _comparedFields) f: readingFieldValue(reading, f),
    };
    vectors.add({
      'modelId': modelId,
      'spec': spec.toJson(),
      'rawRegisters': {
        for (final e in raw.entries)
          e.key.toString(): {for (final a in e.value.entries) a.key.toString(): a.value},
      },
      'expectedFields': expected,
    });
  }
  const encoder = JsonEncoder.withIndent('  ');
  return '${encoder.convert({'sourceCommit': _sourceCommit(), 'vectors': vectors})}\n';
}

String _sourceCommit() => Platform.environment['GOLDEN_SOURCE_COMMIT'] ?? 'dev';

void main() {
  File('golden-vectors.json').writeAsStringSync(buildGoldenVectorsJson());
  stdout.writeln('wrote golden-vectors.json');
}
```

`lib/src/spec/golden_support.dart` exposes `RawRegisters loadFixtureRaw(String modelId)` (reads `test/spec/fixtures/${modelFixture[modelId]}.raw.json`), `InverterReading readerDecode(String modelId, RawRegisters raw)` (Solarman/Huawei flatten unit1, Victron full — by brand/protocol), and `double? readingFieldValue(InverterReading r, String field)` (the `comparedFields` switch). Move `modelFixture`, the reader dispatch, and `readingField` out of the contract test into this file and have the contract test import them (no behavior change — run `dart test test/spec/register_spec_contract_test.dart` to confirm still green).

> Note: `loadFixtureRaw` reads files relative to CWD `packages/inverter_protocol`; both the tool (`dart run`) and the test run from there.

- [ ] **Step 4: Generate + run tests**

```bash
cd packages/inverter_protocol
dart run tool/export_golden_vectors.dart
dart test test/spec/golden_vectors_staleness_test.dart test/spec/register_spec_contract_test.dart
```
Expected: tool writes the file; both tests PASS; full `dart test` still green.

- [ ] **Step 5: Commit (monorepo branch)**

```bash
git add packages/inverter_protocol/tool/export_golden_vectors.dart \
        packages/inverter_protocol/lib/src/spec/golden_support.dart \
        packages/inverter_protocol/lib/inverter_protocol.dart \
        packages/inverter_protocol/golden-vectors.json \
        packages/inverter_protocol/test/spec/register_spec_contract_test.dart \
        packages/inverter_protocol/test/spec/golden_vectors_staleness_test.dart
git commit -m "feat(inverter-protocol): golden-vector export tool + staleness gate (HA SP-B)"
```

> This monorepo branch is merged separately (ff-merge to monorepo main) when SP-B lands. The HA-repo vendoring (Task 5) copies the committed `golden-vectors.json`.

---

## Task 5: Vendored golden vectors + checksum guard + Python contract test

**Repo:** HA repo (back in `.worktrees/ha-direct-harvester-sp-b`).

**Files:**
- Create: `scripts/sync-golden-vectors.sh`
- Create: `tests/fixtures/golden-vectors.json` (vendored copy from the monorepo)
- Test: `tests/harvest/test_golden_vectors.py`

**Interfaces:**
- Consumes: `decode` + `sanitize` (Tasks 2-3), `RegisterSpec.from_dict` (Task 1), the vendored vectors.
- The contract test loads `tests/fixtures/golden-vectors.json`, and for each vector: builds the spec via `RegisterSpec.from_dict(v["spec"])`, builds `raw` (parse string keys → int), computes `sanitize(decode(spec, raw), spec)`, and asserts each `expectedFields[field]` matches (None==None, else `isclose(..., abs_tol=1e-6)`). A second test asserts the vendored file's `sourceCommit` is present (loud if someone hand-edits).

- [ ] **Step 1: Write the failing test**

```python
# tests/harvest/test_golden_vectors.py
import json
import math
import pathlib
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.decoder import decode, sanitize

VECTORS = pathlib.Path(__file__).parent.parent / "fixtures" / "golden-vectors.json"

def _load():
    return json.loads(VECTORS.read_text())

def _raw(d):
    return {int(u): {int(a): w for a, w in addrs.items()} for u, addrs in d.items()}

def test_python_decoder_matches_dart_reader_for_every_vector():
    data = _load()
    assert data["vectors"], "no vectors"
    for v in data["vectors"]:
        spec = RegisterSpec.from_dict(v["spec"])
        out = sanitize(decode(spec, _raw(v["rawRegisters"])), spec)
        for field, expected in v["expectedFields"].items():
            actual = out.get(field)
            if expected is None:
                assert actual is None, f"{v['modelId']}.{field}: expected None, got {actual}"
            else:
                assert actual is not None, f"{v['modelId']}.{field}: expected {expected}, got None"
                assert math.isclose(actual, expected, abs_tol=1e-6), \
                    f"{v['modelId']}.{field}: {actual} != {expected}"

def test_vendored_vectors_have_source_commit():
    assert _load().get("sourceCommit"), "golden-vectors.json missing sourceCommit header"
```

- [ ] **Step 2: Vendor the file + write the sync script**

Create `scripts/sync-golden-vectors.sh`:
```bash
#!/usr/bin/env bash
# Copy the monorepo's generated golden vectors into this repo's test fixtures.
# Run after the monorepo's export_golden_vectors.dart regenerates them.
set -euo pipefail
SRC="${1:-$HOME/git/svitgrid/packages/inverter_protocol/golden-vectors.json}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/golden-vectors.json"
[ -f "$SRC" ] || { echo "source not found: $SRC" >&2; exit 1; }
cp "$SRC" "$DEST"
echo "vendored $SRC -> $DEST"
```
Then run it (or copy manually from the Task-4 worktree's committed file):
```bash
chmod +x scripts/sync-golden-vectors.sh
mkdir -p tests/fixtures
./scripts/sync-golden-vectors.sh ~/git/svitgrid/.worktrees/ha-golden-vectors/packages/inverter_protocol/golden-vectors.json
```

- [ ] **Step 3: Run the contract test — expect PASS**

Run: `python -m pytest tests/harvest/test_golden_vectors.py -v`
Expected: PASS (the Python decoder reproduces the Dart reader for every vector). If a field mismatches, the Python `decode`/`sanitize` diverges from Dart — fix the Python port (the vectors are ground truth), not the test.

- [ ] **Step 4: Commit**

```bash
git add scripts/sync-golden-vectors.sh tests/fixtures/golden-vectors.json \
        tests/harvest/test_golden_vectors.py
git commit -m "test(harvest): vendored golden vectors + Python<->Dart contract test (SP-B)"
```

---

## Task 6: Transport (`transport.py`)

**Files:**
- Create: `custom_components/svitgrid/harvest/transport.py`
- Modify: `manifest.json` (requirements), `requirements.txt`
- Test: `tests/harvest/test_transport.py`

**Interfaces:**
- Consumes: `RegisterSpec`, `RawRegisters` (Tasks 1-2).
- Produces:
  - `plan_ranges(spec: RegisterSpec) -> list[tuple[int, int, int, str]]` — pure: groups `spec.reads` into `(unitId, startAddr, count, functionCode)` contiguous ranges (a 32-bit read spans `address..address+1`), per `(unitId, functionCode)`, capped at `MAX_RANGE = 100` registers; sorted. Returns the read plan.
  - `async def read_raw(hass, spec, cfg) -> RawRegisters` — selects the client by `spec.protocol`, executes each planned range via `hass.async_add_executor_job`, assembles the `RawRegisters` map. `cfg` is the `harvest_config` dict (`ip, port, slave_id, logger_serial`).
  - Sync helpers `_read_solarman(cfg, ranges) -> RawRegisters` (uses `pysolarmanv5.PySolarmanV5`) and `_read_modbus(cfg, ranges) -> RawRegisters` (uses `pymodbus.client.ModbusTcpClient`), each opening one connection, reading every range, closing.
- `plan_ranges` is the unit-testable core; the sync helpers are thin and tested against library test doubles.

- [ ] **Step 1: Write the failing test (focus on `plan_ranges` — pure)**

```python
# tests/harvest/test_transport.py
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.transport import plan_ranges

def _spec(reads, protocol="solarman_v5"):
    return RegisterSpec.from_dict({
        "modelId": "m", "version": 1, "protocol": protocol, "port": 8899,
        "defaultSlaveId": 1, "flags": {}, "reads": reads, "derivations": [], "writes": [],
    })

def test_contiguous_reads_grouped_into_one_range():
    spec = _spec([{"field": "a", "address": 100}, {"field": "b", "address": 101},
                  {"field": "c", "address": 102}])
    ranges = plan_ranges(spec)
    assert ranges == [(1, 100, 3, "FC03")]

def test_32bit_read_spans_two_registers():
    spec = _spec([{"field": "e", "address": 200, "words": 2}])
    assert plan_ranges(spec) == [(1, 200, 2, "FC03")]

def test_distant_reads_split_into_separate_ranges():
    spec = _spec([{"field": "a", "address": 100}, {"field": "z", "address": 900}])
    ranges = plan_ranges(spec)
    assert (1, 100, 1, "FC03") in ranges and (1, 900, 1, "FC03") in ranges
    assert len(ranges) == 2

def test_per_unit_id_grouped_separately():
    spec = _spec([{"field": "a", "address": 843, "unitId": 100},
                  {"field": "b", "address": 784, "unitId": 247}], protocol="modbus_tcp")
    ranges = plan_ranges(spec)
    units = {r[0] for r in ranges}
    assert units == {100, 247}

def test_fc04_grouped_separately_from_fc03():
    spec = _spec([{"field": "a", "address": 10, "functionCode": "FC03"},
                  {"field": "b", "address": 11, "functionCode": "FC04"}])
    fcs = {r[3] for r in plan_ranges(spec)}
    assert fcs == {"FC03", "FC04"}
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement `transport.py`**

```python
# custom_components/svitgrid/harvest/transport.py
"""Wire transport: plan contiguous register ranges from the spec, then read
them over Solarman V5 (pysolarmanv5) or Modbus TCP (pymodbus). All blocking
socket I/O is run by the caller via hass.async_add_executor_job."""
from __future__ import annotations

import logging

from .register_spec import RegisterSpec
from .decoder import RawRegisters

_LOGGER = logging.getLogger(__name__)
MAX_RANGE = 100  # registers per read


def plan_ranges(spec: RegisterSpec) -> list[tuple[int, int, int, str]]:
    """Group reads into (unitId, startAddr, count, functionCode) ranges."""
    # collect (unitId, fc) -> set of addresses (a words==2 read occupies addr, addr+1)
    buckets: dict[tuple[int, str], set[int]] = {}
    for r in spec.reads:
        key = (r.unit_id, r.function_code)
        addrs = buckets.setdefault(key, set())
        addrs.add(r.address)
        if r.words == 2:
            addrs.add(r.address + 1)
    ranges: list[tuple[int, int, int, str]] = []
    for (unit_id, fc), addr_set in buckets.items():
        addrs = sorted(addr_set)
        start = prev = addrs[0]
        for a in addrs[1:]:
            if a == prev + 1 and (a - start + 1) <= MAX_RANGE:
                prev = a
                continue
            ranges.append((unit_id, start, prev - start + 1, fc))
            start = prev = a
        ranges.append((unit_id, start, prev - start + 1, fc))
    ranges.sort()
    return ranges


async def read_raw(hass, spec: RegisterSpec, cfg: dict) -> RawRegisters:
    ranges = plan_ranges(spec)
    if spec.protocol == "solarman_v5":
        return await hass.async_add_executor_job(_read_solarman, cfg, ranges)
    if spec.protocol == "modbus_tcp":
        return await hass.async_add_executor_job(_read_modbus, cfg, ranges)
    raise ValueError(f"unsupported protocol: {spec.protocol}")


def _read_solarman(cfg: dict, ranges: list[tuple[int, int, int, str]]) -> RawRegisters:
    from pysolarmanv5 import PySolarmanV5  # imported lazily so tests can stub
    out: RawRegisters = {}
    sm = PySolarmanV5(
        cfg["ip"], int(cfg["logger_serial"]), port=int(cfg.get("port", 8899)),
        mb_slave_id=int(cfg.get("slave_id", 1)), socket_timeout=8, auto_reconnect=False,
    )
    try:
        for unit_id, start, count, _fc in ranges:
            words = sm.read_holding_registers(register_addr=start, quantity=count)
            slot = out.setdefault(unit_id, {})
            for i, w in enumerate(words):
                slot[start + i] = w
    finally:
        try:
            sm.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return out


def _read_modbus(cfg: dict, ranges: list[tuple[int, int, int, str]]) -> RawRegisters:
    from pymodbus.client import ModbusTcpClient  # lazy import
    out: RawRegisters = {}
    client = ModbusTcpClient(cfg["ip"], port=int(cfg.get("port", 502)), timeout=8)
    try:
        client.connect()
        for unit_id, start, count, fc in ranges:
            if fc == "FC04":
                rr = client.read_input_registers(start, count=count, slave=unit_id)
            else:
                rr = client.read_holding_registers(start, count=count, slave=unit_id)
            if rr.isError():
                _LOGGER.debug("modbus read error unit=%s addr=%s: %s", unit_id, start, rr)
                continue
            slot = out.setdefault(unit_id, {})
            for i, w in enumerate(rr.registers):
                slot[start + i] = w
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return out
```

Add to `manifest.json` `requirements` and `requirements.txt`: `"pysolarmanv5>=3.0"`, `"pymodbus>=3.5"` (pin to the latest 3.x; confirm the read-method signatures `read_holding_registers(register_addr=, quantity=)` for pysolarmanv5 and `read_holding_registers(address, count=, slave=)` for pymodbus against the installed versions during implementation — adjust the call sites if the installed API differs, and add a test note).

- [ ] **Step 4: Run `plan_ranges` tests — expect PASS.** (The sync read helpers are covered by a light test using a stubbed `PySolarmanV5`/`ModbusTcpClient` via `monkeypatch` — add one happy-path test each asserting the assembled `RawRegisters` for a 2-register range.)

- [ ] **Step 5: Commit**

```bash
git add custom_components/svitgrid/harvest/transport.py tests/harvest/test_transport.py \
        manifest.json requirements.txt
git commit -m "feat(harvest): transport — range planning + Solarman/Modbus clients (SP-B)"
```

---

## Task 7: `build_reading_payload` refactor → `assemble_payload(fields)`

**Files:**
- Modify: `custom_components/svitgrid/readings_publisher.py`
- Test: `tests/test_assemble_payload.py` (+ existing `tests/test_readings_publisher.py` must still pass)

**Interfaces:**
- Produces: `assemble_payload(*, inverter_id: str, fields: dict[str, float]) -> dict` — the existing payload-assembly logic (timestamp, `inverterId`, `source`, `pvPower` aggregation, `_PV_STRING_API_NAMES` rename) operating on a pre-collected `fields` dict. `build_reading_payload(*, hass, inverter_id, entity_map)` keeps its signature but now collects `fields` from HA entities then `return assemble_payload(inverter_id=inverter_id, fields=fields)`.
- `assemble_payload` must produce byte-identical output to today's `build_reading_payload` for the same field values (the existing test is the guard).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_assemble_payload.py
from custom_components.svitgrid.readings_publisher import assemble_payload

def test_assembles_renames_and_aggregates():
    payload = assemble_payload(inverter_id="inv-1", fields={
        "batterySoc": 85.0, "pv1Power": 2000.0, "pv2Power": 1800.0, "gridPower": 500.0,
    })
    assert payload["inverterId"] == "inv-1"
    assert payload["source"] == "edge"
    assert "timestamp" in payload
    assert payload["batterySoc"] == 85.0
    # per-string renamed to API names; aggregate present
    assert payload["pvPower1"] == 2000.0 and payload["pvPower2"] == 1800.0
    assert "pv1Power" not in payload and "pv2Power" not in payload
    assert payload["pvPower"] == 3800.0

def test_no_pv_means_no_pvpower_key():
    payload = assemble_payload(inverter_id="i", fields={"batterySoc": 50.0})
    assert "pvPower" not in payload
```

- [ ] **Step 2: Run — expect FAIL** (`assemble_payload` missing).

- [ ] **Step 3: Refactor `readings_publisher.py`**

Extract the assembly into `assemble_payload`; have `build_reading_payload` collect `fields` then delegate:
```python
def assemble_payload(*, inverter_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Assemble the canonical reading payload from already-collected field values.
    (Extracted so both the HA-entity path and the direct-harvest engine share it.)"""
    payload: dict[str, Any] = {
        "inverterId": inverter_id,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": READING_SOURCE,
    }
    for field, value in fields.items():
        payload[field] = value
    pv_total = 0.0
    has_any_pv = False
    for pv_field in ("pv1Power", "pv2Power", "pv3Power", "pv4Power"):
        if pv_field in payload:
            pv_total += payload[pv_field]
            has_any_pv = True
    if has_any_pv:
        payload["pvPower"] = pv_total
    for internal, api_name in _PV_STRING_API_NAMES.items():
        if internal in payload:
            payload[api_name] = payload.pop(internal)
    return payload


def build_reading_payload(*, hass, inverter_id, entity_map):
    fields: dict[str, Any] = {}
    for field, entity_id in entity_map.items():
        state = hass.states.get(entity_id)
        if state is None:
            continue
        raw = state.state
        if raw in _UNAVAILABLE_STATES or not isinstance(raw, str):
            continue
        try:
            fields[field] = float(raw)
        except (TypeError, ValueError):
            continue
    return assemble_payload(inverter_id=inverter_id, fields=fields)
```
(Note: the harvest path passes only non-None decoded fields; `engine.py` filters `None` before calling `assemble_payload` — see Task 9.)

- [ ] **Step 4: Run new test + existing — expect PASS.**

Run: `python -m pytest tests/test_assemble_payload.py tests/test_readings_publisher.py -v`

- [ ] **Step 5: Commit**

```bash
git add custom_components/svitgrid/readings_publisher.py tests/test_assemble_payload.py
git commit -m "refactor(readings): extract assemble_payload(fields) for harvest reuse (SP-B)"
```

---

## Task 8: `api_client.get_register_spec` + version cache (`spec_cache.py`)

**Files:**
- Modify: `custom_components/svitgrid/api_client.py`
- Create: `custom_components/svitgrid/harvest/spec_cache.py`
- Test: `tests/test_get_register_spec.py`, `tests/harvest/test_spec_cache.py`

**Interfaces:**
- Produces:
  - `SvitgridApiClient.get_register_spec(self, model_id: str) -> dict | None` — `GET {base}/api/v1/register-specs/{model_id}`; parsed JSON on 200, else None (mirror `get_preset`).
  - `spec_cache.should_refresh(fetched_version, cached_version) -> bool` (reuse `preset_refresh.should_merge` semantics) and `async def load_spec(fetch, model_id, cached) -> tuple[dict|None, bool]` — returns `(spec_dict, changed)`; fail-open (on exception/None keep `cached`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_get_register_spec.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from custom_components.svitgrid.api_client import SvitgridApiClient

def _mock(status, body):
    session = MagicMock(); resp = MagicMock()
    resp.status = status; resp.json = AsyncMock(return_value=body)
    resp.__aenter__.return_value = resp; resp.__aexit__.return_value = None
    session.get = MagicMock(return_value=resp)
    return session

@pytest.mark.asyncio
async def test_get_register_spec_200_returns_body():
    session = _mock(200, {"modelId": "deye_sg04lp3", "version": 1})
    client = SvitgridApiClient(session, api_base="https://api.example")
    spec = await client.get_register_spec("deye_sg04lp3")
    assert spec["modelId"] == "deye_sg04lp3"
    session.get.assert_called_once()
    assert "/api/v1/register-specs/deye_sg04lp3" in session.get.call_args[0][0]

@pytest.mark.asyncio
async def test_get_register_spec_404_returns_none():
    client = SvitgridApiClient(_mock(404, {}), api_base="https://api.example")
    assert await client.get_register_spec("nope") is None
```
```python
# tests/harvest/test_spec_cache.py
import pytest
from custom_components.svitgrid.harvest.spec_cache import load_spec

@pytest.mark.asyncio
async def test_first_load_changes():
    async def fetch(_m): return {"modelId": "m", "version": 2}
    spec, changed = await load_spec(fetch, "m", cached=None)
    assert changed and spec["version"] == 2

@pytest.mark.asyncio
async def test_same_version_no_change_keeps_cached():
    cached = {"modelId": "m", "version": 2}
    async def fetch(_m): return {"modelId": "m", "version": 2}
    spec, changed = await load_spec(fetch, "m", cached=cached)
    assert not changed and spec is cached

@pytest.mark.asyncio
async def test_fetch_failure_keeps_cached():
    cached = {"modelId": "m", "version": 1}
    async def fetch(_m): raise RuntimeError("net")
    spec, changed = await load_spec(fetch, "m", cached=cached)
    assert not changed and spec is cached
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement.** Add `get_register_spec` to `api_client.py` (mirror `get_preset` exactly, URL `/api/v1/register-specs/{model_id}`). Implement `spec_cache.py`:
```python
# custom_components/svitgrid/harvest/spec_cache.py
"""Version-gated register-spec cache (mirrors preset_refresh)."""
from __future__ import annotations
from ..preset_refresh import should_merge  # reuse numeric/str version compare


async def load_spec(fetch, model_id: str, cached: dict | None) -> tuple[dict | None, bool]:
    """Fetch the spec; return (spec, changed). Fail-open: keep `cached` on
    error/None. `fetch` is an async callable (model_id) -> dict | None."""
    try:
        fetched = await fetch(model_id)
    except Exception:  # noqa: BLE001  fail-open
        return cached, False
    if not fetched:
        return cached, False
    cached_version = cached.get("version") if cached else None
    if cached is None or should_merge(fetched.get("version"), cached_version):
        return fetched, True
    return cached, False
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit**

```bash
git add custom_components/svitgrid/api_client.py custom_components/svitgrid/harvest/spec_cache.py \
        tests/test_get_register_spec.py tests/harvest/test_spec_cache.py
git commit -m "feat(harvest): get_register_spec + version-gated spec cache (SP-B)"
```

---

## Task 9: Engine (`engine.py` — `poll_once` + `run_direct_harvest_loop`)

**Files:**
- Create: `custom_components/svitgrid/harvest/engine.py`
- Test: `tests/harvest/test_engine.py`

**Interfaces:**
- Consumes: `read_raw` (T6), `decode`+`sanitize` (T2-3), `RegisterSpec` (T1), `assemble_payload` (T7), `gate_payload` (existing in `readings_publisher`), `store.append`, `Cadence`, `LifecycleState`.
- Produces:
  - `async def poll_once(*, hass, spec, cfg, inverter_id, store) -> bool` — `raw = await read_raw(...)`; `fields = sanitize(decode(spec, raw), spec)`; drop `None` values; `payload = assemble_payload(inverter_id=inverter_id, fields=non_none_fields)`; `payload, missing = gate_payload(payload)`; if `missing` → log + return False; else `await store.append(payload)` → return True.
  - `async def run_direct_harvest_loop(*, hass, store, cadence, inverter_id, cfg, spec_holder, lifecycle=None, activity=None) -> None` — single-snapshot-per-tick loop mirroring `readings_publisher.run_loop`'s lifecycle/cadence/exception structure (NO idle sub-sampling — see spec §3.4). `spec_holder` is an object with `.spec` (refreshed by the cache); the loop skips a tick (logs) when `.spec is None`.

- [ ] **Step 1: Write the failing test** (poll_once with stubbed transport/store)

```python
# tests/harvest/test_engine.py
import pytest
from unittest.mock import AsyncMock
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest import engine as eng

SPEC = RegisterSpec.from_dict({
    "modelId": "deye_sg04lp3", "version": 1, "protocol": "solarman_v5", "port": 8899,
    "defaultSlaveId": 1, "flags": {"batteryPositiveIsDischarge": True},
    "reads": [
        {"field": "batterySoc", "address": 588},
        {"field": "batteryPower", "address": 590, "signed": True},
        {"field": "batteryVoltage", "address": 587, "scale": 0.01},
        {"field": "gridPower", "address": 625, "signed": True},
        {"field": "loadPower", "address": 653},
        {"field": "pv1Power", "address": 672}, {"field": "pv2Power", "address": 673},
    ],
    "derivations": [
        {"field": "batteryPower", "op": "builtin", "builtin": "battery_sign_normalize",
         "inputs": ["batteryPower"]},
        {"field": "totalPvPower", "op": "sum", "inputs": ["pv1Power", "pv2Power"]},
    ],
    "writes": [],
})

@pytest.mark.asyncio
async def test_poll_once_appends_payload(hass, monkeypatch):
    raw = {1: {588: 78, 590: 1500, 587: 5230, 625: 64536, 653: 1800, 672: 1500, 673: 800}}
    monkeypatch.setattr(eng, "read_raw", AsyncMock(return_value=raw))
    store = type("S", (), {"append": AsyncMock()})()
    ok = await eng.poll_once(hass=hass, spec=SPEC, cfg={"ip": "x", "logger_serial": "1"},
                             inverter_id="inv-1", store=store)
    assert ok is True
    store.append.assert_awaited_once()
    payload = store.append.await_args[0][0]
    assert payload["batterySoc"] == 78.0
    assert payload["batteryPower"] == -1500.0   # sign-normalized
    assert payload["gridPower"] == -1000.0
    assert payload["pvPower"] == 2300.0
    assert payload["pvPower1"] == 1500.0 and payload["pvPower2"] == 800.0

@pytest.mark.asyncio
async def test_poll_once_gated_when_required_missing(hass, monkeypatch):
    # only batterySoc present → CORE_PAYLOAD_FIELDS missing → gated, not appended
    monkeypatch.setattr(eng, "read_raw", AsyncMock(return_value={1: {588: 50}}))
    store = type("S", (), {"append": AsyncMock()})()
    ok = await eng.poll_once(hass=hass, spec=SPEC, cfg={"ip": "x", "logger_serial": "1"},
                             inverter_id="inv-1", store=store)
    assert ok is False
    store.append.assert_not_awaited()
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement `engine.py`**

```python
# custom_components/svitgrid/harvest/engine.py
"""Direct-harvest engine: poll the inverter, decode, append to the store.
Reuses the existing store/cadence/sender/gate pipeline."""
from __future__ import annotations

import asyncio
import logging

from ..readings_publisher import assemble_payload, gate_payload, _clamp_interval, _DEFAULT_INTERVAL_S
from .decoder import decode, sanitize
from .transport import read_raw

_LOGGER = logging.getLogger(__name__)


async def poll_once(*, hass, spec, cfg, inverter_id, store) -> bool:
    raw = await read_raw(hass, spec, cfg)
    fields = sanitize(decode(spec, raw), spec)
    non_none = {k: v for k, v in fields.items() if v is not None}
    payload = assemble_payload(inverter_id=inverter_id, fields=non_none)
    payload, missing = gate_payload(payload)
    if missing:
        _LOGGER.debug("harvest %s gated: missing %s", inverter_id, missing)
        return False
    await store.append(payload)
    return True


async def run_direct_harvest_loop(*, hass, store, cadence, inverter_id, cfg,
                                  spec_holder, lifecycle=None, activity=None) -> None:
    """Single-snapshot-per-tick harvest loop (mirrors readings_publisher.run_loop
    lifecycle/cadence/fail-soft; no idle sub-sampling — spec §3.4)."""
    while not hass.is_stopping and (lifecycle is None or lifecycle.active):
        next_sleep_s = _clamp_interval(float(cadence.interval_s))
        try:
            spec = getattr(spec_holder, "spec", None)
            if spec is None:
                _LOGGER.debug("harvest %s: no spec yet, skipping tick", inverter_id)
            else:
                ok = await poll_once(hass=hass, spec=spec, cfg=cfg,
                                     inverter_id=inverter_id, store=store)
                if ok and activity is not None:
                    activity.record_ingest_success()
        except Exception as exc:  # noqa: BLE001  fail-soft
            _LOGGER.exception("harvest %s failed; retry next tick", inverter_id)
            if activity is not None:
                activity.record_ingest_failure(reason=str(exc) or type(exc).__name__)
            next_sleep_s = float(_DEFAULT_INTERVAL_S)
        await asyncio.sleep(next_sleep_s)
    _LOGGER.info("harvest %s stopped", inverter_id)
```
(Confirm `gate_payload`, `_clamp_interval`, `_DEFAULT_INTERVAL_S`, and `activity.record_ingest_success()`'s real signature in `readings_publisher.py`; adjust the import/call to the actual names. If `record_ingest_success` requires args, pass the same ones the entity loop passes.)

- [ ] **Step 4: Run — expect PASS.** (Add a loop test that runs one iteration with a fake `hass.is_stopping` flipping True after one tick, asserting `poll_once` was called.)

- [ ] **Step 5: Commit**

```bash
git add custom_components/svitgrid/harvest/engine.py tests/harvest/test_engine.py
git commit -m "feat(harvest): engine poll_once + direct-harvest loop (SP-B)"
```

---

## Task 10: Config-flow `harvest_config` step

**Files:**
- Modify: `custom_components/svitgrid/config_flow.py`
- Test: `tests/test_config_flow_harvest.py`

**Interfaces:**
- Produces: `async_step_harvest_config(self, user_input=None)` collecting `protocol` (select `solarman_v5`/`modbus_tcp`), `ip`, `port` (default 8899), `slave_id` (default 1), `model_id`, `logger_serial` (optional). On submit, stores a `harvest_config` dict (snake_case keys: `protocol, ip, port, slave_id, model_id, logger_serial`) onto the inverter entry being built and proceeds to finalize. Validation: `ip` required; `logger_serial` required when `protocol == "solarman_v5"`.

- [ ] **Step 1: Write the failing test** (config-flow steps use the `hass` fixture + `flow = ConfigFlow(); flow.hass = hass`; assert the produced `harvest_config`). Mirror existing config-flow tests in `tests/` for the exact harness shape.

```python
# tests/test_config_flow_harvest.py
import pytest
from custom_components.svitgrid.config_flow import SvitgridConfigFlow  # confirm class name

@pytest.mark.asyncio
async def test_harvest_config_builds_dict(hass):
    flow = SvitgridConfigFlow(); flow.hass = hass
    result = await flow.async_step_harvest_config({
        "protocol": "solarman_v5", "ip": "192.168.1.50", "port": 8899,
        "slave_id": 1, "model_id": "deye_sg04lp3", "logger_serial": "1234567890",
    })
    cfg = flow._harvest_config  # confirm the attribute the step stores onto
    assert cfg == {
        "protocol": "solarman_v5", "ip": "192.168.1.50", "port": 8899,
        "slave_id": 1, "model_id": "deye_sg04lp3", "logger_serial": "1234567890",
    }

@pytest.mark.asyncio
async def test_harvest_config_shows_form_without_input(hass):
    flow = SvitgridConfigFlow(); flow.hass = hass
    result = await flow.async_step_harvest_config(None)
    assert result["type"] == "form"
    assert result["step_id"] == "harvest_config"

@pytest.mark.asyncio
async def test_solarman_requires_logger_serial(hass):
    flow = SvitgridConfigFlow(); flow.hass = hass
    result = await flow.async_step_harvest_config({
        "protocol": "solarman_v5", "ip": "192.168.1.50", "port": 8899,
        "slave_id": 1, "model_id": "deye_sg04lp3",  # no logger_serial
    })
    assert result["type"] == "form" and result.get("errors")
```

- [ ] **Step 2-5:** Run (FAIL) → implement `async_step_harvest_config` (mirror `async_step_manual_meta`'s `vol.Schema`, store `self._harvest_config`, validate logger serial for solarman, route to the existing finalize step that writes `entry.data["inverters"]` — add `harvest_config` into the inverter dict alongside `entity_map`) → run (PASS) → commit `feat(harvest): config-flow step to enter harvest_config (SP-B)`.

> Confirm the real flow-class name + the finalize step + how the inverter dict is assembled (config_flow.py lines ~237-263) and thread `harvest_config` into it.

---

## Task 11: Entry wiring — spawn the harvest loop

**Files:**
- Modify: `custom_components/svitgrid/__init__.py`
- Test: `tests/test_init_harvest_wiring.py`

**Interfaces:**
- Consumes: `run_direct_harvest_loop` (T9), `load_spec`+`get_register_spec` (T8), the existing per-inverter setup loop (`__init__.py` ~lines 497-508), `store`, `cadence`, `lifecycle`, `activity`.
- Behaviour: for each inverter in `entry.data["inverters"]`, if it has a `harvest_config`, build a small `spec_holder` (load the spec once via `load_spec(client.get_register_spec, cfg["model_id"], cached=None)` and on a periodic refresh), and spawn `run_direct_harvest_loop` as a background task (stored in `readings_tasks[inverter_id]` so the existing shutdown cancels it). Inverters WITHOUT `harvest_config` keep the existing `run_readings_loop`. An inverter has at most one loop.

- [ ] **Step 1: Write the failing test** — set up an entry with one inverter carrying `harvest_config`, run `async_setup_entry`, assert a background task named `svitgrid_harvest_<id>` exists and the entity loop was NOT spawned for it. (Use the repo's existing `async_setup_entry` test harness as the template — confirm how other `__init__` tests construct the entry + mock the api_client.)

- [ ] **Step 2-5:** Run (FAIL) → implement the branch in the per-inverter setup (spawn harvest vs entity loop based on `inv.get("harvest_config")`; load the initial spec into a `spec_holder`; ensure the task is registered in `readings_tasks` for shutdown) → run (PASS) + run the FULL suite `python -m pytest tests/ -q` → commit `feat(harvest): spawn direct-harvest loop when harvest_config present (SP-B)`.

> Keep the spec-refresh simple: load once at setup; a periodic re-fetch can reuse the existing rollup timer or the preset-refresh cadence — wire `load_spec` into whatever refresh hook the relay path already uses, or do a fetch at loop start. Don't over-build; SP-D revisits config/refresh.

---

## Task 12: Full verification

**Files:** none.

- [ ] **Step 1: HA repo suite** — `python -m pytest tests/ -q` (all pass, incl. the golden-vector contract + existing relay tests unchanged). `ruff check custom_components/svitgrid/harvest tests/harvest` clean.
- [ ] **Step 2: Golden-vector freshness** — re-run the monorepo tool (`dart run tool/export_golden_vectors.dart` in the Task-4 worktree), re-vendor (`scripts/sync-golden-vectors.sh`), confirm `git status` shows no change to `tests/fixtures/golden-vectors.json` (vendored copy current).
- [ ] **Step 3: Manifest** — `pysolarmanv5` + `pymodbus` present in `manifest.json` requirements + `requirements.txt`; the add-on imports them lazily (no import at module top) so test collection doesn't require them installed unless transport tests run.
- [ ] **Step 4: Report** — models covered by golden vectors, any reader clamp deliberately not reproduced (batteryVoltage HV/LV, Huawei pvPower≥0) + why, and the UNVERIFIED-addresses posture carried from SP-A.

---

## Self-Review (completed by plan author)

**Spec coverage:** §3.1 register_spec→T1; §3.2 decoder+sanitize→T2/T3; §3.3 transport→T6; §3.4 engine/loop→T9; §3.5 payload refactor→T7; §4 golden vectors→T4(gen)/T5(vendor+contract); §5 config+fetch→T10/T8; §6 error handling→T9 (fail-soft) + T8 (fail-open); §7 testing→every task is TDD; §9 deliverables→T1-T12. ✓

**Placeholder scan:** the integration tasks (T10/T11) carry "confirm the real class/attr/signature" notes rather than guessed names — this is deliberate (the explore couldn't see the exact config-flow class name / activity signature); the mechanism + exact schema/keys are specified, only local names are to be confirmed against the file. Not an unspecified placeholder.

**Type consistency:** `RawRegisters = dict[int, dict[int,int]]`, `RegisterSpec`/`ReadDef`/`Derivation`/`SpecFlags`, `decode`/`sanitize`, `assemble_payload(inverter_id, fields)`, `read_raw(hass, spec, cfg)`, `plan_ranges`, `poll_once`/`run_direct_harvest_loop`, `load_spec`, `get_register_spec` — names consistent across tasks. Endpoint `/api/v1/register-specs/:modelId` used consistently.

**Known risks carried forward:** (1) `pysolarmanv5`/`pymodbus` exact read-method signatures must be confirmed against the installed versions (T6 note). (2) the config-flow/`__init__` integration (T10/T11) names must be confirmed against the real files. (3) golden vectors prove Python==Dart, not address correctness — every non-`deye_sg04lp3` model stays `verified:false`.
