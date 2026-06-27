# HA Direct Harvester — SP-C: Native Modbus Write Executor (control)

**Date:** 2026-06-27
**Status:** Design (approved for spec review)
**Initiative:** Home Assistant as a full-scale inverter harvester (direct Modbus TCP)
**Sub-project:** SP-C of 4 — direct control writes
**Repos:** the `svitgrid` monorepo (write-spec data + write golden vectors) and the
`home-assistant-svitgrid` add-on (this repo: the write executor).

---

## 1. Background & motivation

SP-B made the add-on read the inverter directly over Modbus, driven by a
data-driven register-spec. SP-C makes it **write** control registers directly —
set work-mode, force the generator, toggle solar-sell / grid-charge, set the
gen-port mode, and program battery-charge / time-of-use (TOU) windows — driven by
the register-spec's `writes` array (which SP-A reserved but left empty).

The add-on **already** has the command machinery: `command_poller.py` polls
`GET /api/v3/executors/commands`, verifies the admin ECDSA-P256 signature
**before** dispatch, calls `executor.dispatch(command_name, payload)`, and sends a
**signed ACK**. SP-C is a **new native executor** plugged into that path — it does
NOT touch the poller, the signing, or the trust model. Signature trust is already
established by the time `dispatch` runs.

SP-C is **writes only**. The phone→add-on onboarding handoff is SP-D.

### Initiative context (not all in scope here)

- **SP-A — register-spec format + extraction** *(done, merged monorepo main).*
- **SP-B — Python harvest engine (reads)** *(done, merged HA main `7015f1d` +
  monorepo `3f715ce42`).*
- **SP-C — native write/command executor** *(this spec).* Depends on SP-A + SP-B.
- **SP-D — onboarding + pairing handoff** *(pending; also wires the harvest
  config-flow into a user-reachable menu — both SP-B and SP-C land dormant until
  then).* 

### Locked decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Command scope | Full set incl. multi-register TOU: `set_work_mode`, `set_gen_force`, `set_solar_sell`, `set_grid_charge_toggle`, `set_gen_port_mode`, `set_battery_charge` (TOU) |
| Write-spec format | Extend `WriteDef` to a command-spec with one or more **field-writes**; `encoding: bit:N` = read-modify-write; a **`slot` block** (base + per-field offsets + stride + end-via-next-slot) for TOU |
| Correctness | **Write golden vectors** — `{command, payload} → expectedRegisterWrites` generated from the REAL Dart writer; a Python contract test pins the Python executor to it (mirrors SP-B reads) |
| Verification | **Verify read-back**: write → read the written registers → confirm intended values → ACK success; else ACK `rejected/reason` |
| Trust | Signature is verified upstream by the poller; the executor does NOT re-verify |
| Capability gate | A model supports a command iff its spec has a `writes` entry for it (Victron/Solplanet/grid-tie/micro get none) |
| Wiring | A direct-harvest inverter (`harvest_config`) gets the native `WriteExecutor` in `executors_by_inverter`; relay inverters keep their YAML/SMG-II executor |

---

## 2. Scope

### In scope (SP-C)

**Monorepo:**
1. Extend the write-spec format (`WriteDef` → command-spec with field-writes +
   `slot` block; `bit:N` RMW encoding).
2. Author each writable model's `writes` data in the export tool, from the
   existing Dart writers — gated by the same hybrid-only capability rule.
3. A **write golden-vector tool** + committed `write-golden-vectors.json` +
   staleness gate.

**This repo (HA add-on):**
4. `WriteDef`/command-spec parsing in `harvest/register_spec.py`.
5. A transport **write path** (`write_registers` + `bit-RMW`) in
   `harvest/transport.py`.
6. `harvest/write_executor.py` — a `BaseExecutor` that computes register writes
   from the spec, writes, **reads back & confirms**, returns a result (or raises
   → poller ACKs rejected).
7. Executor wiring: a direct-harvest inverter gets the native `WriteExecutor`.
8. The vendored **write golden-vector contract test** (Python executor == Dart
   writer register writes).

### Out of scope

- Reads (SP-B), onboarding/pairing handoff and making controls user-reachable (SP-D).
- New command *types* beyond the six listed; the relay path's YAML/SMG-II
  executors are unchanged for relay inverters.
- Live-hardware verification — every model's write addresses are the SAME the
  mobile/edge fleet already uses, but most are UNVERIFIED on real hardware (per
  the model table). SP-C proves Python == Dart writer, not address correctness;
  verify-read-back is the runtime net.

---

## 3. Write-spec format extension

The Dart `WriteDef` today: `{command, address, encoding('full_word'|'bit:N'),
valueScale, onValue, offValue, limits}`. It expresses a single-register write.
SP-C generalizes a model's `writes` to a list of **command-specs**, each one of
two shapes:

### 3.1 Simple command (one or a few field-writes)

```yaml
writes:
  - command: set_gen_force          # 3-phase: full word
    fields:
      - { payloadField: enabled, address: 132, encoding: full_word, onValue: 1, offValue: 0 }
  - command: set_gen_force          # 1-phase variant (different model spec)
    fields:
      - { payloadField: enabled, address: 326, encoding: "bit:13" }   # read-modify-write
  - command: set_work_mode
    fields:
      - { payloadField: workMode, address: 142, encoding: full_word, limits: { min: 0, max: 2 } }
  - command: set_solar_sell
    fields:
      - { payloadField: enabled, address: 145, encoding: "bit:0", onValue: 1, offValue: 0 }
  - command: set_grid_charge_toggle
    fields:
      - { payloadField: enabled, address: 130, encoding: full_word, onValue: 1, offValue: 0 }
  - command: set_gen_port_mode
    fields:
      - { payloadField: mode, address: 133, encoding: full_word }
```

- `encoding: full_word` → `value = round(payload[field] / valueScale)` (or
  `onValue`/`offValue` for booleans), clamped to `limits`, written as one register.
- `encoding: bit:N` → **read-modify-write**: read the register, set bit N for a
  truthy payload (or `onValue`), clear it otherwise, write the modified word back.
- A command may carry multiple `fields` (each its own register/encoding); all are
  written in one executor call.

### 3.2 Slotted command (battery-charge / TOU)

```yaml
  - command: set_battery_charge
    slot:
      indexField: slotIndex          # payload carries which of the 6 TOU slots
      count: 6
      stride: 1                      # registers per slot step
      endViaNextSlotStart: true      # set slot N's end by writing slot N+1's start
      fields:
        - { payloadField: slotStart,         base: 148, encoding: full_word }   # HHMM
        - { payloadField: chargePowerLimitW, base: 154, encoding: full_word, limits: { min: 0, max: 12000 } }
        - { payloadField: chargeVoltage,     base: 160, encoding: full_word, valueScale: 0.01 }
        - { payloadField: gridChargeSoc,     base: 166, encoding: full_word, limits: { min: 0, max: 100 } }
        - { payloadField: gridChargeEnabled, base: 172, encoding: "bit:0", onValue: 1, offValue: 0 }
        - { payloadField: slotEnd,           base: 148, encoding: full_word, viaNextSlot: true }  # writes slot (N+1) start
```

- The target register for a slot field = `base + indexField * stride` (1-phase
  models use a different base set, e.g. 250/256/262/268/274 — emitted per model).
- `endViaNextSlotStart`/`viaNextSlot`: the `slotEnd` field is written to the
  **next** slot's start register (`base + (index+1)*stride`) because Deye has no
  end register. The executor and the golden vectors encode this exactly as the
  Dart writer does (consistent with the SP-A custom-event TOU behavior).

The format is data; the executor and the contract test give it meaning. The Dart
`battery_charge_registers.dart` / `work_mode_registers.dart` / `gen_force_executor.dart`
already hold these families — the export tool reads them, it does not re-derive.

---

## 4. Monorepo half — author writes + write golden vectors

### 4.1 Author `writes`

`tool/export_register_specs.dart` is extended so each **writable** model emits a
`writes` list from the Dart writers. Capability gate = the existing hybrid set
(`configRegisterBatchForModel` / the API's `CONFIG_WRITABLE_MODELS` +
`SELL_TO_GRID_WRITABLE_MODELS` + `GEN_FORCE_CAPABLE_MODELS`): Victron, Solplanet,
grid-tie string, and microinverter models get **no** `writes`. Specs regenerate;
the SP-A staleness gate already byte-checks them.

### 4.2 Write golden vectors

A new `tool/export_write_golden_vectors.dart` emits
`write-golden-vectors.json`: a list of `{ modelId, command, payload,
expectedRegisterWrites: [{unitId, address, value}] }`, where
`expectedRegisterWrites` is what the **real Dart writer** produces for that
command+payload (the ground truth — including bit-RMW masking against a seeded
prior register value, and the TOU end-via-next-slot register math). Committed and
staleness-gated like SP-B's read vectors.

The vector set MUST cover: every command × a representative writable model;
the 1-phase **bit:13 RMW** gen-force (with a seeded prior word so the mask is
exercised); a **TOU slot** write including the end-via-next-slot register; a
`limits`-clamped value; and a boolean on/off pair.

Bit-RMW vectors carry a `priorRegisters` map (the register value the executor
reads before masking) so the expected post-write value is deterministic.

### 4.3 Distribution

`write-golden-vectors.json` is vendored into the HA repo at
`tests/fixtures/write-golden-vectors.json` (extend `scripts/sync-golden-vectors.sh`
to copy both files). The Python write-contract test consumes it. Same
checksum/`sourceCommit` posture as SP-B.

---

## 5. HA half — parse, write, verify, wire

### 5.1 `register_spec.py` — `WriteDef`/command-spec

Add the command-spec dataclasses (`WriteCommand`, `FieldWrite`, `SlotSpec`) and
parse `spec.writes`. `RegisterSpec.validate()` gains: a `bit:N` encoding has a
valid N; a `slot` command has `indexField`+`count`+`fields`; a field's
`payloadField` is a string. Unknown encodings rejected.

### 5.2 `transport.py` — write path

- `async def write_registers(hass, spec, cfg, writes)` where `writes` is a list of
  `(unitId, address, value)` — pymodbus `client.write_registers(address, values,
  device_id=)`; pysolarmanv5 `sm.write_holding_registers(register_addr, values)`.
  Run via `hass.async_add_executor_job`; lazy imports.
- `async def read_modify_write_bit(hass, spec, cfg, unitId, address, bit, set_)` —
  read the current word (reusing the read path), set/clear the bit, write it back.
  One connection per command where the protocol allows.

### 5.3 `write_executor.py` — `WriteExecutor(BaseExecutor)`

`async def dispatch(self, command_name, payload) -> dict`:
1. Find the command-spec in `spec_holder.spec.writes`; if absent → raise
   `NotImplementedError` (poller ACKs `unsupported`).
2. Compute the concrete register writes: for each field, resolve the address
   (slot base+offset), encode the value (`valueScale`, `onValue/offValue`, `limits`
   clamp), and for `bit:N` perform RMW (read prior word → mask). Produce a
   `[(unitId, address, value)]` list — **the exact thing the golden vectors pin**.
3. Write them (transport).
4. **Verify**: read back the written registers; confirm each holds the intended
   value (for `bit:N`, the bit matches). If any mismatch → raise (poller ACKs
   `rejected`, reason names the field). Else return
   `{written: [...], verified: true}`.
A pure helper `compute_register_writes(command_spec, payload, prior_registers)`
holds step 2 so the contract test can assert it == the Dart writer without any
I/O.

### 5.4 Wiring

In entry-setup, when an inverter has `harvest_config`, build a
`WriteExecutor(spec_holder, hass, cfg)` and put it in `executors_by_inverter[id]`
(the dict `command_poller` dispatches against). Relay inverters keep their
existing YAML/SMG-II executor. The command poller is otherwise unchanged.

---

## 6. Safety & error handling

- **Verify-read-back is the net for UNVERIFIED addresses.** A write to a wrong /
  read-only register, or one the inverter ignores, reads back wrong → `rejected`,
  not false success. (Trade-off: a register the inverter silently transforms could
  read back "mismatched" → a false `rejected`; safer than false success.)
- **Capability gate**: no `writes` entry for the command on this model →
  `unsupported`. `limits` clamp out-of-range values before writing.
- **Executor contract**: any failure raises; the poller catches and sends a signed
  `rejected` ACK with a reason — it never crashes the poll loop. Signature trust is
  already established upstream.
- A command for an inverter whose `spec_holder.spec is None` (not yet loaded) →
  raise `rejected` (reason `spec_not_loaded`); the next poll retries.

---

## 7. Testing

TDD throughout, **all offline — no hardware**:
- `register_spec.py`: command-spec/`WriteDef` parse + `validate()` (bit:N, slot,
  unknown encoding).
- `write_executor.compute_register_writes`: unit tests per command (full_word,
  bit:N RMW with a prior word, slot base+offset, end-via-next-slot, limits clamp,
  on/off).
- **Write golden-vector contract** (`test_write_golden_vectors.py`): for every
  vector, `compute_register_writes(spec, command, payload, priorRegisters)` ==
  `expectedRegisterWrites`. The Py↔Dart pin.
- `transport.py`: `write_registers` + `read_modify_write_bit` against
  `pymodbus`/`pysolarmanv5` doubles (assert the written address/values; RMW reads
  then writes the masked word).
- `write_executor.dispatch`: happy (write+verify→result), verify-mismatch→raise,
  unsupported command→raise, spec-not-loaded→raise — with a fake transport.
- Wiring: a `harvest_config` inverter gets a `WriteExecutor` in
  `executors_by_inverter`; relay inverters keep theirs; full suite stays green.

---

## 8. Open questions / risks

- **TOU end-via-next-slot fidelity.** The slot-end-by-next-slot-start math is the
  subtlest write; the golden vectors (generated from the Dart writer) are the
  oracle. A vector with a near-last slot (does slot 5's end write slot 6 / wrap?)
  must match the Dart writer's actual behavior — planning enumerates it.
- **Bit-RMW read dependency.** RMW needs a read of the current word first; if that
  read fails, the command must `rejected` (not write a half-known word). The
  golden vectors seed `priorRegisters` so the pure computation is deterministic;
  the transport test covers the read-then-write.
- **Unverified addresses.** SP-C writes the same addresses the fleet already uses;
  correctness vs the Dart writer is pinned, but address-vs-hardware is not.
  verify-read-back + the per-model capability gate are the containment.
- **Two write tools in the monorepo.** SP-C adds a second golden-vector tool (for
  writes) alongside SP-B's read one; they share fixtures/dispatch helpers where
  possible (`golden_support.dart`).

---

## 9. Deliverables checklist (SP-C)

Monorepo:
- [ ] Write-spec format extension (`WriteDef`→command-spec, `slot` block, `bit:N`).
- [ ] Export tool emits `writes` for writable models (capability-gated) + regenerated specs.
- [ ] `tool/export_write_golden_vectors.dart` + `write-golden-vectors.json` + staleness gate.

This repo:
- [ ] `harvest/register_spec.py` command-spec parse + validate (+ tests).
- [ ] `harvest/transport.py` `write_registers` + `read_modify_write_bit` (+ tests).
- [ ] `harvest/write_executor.py` — `compute_register_writes` + `dispatch` (write+verify) (+ tests).
- [ ] Vendored `tests/fixtures/write-golden-vectors.json` + sync-script extension + Python contract test.
- [ ] Executor wiring in `__init__.py` (native WriteExecutor for harvest inverters) (+ test).
