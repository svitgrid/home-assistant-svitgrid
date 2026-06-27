# HA Direct Harvester — SP-C: Native Modbus Write Executor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Svitgrid HA add-on write inverter control registers directly (work-mode, gen-force, solar-sell, grid-charge toggle, gen-port mode, sell-power-cap, battery-charge/TOU) over Modbus, driven by the register-spec's `writes`, with write→verify-read-back, pinned to the real Dart writers by write golden vectors.

**Architecture:** Monorepo: extend the Dart write-spec format, a reference write-applier (executable meaning of the write-spec) validated against the real Dart writers, authored `writes` per writable model, and a write golden-vector tool. HA add-on: parse the command-specs, a pure `compute_register_writes` (Python port of the applier) pinned by the vendored vectors, a transport write path (write + bit-RMW), and a `WriteExecutor(BaseExecutor)` that computes → writes → verifies → ACKs, wired in for direct-harvest inverters.

**Tech Stack:** Dart (monorepo `inverter_protocol`), Python 3.11+ HA custom component, `pysolarmanv5`/`pymodbus`, `pytest`/`pytest-asyncio`/`pytest-homeassistant-custom-component`.

## Global Constraints

- **TDD always** — failing test first, watch it fail, implement, watch it pass, commit.
- **Two repos.** Tasks 1-4 are in the `svitgrid` monorepo (its own worktree/branch `feat/ha-write-golden-vectors`). Tasks 5-11 are in the HA repo (worktree `.worktrees/ha-direct-harvester-sp-c`, branch `feat/ha-direct-harvester-sp-c`).
- **The golden vectors / real Dart writers are GROUND TRUTH.** A contract mismatch means the Python (or the authored spec) is wrong — fix it, never the vectors.
- **Seven commands**: `set_work_mode, set_gen_force, set_solar_sell, set_grid_charge_toggle, set_gen_port_mode, set_sell_power_cap, set_battery_charge`. SP-C adds the new ones to `const.DISPATCHABLE_COMMANDS` (currently `{set_battery_charge, set_work_mode, set_solar_sell, set_grid_charge_toggle}`).
- **Addresses/encodings (3-phase / 1-phase)** — from the Dart writers (the export tool authors from these; verify against them):
  - work_mode: 142/244 full_word, `workMode` 0-2.
  - gen_force: 132 full_word (3ph) / 326 `bit:13` RMW (1ph), payload `on`, on=1/off=0.
  - solar_sell: 145/247 `bit:0`, payload `solarSell` 0/1.
  - grid_charge_toggle: **146/248** `bit:0` (preserve bits 1-7; read MUST succeed), payload `gridChargeEnabled`.
  - gen_port_mode: 133/235 full_word, `genPortMode` 0-2.
  - sell_power_cap: 143/245 full_word, `sellPowerCapW`, clamp to `ratedPowerW` (default 15000).
  - battery_charge/TOU: 6 slots, stride 1. 3ph bases time 148 / power 154 / voltage 160 / soc 166 / enable 172; 1ph bases 250 / 256 / 262 / 268 / 274. Reg = `base + slotIndex`. End = next slot's start (`base_time + ((slotIndex+1) mod 6)`). Values RAW (valueScale 1). Enable = `(prev & ~0x03) | (enabled?1:0)`.
- **Capability gate**: only models in `configRegisterBatchForModel` (Dart) get `writes` (3-phase set + 1-phase set listed below); Victron/Solplanet/Huawei/grid-tie/micro get `writes: []`.
- **`encoding: bit:N`** = read-modify-write: read the word, set/clear bit N (optionally clearing `clearMask` first), preserve other bits, write back. A failed prior-read → the command rejects (never write a half-known word).
- **Verify-read-back**: after writing, read the written registers back; confirm each holds the intended value (bit fields: the bit matches). Mismatch → raise (poller ACKs `rejected`).
- **Executor contract**: `dispatch(command_name, payload) -> dict` returns the ACK `result`; raises on failure (poller ACKs `rejected/reason`); `NotImplementedError` → `unsupported`. Signature is already verified upstream — do NOT re-verify.
- **pymodbus 3.x** uses `device_id=` (not `slave=`); pin already `>=3.7`. pysolarmanv5 `write_holding_registers(register_addr, values)`.
- Worktree deps: HA — use `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/python -m pytest`; monorepo — `dart test` in `packages/inverter_protocol`.

Writable model sets (from `config_register_batch.dart`):
- 3-phase: `deye_sg04lp3, deye_sg01hp3, deye_sg01hp3_50k, deye_sg01hp3_30k, deye_sg02hp3_80k, deye_sg05lp3, deye_gb_s20k, sunsynk_3phase, sunsynk_3phase_15k`.
- 1-phase: `deye_sg03lp1, deye_sg01lp1, deye_sg02lp1, deye_sun_g3, deye_sg04lp1, deye_sg05lp1, deye_sg01lp1_16k, sunsynk_1phase, sunsynk_1phase_12k, sunsynk_1phase_16k, deye_sg01lp1_us, solark_5k, solark_12k, solark_15k, solark_18k`.

---

## File Structure

**Monorepo (`packages/inverter_protocol/`):**
- `lib/src/spec/register_spec.dart` — extend `WriteDef` → `WriteCommand`/`FieldWrite`/`SlotSpec`.
- `lib/src/spec/write_reference.dart` — `applyWrites(...)` reference applier (NEW).
- `tool/export_register_specs.dart` — author `writes` per writable model (the `writes: const []` site).
- `tool/export_write_golden_vectors.dart` (NEW) + `write-golden-vectors.json` + `test/spec/write_golden_vectors_staleness_test.dart`.
- `test/spec/write_reference_test.dart` (unit) + `test/spec/write_applier_vs_writers_test.dart` (applier == real writers).

**HA repo (`custom_components/svitgrid/harvest/`):**
- `register_spec.py` — add `WriteCommand`/`FieldWrite`/`SlotSpec` + parse `writes`.
- `write_compute.py` — `compute_register_writes(command_spec, payload, prior) -> list[(unitId,address,value)]` (NEW).
- `write_executor.py` — `WriteExecutor(BaseExecutor)` (NEW).
- `transport.py` — add `write_registers` + `read_word`/`read_modify_write_bit`.
**HA repo (other):**
- `const.py` — extend `DISPATCHABLE_COMMANDS`.
- `__init__.py` — wire `WriteExecutor` for harvest inverters.
- `scripts/sync-golden-vectors.sh` — also vendor `write-golden-vectors.json`.
- `tests/fixtures/write-golden-vectors.json` (vendored) + `tests/harvest/test_write_*.py`.

---

## Task 1: Dart write-spec format (`WriteCommand`/`FieldWrite`/`SlotSpec`)

**Repo:** monorepo. Worktree: `cd ~/git/svitgrid && git worktree add .worktrees/ha-write-golden-vectors -b feat/ha-write-golden-vectors && cd .worktrees/ha-write-golden-vectors/packages/inverter_protocol && dart pub get`.

**Files:** Modify `lib/src/spec/register_spec.dart`; Test `test/spec/write_spec_test.dart`.

**Interfaces — Produces:**
- `class FieldWrite { final String payloadField; final int? address; final int? base; final String encoding; final double valueScale; final int? onValue; final int? offValue; final int? clearMask; final bool viaNextSlot; final Map<String,num>? limits; }` (`address` for simple, `base` for slot fields).
- `class SlotSpec { final String indexField; final int count; final int stride; final bool endViaNextSlotStart; final List<FieldWrite> fields; }`
- `class WriteCommand { final String command; final List<FieldWrite> fields; final SlotSpec? slot; }` + `fromJson`/`toJson`.
- `RegisterSpec.writes` becomes `List<WriteCommand>` (was `List<WriteDef>`); `fromJson` parses `d['writes']`; `toJson` emits it. Keep field order stable so the SP-A staleness gate produces deterministic JSON.

- [ ] **Step 1: failing test** — parse a simple command (full_word + bit:N) and a slot command from JSON; round-trip toJson; `validate()` rejects an unknown encoding + a slot command missing `indexField`.

```dart
// test/spec/write_spec_test.dart (sketch — implementer writes full cases)
import 'package:test/test.dart';
import 'package:inverter_protocol/src/spec/register_spec.dart';
void main() {
  test('parses a simple full_word command', () {
    final c = WriteCommand.fromJson({'command': 'set_work_mode', 'fields': [
      {'payloadField': 'workMode', 'address': 142, 'limits': {'min': 0, 'max': 2}}]});
    expect(c.command, 'set_work_mode');
    expect(c.fields.single.address, 142);
    expect(c.fields.single.encoding, 'full_word');
  });
  test('parses a bit:N RMW field with clearMask', () {
    final f = FieldWrite.fromJson({'payloadField': 'gridChargeEnabled', 'base': 172,
      'encoding': 'bit:0', 'clearMask': 3, 'onValue': 1, 'offValue': 0});
    expect(f.encoding, 'bit:0'); expect(f.clearMask, 3); expect(f.base, 172);
  });
  test('parses a slot command', () {
    final c = WriteCommand.fromJson({'command': 'set_battery_charge', 'slot': {
      'indexField': 'slotIndex', 'count': 6, 'stride': 1, 'endViaNextSlotStart': true,
      'fields': [{'payloadField': 'slotStart', 'base': 148}]}});
    expect(c.slot!.indexField, 'slotIndex'); expect(c.slot!.count, 6);
  });
}
```

- [ ] **Step 2: run → FAIL.** `dart test test/spec/write_spec_test.dart`
- [ ] **Step 3: implement** the dataclasses + `fromJson`/`toJson` in `register_spec.dart`; change `RegisterSpec.writes` type + parse/emit. Extend `RegisterSpec.validate()` (the read-side validator) with write checks: each field has a `payloadField`; `encoding` is `full_word` or matches `^bit:\d+$`; a `slot` command has `indexField`+`count>=1`+non-empty `fields`.
- [ ] **Step 4: run → PASS**, then full `dart test` (existing reads/specs unaffected — `WriteDef` removal must not break the export tool, which currently sets `writes: const []`; update that site to `writes: const <WriteCommand>[]`).
- [ ] **Step 5: commit** `feat(spec): write-command format (fields + slot block + bit/clearMask) (HA SP-C)`.

---

## Task 2: Dart reference write-applier (`write_reference.dart`)

**Repo:** monorepo. **Files:** Create `lib/src/spec/write_reference.dart`; export it from `lib/inverter_protocol.dart`; Test `test/spec/write_reference_test.dart`.

**Interfaces — Produces:**
- `class RegisterWrite { final int unitId; final int address; final int value; }` (+ `==`/`hashCode`/`toJson` for comparison).
- `List<RegisterWrite> applyWrites(WriteCommand cmd, Map<String,dynamic> payload, Map<int,int> prior, {int unitId = 1})` — the executable meaning:
  - simple `full_word`: `value = onValue/offValue` (boolean/0-1 payload) else `(payload[field] / valueScale).round()`, clamped to `limits`.
  - `bit:N`: `base = prior[address] ?? 0`; if `clearMask != null` start from `base & ~clearMask`; set bit N when truthy (`on`/`onValue`/`solarSell==1`/`gridChargeEnabled`), else clear bit N; preserve other bits → one `RegisterWrite`.
  - slot: `idx = payload[indexField]`; each field's address = `base + idx*stride`; `viaNextSlot` field → address `base + ((idx+1) mod count)*stride`; values raw (valueScale 1) unless the field sets scale; enable field uses `clearMask`.
  - Returns the writes in a deterministic order (declaration order; slot: the field order as listed).

- [ ] **Step 1: failing unit tests** — one per encoding/case: full_word with limits clamp; boolean on/off; `bit:0` set+clear preserving other bits (seed `prior`); `bit:13` RMW; `clearMask:0x03` enable; slot field address = base+idx; `viaNextSlot` end = base+((idx+1)%6); wrap at slot 5 → slot 0's start.

- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement `applyWrites`** per the constraints. Pure (no I/O); `prior` supplies the RMW base words.
- [ ] **Step 4: run → PASS** + full `dart test`.
- [ ] **Step 5: commit** `feat(spec): reference write-applier (executable meaning of write-spec) (HA SP-C)`.

---

## Task 3: Author `writes` in the export tool + validate against the real writers

**Repo:** monorepo. **Files:** Modify `tool/export_register_specs.dart`; Create `test/spec/write_applier_vs_writers_test.dart`; regenerate `register-specs/*.json`.

**Interfaces — Consumes:** the writers' register data (`config_register_batch.dart` model sets; `battery_charge_executor.dart`/`work_mode_executor.dart`/`gen_force_executor.dart`/`solar_sell_executor.dart`/`grid_charge_toggle_executor.dart`/`gen_port_mode_executor.dart`/`sell_power_cap_executor.dart` addresses); `applyWrites` (Task 2); the real writers' compute.

- [ ] **Step 1: failing test — reference applier == real writers** (the data-correctness gate). For each writable model + command + a representative payload + seeded prior: run the REAL Dart writer's compute (extract or invoke it against a mock client that records `(address,value)` writes) and assert `applyWrites(authoredCommand, payload, prior)` produces the identical `RegisterWrite` list. Covers: gen_force 3ph full + 1ph bit:13; work_mode; solar_sell bit:0; grid_charge_toggle 146 bit:0 preserve; gen_port_mode; sell_power_cap clamp; battery_charge slot incl. end-via-next-slot + enable clearMask.

> The real writers do I/O (read prior, write, verify, retry). To get their pure register-write decisions: either (a) extract a pure `computeWrites(payload, prior)` from each writer and call it, OR (b) drive each writer with a fake client that returns `prior` on reads and records writes, then compare the recorded writes. Prefer (b) if extraction is invasive — it captures the real writer verbatim. Document which you used.

- [ ] **Step 2: run → FAIL** (no `writes` authored yet).
- [ ] **Step 3: author `writes`** in `buildSpecsForModels`: for a model in the 3-phase or 1-phase writable set, build the 7 `WriteCommand`s with the addresses from the Global Constraints (3ph vs 1ph base set); non-writable models keep `writes: const []`. Then iterate to make Step-1's applier==writers test pass (fix authored addresses/encoding until the real writers agree). Re-run `dart run tool/export_register_specs.dart` so committed JSON reflects the authored writes; the SP-A staleness gate now covers them.
- [ ] **Step 4: run → PASS** + full `dart test` + `git status` shows the regenerated `register-specs/*.json`.
- [ ] **Step 5: commit** `feat(spec): author write-commands per model + applier==writers contract (HA SP-C)`.

---

## Task 4: Write golden-vector tool + staleness gate

**Repo:** monorepo. **Files:** Create `tool/export_write_golden_vectors.dart`, `write-golden-vectors.json`, `test/spec/write_golden_vectors_staleness_test.dart`.

**Interfaces — Produces:** `String buildWriteGoldenVectorsJson()` → `{sourceCommit, vectors:[{modelId, command, payload, priorRegisters, expectedRegisterWrites}]}` (pretty + trailing newline), generated by running `applyWrites` over a curated fixture list (a representative `{model, command, payload, prior}` per command × at least one 3ph and one 1ph model, incl. the bit-RMW/clearMask/TOU-end cases). Mirror `export_golden_vectors.dart` + `golden_vectors_staleness_test.dart` (compare `vectors` content, not the `sourceCommit` header).

- [ ] **Step 1-5:** staleness test first (RED) → implement tool + the fixture list → `dart run` to generate → tests GREEN + full `dart test` → commit `feat(spec): write golden-vector tool + staleness gate (HA SP-C)`.

> This monorepo branch (`feat/ha-write-golden-vectors`) merges to monorepo main when SP-C lands; the HA repo (Task 7) vendors `write-golden-vectors.json` from it.

---

## Task 5: Python write-spec parsing (`register_spec.py`)

**Repo:** HA. **Files:** Modify `custom_components/svitgrid/harvest/register_spec.py`; Test `tests/harvest/test_write_spec.py`.

**Interfaces — Produces:** `@dataclass(frozen=True) FieldWrite(payload_field, address=None, base=None, encoding="full_word", value_scale=1.0, on_value=None, off_value=None, clear_mask=None, via_next_slot=False, limits=None)`; `SlotSpec(index_field, count, stride, end_via_next_slot, fields)`; `WriteCommand(command, fields, slot=None)` with `from_dict`. `RegisterSpec.writes: tuple[WriteCommand,...]` parsed in `from_dict` (camelCase keys: `payloadField, valueScale, onValue, offValue, clearMask, viaNextSlot, indexField, endViaNextSlotStart`). `validate()` adds the write checks (mirror Task 1).

- [ ] **Step 1: failing test** (parse simple + bit:N + slot from the camelCase JSON; `writes` tuple populated; validate rejects bad encoding/slot). `RegisterSpec.from_dict({... "writes":[...]})`.
- [ ] **Step 2-4:** RED → implement the dataclasses + parse `d.get("writes", [])` → GREEN; run `tests/harvest/test_register_spec.py` (unchanged-pass) + ruff.
- [ ] **Step 5: commit** `feat(harvest): parse write-command specs (SP-C)`.

---

## Task 6: Python `compute_register_writes` (port of the applier)

**Repo:** HA. **Files:** Create `custom_components/svitgrid/harvest/write_compute.py`; Test `tests/harvest/test_write_compute.py`.

**Interfaces — Produces:** `compute_register_writes(cmd: WriteCommand, payload: dict, prior: dict[int,int], unit_id: int = 1) -> list[tuple[int,int,int]]` (list of `(unit_id, address, value)`) — the exact Python port of Task 2's `applyWrites`. Pure (no I/O); `prior` supplies RMW base words. Raise `ValueError` for an out-of-range slot index or a missing required payload field.

```python
# custom_components/svitgrid/harvest/write_compute.py  (core sketch — implementer completes per the applier)
from __future__ import annotations
from .register_spec import WriteCommand, FieldWrite

def _encode_value(f: FieldWrite, payload: dict, prior: dict[int, int], address: int):
    if f.encoding.startswith("bit:"):
        bit = int(f.encoding.split(":", 1)[1])
        base = prior.get(address, 0)
        if f.clear_mask is not None:
            base &= ~f.clear_mask
        truthy = _is_on(payload[f.payload_field], f)
        return (base | (1 << bit)) if truthy else (base & ~(1 << bit))
    # full_word
    raw = payload[f.payload_field]
    if f.on_value is not None and isinstance(raw, bool):
        v = f.on_value if raw else f.off_value
    else:
        v = round(float(raw) / f.value_scale)
    if f.limits:
        lo, hi = f.limits.get("min"), f.limits.get("max")
        if lo is not None: v = max(int(lo), v)
        if hi is not None: v = min(int(hi), v)
    return v

def _is_on(raw, f: FieldWrite) -> bool:
    if isinstance(raw, bool): return raw
    return int(raw) == (f.on_value if f.on_value is not None else 1)

def compute_register_writes(cmd, payload, prior, unit_id=1):
    out: list[tuple[int,int,int]] = []
    if cmd.slot is not None:
        s = cmd.slot
        idx = int(payload[s.index_field])
        if not (0 <= idx < s.count):
            raise ValueError(f"slot index {idx} out of range 0..{s.count-1}")
        for f in s.fields:
            addr = f.base + (((idx + 1) % s.count) if f.via_next_slot else idx) * s.stride
            out.append((unit_id, addr, _encode_value(f, payload, prior, addr)))
        return out
    for f in cmd.fields:
        out.append((unit_id, f.address, _encode_value(f, payload, prior, f.address)))
    return out
```

- [ ] **Step 1: failing tests** — port Task 2's unit cases to Python (full_word+limits, on/off, bit:0 set/clear preserving bits, bit:13, clearMask:0x03, slot base+idx, viaNextSlot end incl. wrap, slot-index-out-of-range raises).
- [ ] **Step 2-4:** RED → implement → GREEN + ruff.
- [ ] **Step 5: commit** `feat(harvest): compute_register_writes (port of reference write-applier) (SP-C)`.

---

## Task 7: Vendored write golden vectors + Python write-contract test

**Repo:** HA. **Files:** Create `tests/fixtures/write-golden-vectors.json` (vendored from Task 4); Modify `scripts/sync-golden-vectors.sh` (vendor both files); Test `tests/harvest/test_write_golden_vectors.py`.

**Interfaces — Consumes:** `compute_register_writes` (T6), `RegisterSpec.from_dict`/the per-model spec.

- [ ] **Step 1: failing contract test** — load `write-golden-vectors.json`; for each vector parse the model's spec (the vector carries `modelId` + `command` + `payload` + `priorRegisters` + `expectedRegisterWrites`; load the model's `WriteCommand` from the committed register-spec OR embed the command spec in the vector — match what Task 4 emits), compute `compute_register_writes(cmd, payload, {int(k):v ...})`, assert `== expectedRegisterWrites` (as `(unitId,address,value)` tuples). Plus a `sourceCommit` present check.

```python
# tests/harvest/test_write_golden_vectors.py (sketch)
import json, pathlib
from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.write_compute import compute_register_writes
V = pathlib.Path(__file__).parent.parent / "fixtures" / "write-golden-vectors.json"

def test_python_writes_match_dart_applier_for_every_vector():
    data = json.loads(V.read_text()); assert data["vectors"]
    for v in data["vectors"]:
        spec = RegisterSpec.from_dict(v["spec"])  # if Task 4 embeds the spec; else load by modelId
        cmd = next(c for c in spec.writes if c.command == v["command"])
        prior = {int(a): w for a, w in v.get("priorRegisters", {}).items()}
        got = compute_register_writes(cmd, v["payload"], prior)
        exp = [tuple(x) for x in v["expectedRegisterWrites"]]  # [[unitId,addr,val],...]
        assert got == exp, f"{v['modelId']}/{v['command']}: {got} != {exp}"
```

- [ ] **Step 2: vendor** — extend `scripts/sync-golden-vectors.sh` to also copy `write-golden-vectors.json` from the monorepo, then run it (source = the Task-4 worktree's committed file). Confirm the vendored file's vector shape matches what the test expects (adjust the test's spec-load to match Task 4's emission — embed `spec` per vector OR load by modelId from `golden-vectors.json`/a committed specs map; pick one in Task 4 and keep consistent).
- [ ] **Step 3: run → PASS** (the moment of truth — Python writes == Dart applier for every vector; on mismatch fix `write_compute.py` / the parse, never the vectors). ruff clean.
- [ ] **Step 4: commit** `test(harvest): vendored write golden vectors + Python write-contract (SP-C)`.

---

## Task 8: Transport write path (`write_registers` + bit-RMW)

**Repo:** HA. **Files:** Modify `custom_components/svitgrid/harvest/transport.py`; Test `tests/harvest/test_transport_write.py`.

**Interfaces — Produces:**
- `async def read_word(hass, spec, cfg, unit_id, address) -> int | None` — read one register (reuse the read clients).
- `async def write_registers(hass, spec, cfg, writes: list[tuple[int,int,int]]) -> None` — group by unit, write each via `hass.async_add_executor_job(_write_solarman|_write_modbus, ...)`. pymodbus: `client.write_registers(address, [value], device_id=unit_id)` (or `write_register`); pysolarmanv5: `sm.write_holding_registers(register_addr=address, values=[value])`. Raise on `isError()`/exception. Lazy imports.

- [ ] **Step 1: failing tests** (monkeypatch stubbed `pymodbus`/`pysolarmanv5`): `write_registers` calls the right write method with `(address, [value], device_id=)`/`(register_addr=, values=)` and raises on an error result; `read_word` returns the word. Mirror SP-B's transport stub-test pattern (`patch.dict(sys.modules, ...)`).
- [ ] **Step 2-4:** RED → implement → GREEN + ruff.
- [ ] **Step 5: commit** `feat(harvest): transport write path (write_registers + read_word) (SP-C)`.

---

## Task 9: `WriteExecutor` (compute → write → verify)

**Repo:** HA. **Files:** Create `custom_components/svitgrid/harvest/write_executor.py`; Test `tests/harvest/test_write_executor.py`.

**Interfaces — Consumes:** `BaseExecutor` (`executors/base.py`), `compute_register_writes` (T6), `read_word`/`write_registers` (T8), `spec_holder.spec` (parsed `RegisterSpec`).
**Produces:** `class WriteExecutor(BaseExecutor)` with `__init__(self, hass, spec_holder, cfg)` and `async def dispatch(self, command_name, payload) -> dict`:
1. `spec = spec_holder.spec`; if None → raise `RuntimeError("spec_not_loaded")`.
2. `cmd = next((c for c in spec.writes if c.command == command_name), None)`; if None → raise `NotImplementedError(command_name)` (poller → `unsupported`).
3. Build `prior` by reading every register the command's `bit:N`/`viaNextSlot` fields depend on (the addresses `compute_register_writes` will index — for bit fields and for the enable clearMask; a failed required read → raise `RuntimeError("prior_read_failed:<addr>")`).
4. `writes = compute_register_writes(cmd, payload, prior, unit_id=spec.default_slave_id)`.
5. `await write_registers(...)`.
6. **Verify**: read back each written address; for a `bit:N` field confirm the bit matches; for full_word confirm the value equals. Mismatch → raise `RuntimeError(f"verify_failed:{addr}")`.
7. return `{"written": [[u,a,v],...], "verified": True}`.

`set_battery_charge` (legacy abstract) routes through `dispatch` — implement the abstract `set_battery_charge` to call `self.dispatch("set_battery_charge", payload)` so the ABC is satisfied.

- [ ] **Step 1: failing tests** (fake transport via monkeypatch/AsyncMock on the module's `read_word`/`write_registers`): happy path (work_mode) → writes + verify → result; gen_force 1-phase bit:13 (prior seeded) → RMW word written + verified; unsupported command → `NotImplementedError`; spec None → raises; verify-mismatch (read-back differs) → raises; prior-read-fail → raises.
- [ ] **Step 2-4:** RED → implement → GREEN + ruff. Run the full `tests/harvest/` dir.
- [ ] **Step 5: commit** `feat(harvest): WriteExecutor — compute, write, verify-read-back (SP-C)`.

---

## Task 10: Wiring — DISPATCHABLE_COMMANDS + native executor

**Repo:** HA. **Files:** Modify `custom_components/svitgrid/const.py`, `custom_components/svitgrid/__init__.py`; Test `tests/test_init_write_wiring.py`.

**Interfaces:**
- `const.DISPATCHABLE_COMMANDS` gains `set_gen_force, set_gen_port_mode, set_sell_power_cap` (the other 4 already present).
- In `async_setup_entry`'s per-inverter loop: when `inv.get("harvest_config")`, set `executors_by_inverter[inverter_id] = WriteExecutor(hass=hass, spec_holder=spec_holder, cfg=harvest_config)` (reuse the SAME `spec_holder` built for the read loop). For non-harvest inverters keep the existing `YamlDispatcher` branch. (An inverter gets exactly one executor.)

- [ ] **Step 1: failing test** — set up an entry with one `harvest_config` inverter; run `async_setup_entry`; assert `executors_by_inverter[id]` is a `WriteExecutor` whose `spec_holder` is the same object passed to `run_direct_harvest_loop`; a relay inverter still gets `YamlDispatcher`. Mirror `tests/test_init_harvest_wiring.py` + `tests/test_setup_entry_multi.py`.
- [ ] **Step 2-4:** RED → implement (const + the wiring branch) → GREEN; run the FULL `tests/` suite (no regression — the relay command path + existing executor wiring unchanged). ruff clean.
- [ ] **Step 5: commit** `feat(harvest): dispatch new write commands + native WriteExecutor for harvest inverters (SP-C)`.

---

## Task 11: Full verification

- [ ] **Step 1: monorepo** — in the Task-1..4 worktree: `cd packages/inverter_protocol && dart test` (write-spec, applier, applier-vs-writers, write-golden staleness, + all SP-A/SP-B specs still green). Re-run `dart run tool/export_register_specs.dart` and `dart run tool/export_write_golden_vectors.dart`; `git status` shows no diff (committed JSON current).
- [ ] **Step 2: HA suite** — `/Users/ivanursul/git/home-assistant-svitgrid/.venv/bin/python -m pytest tests/ -q` all pass (incl. the write-contract + the existing relay/command tests). `ruff check custom_components/svitgrid/harvest custom_components/svitgrid/write_executor.py tests/harvest` clean.
- [ ] **Step 3: vendored freshness** — re-run `scripts/sync-golden-vectors.sh`; `git status` shows no change to `tests/fixtures/write-golden-vectors.json`.
- [ ] **Step 4: report** — commands covered, any model/command deliberately not authored + why, and the UNVERIFIED-addresses posture (verify-read-back is the runtime net; addresses match the fleet writers but are not hardware-verified).

---

## Self-Review (completed by plan author)

**Spec coverage:** §3.1/§3.2 format → T1; §4.1 author writes → T3; §4.2 reference applier + applier-vs-writers + write golden vectors → T2/T3/T4; §5.1 parse → T5; §5.2 transport write → T8; §5.3 compute + executor + verify → T6/T9; §5.4 wiring → T10; §6 safety (verify/capability/contract) → T9; §7 testing → every task; §9 deliverables → T1-T11. ✓

**Placeholder scan:** Dart-side tasks (T2-T4) carry structural sketches + "mirror SP-B's pattern" with exact signatures + the constraint addresses, not guessed values — the exact write addresses/encodings are in Global Constraints, and the applier-vs-real-writers contract (T3) is the gate that forces correctness. The Python core (`compute_register_writes`, the executor flow) is given in full. Not unspecified placeholders.

**Type consistency:** `WriteCommand`/`FieldWrite`/`SlotSpec` (Dart + Python), `applyWrites`→`compute_register_writes` returning `(unitId,address,value)`, `RegisterWrite`, `WriteExecutor.dispatch`, `read_word`/`write_registers`, `DISPATCHABLE_COMMANDS` — consistent across tasks. Vector shape `{modelId,command,payload,priorRegisters,expectedRegisterWrites}` consistent T4↔T7.

**Carried risk:** the applier-vs-real-writers extraction (T3) — capturing the real writers' `(address,value)` via a fake client is the recommended low-invasion path; if a writer's compute can't be cleanly captured, the implementer reports it. The TOU end-via-next-slot wrap (slot 5 → slot 0 start) and the enable `clearMask` are the subtlest; the golden vectors pin them.
