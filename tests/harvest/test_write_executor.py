"""Tests for WriteExecutor (SP-C Task 9).

TDD — tests written RED-first, then implementation added to
custom_components/svitgrid/harvest/write_executor.py.

Coverage:
  1. Happy path: full_word work_mode command — writes + verify succeed → result dict.
  2. gen_force bit:13 (bit:N with clear_mask) — prior read seeded, RMW word written,
     verify reads back the exact written value → result dict.
  3. Unsupported command → NotImplementedError.
  4. spec_holder.spec is None → RuntimeError("spec_not_loaded").
  5. Verify mismatch (read-back differs) → RuntimeError("verify_failed:...").
  6. Prior read fails (read_word returns None for a bit field) →
     RuntimeError("prior_read_failed:...").
  7. set_battery_charge (legacy ABC method) routes through dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.svitgrid.harvest.register_spec import RegisterSpec, WriteCommand

# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------

_CFG = {"ip": "192.168.1.100", "logger_serial": "99999", "port": "8899", "slave_id": "1"}

MODULE = "custom_components.svitgrid.harvest.write_executor"


def _spec_with_writes(writes_raw: list[dict]) -> RegisterSpec:
    """Build a minimal RegisterSpec with the given write commands."""
    return RegisterSpec.from_dict(
        {
            "modelId": "test_model",
            "version": 1,
            "protocol": "solarman_v5",
            "port": 8899,
            "defaultSlaveId": 1,
            "flags": {},
            "reads": [],
            "derivations": [],
            "writes": writes_raw,
        }
    )


def _spec_work_mode() -> RegisterSpec:
    """RegisterSpec with a single full_word work_mode write command."""
    return _spec_with_writes(
        [
            {
                "command": "set_work_mode",
                "fields": [
                    {"payloadField": "workMode", "address": 142, "encoding": "full_word"},
                ],
            }
        ]
    )


def _spec_gen_force() -> RegisterSpec:
    """RegisterSpec with a bit:13 gen_force write command (1-phase RMW).

    Uses the production-realistic ``set_``-prefixed cloud command name.
    """
    return _spec_with_writes(
        [
            {
                "command": "set_gen_force",
                "fields": [
                    {
                        "payloadField": "genForce",
                        "address": 326,
                        "encoding": "bit:13",
                        "onValue": 1,
                        "offValue": 0,
                        "clearMask": 0x1FFF,
                    },
                ],
            }
        ]
    )


def _spec_battery_charge() -> RegisterSpec:
    """RegisterSpec with set_battery_charge (legacy routing test)."""
    return _spec_with_writes(
        [
            {
                "command": "set_battery_charge",
                "fields": [
                    {"payloadField": "chargeLimit", "address": 340, "encoding": "full_word"},
                ],
            }
        ]
    )


def _spec_battery_charge_slot() -> RegisterSpec:
    """RegisterSpec with a slot command: full_word start + bit:0+clear_mask enable.

    Models the battery_charge slot (count=6, stride=1) with:
      - slotStart at base=148 (full_word)
      - gridChargeEnabled at base=172 (bit:0, clear_mask=0x03)
    """
    return _spec_with_writes(
        [
            {
                "command": "set_battery_charge_slot",
                "fields": [],
                "slot": {
                    "indexField": "slotIndex",
                    "count": 6,
                    "stride": 1,
                    "fields": [
                        {
                            "payloadField": "slotStart",
                            "base": 148,
                            "encoding": "full_word",
                        },
                        {
                            "payloadField": "gridChargeEnabled",
                            "base": 172,
                            "encoding": "bit:0",
                            "clearMask": 3,
                            "onValue": 1,
                            "offValue": 0,
                        },
                    ],
                },
            }
        ]
    )


def _words_via(read_word):
    """A read_words stand-in that reads each address through *read_word*.

    Keeps a test's read_word side-effect sequence meaningful: the batched
    verify read consumes it in write order.
    """

    async def read_words(hass, spec, cfg, unit, addresses):
        out = {}
        for a in addresses:
            v = await read_word(hass, spec, cfg, unit, a)
            if v is not None:
                out[a] = v
        return out

    return read_words


def _make_executor(
    spec: RegisterSpec | None, read_word_side_effect=None, write_registers_side_effect=None
):
    """Build a WriteExecutor with mocked transport functions.

    Returns (executor, mock_read_word, mock_write_registers).
    """
    from custom_components.svitgrid.harvest.write_executor import WriteExecutor

    hass = object()  # not used directly; transport is patched
    spec_holder = SimpleNamespace(spec=spec)
    executor = WriteExecutor(hass, spec_holder, _CFG)

    mock_read = AsyncMock()
    mock_write = AsyncMock()

    if read_word_side_effect is not None:
        mock_read.side_effect = read_word_side_effect
    if write_registers_side_effect is not None:
        mock_write.side_effect = write_registers_side_effect

    return executor, mock_read, mock_write


# ---------------------------------------------------------------------------
# 1. Happy path — full_word (no prior needed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_full_word_happy_path():
    """work_mode full_word: write + verify both succeed → correct result dict."""
    spec = _spec_work_mode()
    executor, mock_read, mock_write = _make_executor(spec)

    # prior read returns the owner's mode (2), the verify read the written 3
    mock_read.side_effect = [2, 3]

    with (
        patch(f"{MODULE}.read_word", mock_read),
        patch(f"{MODULE}.read_words", _words_via(mock_read)),
        patch(f"{MODULE}.write_registers", mock_write),
    ):
        result = await executor.dispatch("set_work_mode", {"workMode": 3})

    # The value read before the write is reported, as the firmware's ACK does.
    assert result == {"written": [[1, 142, 3]], "verified": True, "prevWorkMode": 2}
    mock_write.assert_awaited_once_with(executor._hass, spec, _CFG, [(1, 142, 3)])
    # prior read + verify read
    assert mock_read.await_count == 2


# ---------------------------------------------------------------------------
# 2. gen_force bit:13 — prior read + RMW + verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_bit_field_prior_read_and_rmw():
    """gen_force bit:13 with clear_mask: prior read seeded → RMW word written → verified."""
    executor, mock_read, mock_write = _make_executor(_spec_gen_force())

    # prior register 326 = 0x0000; verify read also returns the expected written value
    # compute: base = 0x0000 & ~0x1FFF = 0x0000; set bit 13 → 0x2000
    mock_read.side_effect = [0x0000, 0x2000]  # prior, then verify

    with (
        patch(f"{MODULE}.read_word", mock_read),
        patch(f"{MODULE}.read_words", _words_via(mock_read)),
        patch(f"{MODULE}.write_registers", mock_write),
    ):
        result = await executor.dispatch("set_gen_force", {"genForce": 1})

    assert result == {"written": [[1, 326, 0x2000]], "verified": True, "prevGenForce": 0}
    mock_write.assert_awaited_once()
    # prior read + verify read = 2 total calls
    assert mock_read.await_count == 2


# ---------------------------------------------------------------------------
# 2b. slot + bit:0 — prior read at slot-resolved address + RMW + verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_slot_bit_field_prior_rmw():
    """slot + bit:0 with clear_mask: the slot table is read first, the enable
    word is a read-modify-write of its live value, and both writes verify.

    Setup:
      - slotIndex = 2, count = 6, stride = 1
      - enable field: base = 172, bit:0, clear_mask = 0x03
        → resolved address = 172 + 2*1 = 174
        → live value 0xF2 = 0b11110010
        → written value = (0xF2 & ~0x03) | (1 << 0) = 0xF0 | 1 = 241
      - start field: base = 148, full_word
        → resolved address = 148 + 2*1 = 150
        → written value = 800
    """
    spec = _spec_battery_charge_slot()
    executor, _read, mock_write = _make_executor(spec)
    registers = {174: 0xF2}
    table_reads: list[list[int]] = []

    async def fake_read_words(hass, spec_arg, cfg, unit, addresses):
        table_reads.append(sorted(addresses))
        return {a: registers.get(a, 0) for a in addresses}

    async def fake_write(hass, spec_arg, cfg, writes):
        for _u, a, v in writes:
            registers[a] = v

    mock_write.side_effect = fake_write

    with (
        patch(f"{MODULE}.read_words", fake_read_words),
        patch(f"{MODULE}.write_registers", mock_write),
    ):
        result = await executor.dispatch(
            "set_battery_charge_slot",
            {"slotIndex": 2, "slotStart": 800, "gridChargeEnabled": True},
        )

    # The whole table first, then the verify of what was written.
    assert table_reads[0] == [*range(148, 154), *range(172, 178)]
    assert table_reads[1] == [150, 174]
    mock_write.assert_awaited_once_with(executor._hass, spec, _CFG, [(1, 150, 800), (1, 174, 241)])
    assert result["written"] == [[1, 150, 800], [1, 174, 241]]
    assert result["verified"] is True
    assert result["prevSlots"][2]["enable"] == 0xF2


# ---------------------------------------------------------------------------
# 3. Unsupported command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_unsupported_command_raises():
    """Command not in spec.writes → NotImplementedError."""
    executor, mock_read, mock_write = _make_executor(_spec_work_mode())

    with (
        patch(f"{MODULE}.read_word", mock_read),
        patch(f"{MODULE}.read_words", _words_via(mock_read)),
        patch(f"{MODULE}.write_registers", mock_write),
        pytest.raises(NotImplementedError),
    ):
        await executor.dispatch("unknown_command", {})


# ---------------------------------------------------------------------------
# 4. spec_holder.spec is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_spec_none_raises():
    """spec_holder.spec is None → RuntimeError('spec_not_loaded')."""
    executor, mock_read, mock_write = _make_executor(spec=None)

    with (
        patch(f"{MODULE}.read_word", mock_read),
        patch(f"{MODULE}.read_words", _words_via(mock_read)),
        patch(f"{MODULE}.write_registers", mock_write),
        pytest.raises(RuntimeError, match="spec_not_loaded"),
    ):
        await executor.dispatch("set_work_mode", {"workMode": 3})


# ---------------------------------------------------------------------------
# 5. Verify mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_verify_mismatch_raises():
    """Read-back after write returns wrong value → RuntimeError('verify_failed:...')."""
    executor, mock_read, mock_write = _make_executor(_spec_work_mode())

    # prior 2; verify reads back 99 after writing 3 → mismatch
    mock_read.side_effect = [2, 99]

    with (
        patch(f"{MODULE}.read_word", mock_read),
        patch(f"{MODULE}.read_words", _words_via(mock_read)),
        patch(f"{MODULE}.write_registers", mock_write),
        pytest.raises(RuntimeError, match="verify_failed:142"),
    ):
        await executor.dispatch("set_work_mode", {"workMode": 3})


# ---------------------------------------------------------------------------
# 6. Prior read fails (bit field, read_word returns None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_prior_read_fail_raises():
    """read_word returns None for a bit:N field prior → RuntimeError('prior_read_failed:...')."""
    executor, mock_read, mock_write = _make_executor(_spec_gen_force())

    mock_read.return_value = None  # prior read fails

    with (
        patch(f"{MODULE}.read_word", mock_read),
        patch(f"{MODULE}.read_words", _words_via(mock_read)),
        patch(f"{MODULE}.write_registers", mock_write),
        pytest.raises(RuntimeError, match="prior_read_failed:326"),
    ):
        await executor.dispatch("set_gen_force", {"genForce": 1})


# ---------------------------------------------------------------------------
# 7. Legacy set_battery_charge routes through dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_battery_charge_routes_through_dispatch():
    """set_battery_charge (legacy abstract method) calls dispatch('set_battery_charge', ...)."""
    executor, mock_read, mock_write = _make_executor(_spec_battery_charge())

    # prior read 50, verify read returns the written 80
    mock_read.side_effect = [50, 80]

    with (
        patch(f"{MODULE}.read_word", mock_read),
        patch(f"{MODULE}.read_words", _words_via(mock_read)),
        patch(f"{MODULE}.write_registers", mock_write),
    ):
        result = await executor.set_battery_charge({"chargeLimit": 80})

    assert result == {"written": [[1, 340, 80]], "verified": True, "prevChargeLimit": 50}


# ---------------------------------------------------------------------------
# 8. Verify read returning None → RuntimeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_verify_read_none_raises():
    """read_word returns None during verify (not prior) → RuntimeError('verify_read_failed:...')."""
    executor, mock_read, mock_write = _make_executor(_spec_work_mode())

    # the prior read succeeds; the verify read returns None
    mock_read.side_effect = [2, None]

    with (
        patch(f"{MODULE}.read_word", mock_read),
        patch(f"{MODULE}.read_words", _words_via(mock_read)),
        patch(f"{MODULE}.write_registers", mock_write),
        pytest.raises(RuntimeError, match="verify_read_failed:142"),
    ):
        await executor.dispatch("set_work_mode", {"workMode": 3})


# ---------------------------------------------------------------------------
# 9. REGRESSION (SP-C critical fix): a REAL set_-prefixed cloud command name
#    resolves against a REAL/vendored spec's writes.
#
#    The bug: the cloud dispatches `set_`-prefixed command names
#    (DISPATCHABLE_COMMANDS in const.py — set_gen_force, set_work_mode, …) but
#    the authored register-spec `writes[].command` values used to be UNPREFIXED
#    (gen_force, work_mode, …). dispatch() matches command names by exact string,
#    so every real write ACKed "unsupported" (NotImplementedError). These tests
#    load the actual VENDORED write-golden-vectors.json (the same artifact the
#    add-on ships) and prove a real cloud command name now resolves + writes.
# ---------------------------------------------------------------------------

_WRITE_VECTORS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "write-golden-vectors.json"


def _load_write_vectors() -> list[dict]:
    data = json.loads(_WRITE_VECTORS_PATH.read_text())
    return data["vectors"] if isinstance(data, dict) else data


def _vector_for(command: str) -> dict:
    """Return the first vendored write vector whose top-level command matches."""
    for v in _load_write_vectors():
        if v["command"] == command:
            return v
    raise AssertionError(f"no vendored write vector with command {command!r}")


def _executor_for_vector(vector: dict):
    """Build a WriteExecutor whose spec.writes is the REAL vendored WriteCommand.

    Models hardware with an in-memory register file seeded from the vector's
    priorRegisters; write_registers mutates it and read_word reads it back, so
    prior-read + verify both behave like a real (working) inverter.
    Returns (executor, registers, read_word, write_registers).
    """
    from custom_components.svitgrid.harvest.write_executor import WriteExecutor

    cmd = WriteCommand.from_dict(vector["writeCommand"])
    spec = SimpleNamespace(writes=(cmd,), default_slave_id=1)
    spec_holder = SimpleNamespace(spec=spec)
    executor = WriteExecutor(object(), spec_holder, _CFG)

    registers: dict[int, int] = {
        int(k): int(v) for k, v in vector.get("priorRegisters", {}).items()
    }

    async def fake_read_word(hass, spec_arg, cfg, unit, addr):
        return registers.get(int(addr), 0)

    async def fake_write_registers(hass, spec_arg, cfg, writes):
        for _unit, addr, value in writes:
            registers[int(addr)] = int(value)

    return executor, registers, fake_read_word, fake_write_registers


@pytest.mark.asyncio
async def test_real_set_prefixed_cloud_command_resolves_against_vendored_spec():
    """A real `set_gen_force` cloud command resolves + writes against the vendored spec.

    This is the exact gap the reviewer flagged: previously this dispatch raised
    NotImplementedError because the authored command was unprefixed `gen_force`.
    """
    vector = _vector_for("set_gen_force")
    executor, _registers, fake_read, fake_write = _executor_for_vector(vector)

    expected_writes = [
        [w["unitId"], w["address"], w["value"]] for w in vector["expectedRegisterWrites"]
    ]

    with (
        patch(f"{MODULE}.read_word", fake_read),
        patch(f"{MODULE}.read_words", _words_via(fake_read)),
        patch(f"{MODULE}.write_registers", fake_write),
    ):
        # Must NOT raise NotImplementedError — the real cloud name now resolves.
        result = await executor.dispatch(vector["command"], vector["payload"])

    assert result["verified"] is True
    assert result["written"] == expected_writes


@pytest.mark.asyncio
async def test_legacy_set_battery_charge_alias_resolves_against_vendored_spec():
    """The legacy alias dispatch('set_battery_charge', ...) resolves against the spec."""
    vector = _vector_for("set_battery_charge")
    executor, _registers, fake_read, fake_write = _executor_for_vector(vector)

    expected_writes = [
        [w["unitId"], w["address"], w["value"]] for w in vector["expectedRegisterWrites"]
    ]

    with (
        patch(f"{MODULE}.read_word", fake_read),
        patch(f"{MODULE}.read_words", _words_via(fake_read)),
        patch(f"{MODULE}.write_registers", fake_write),
    ):
        result = await executor.dispatch("set_battery_charge", vector["payload"])

    assert result["verified"] is True
    assert result["written"] == expected_writes


@pytest.mark.asyncio
async def test_dispatch_does_not_write_a_field_the_payload_omits():
    """An omitted field is not written, and its register keeps its value.

    The slot table is still read in full first, as the edge firmware does: it
    is the restore's record of the owner's slots.
    """
    spec = _spec_battery_charge_slot()
    executor, _read, mock_write = _make_executor(spec)

    async def fake_read_words(hass, spec_arg, cfg, unit, addresses):
        return {a: (800 if a == 148 else 0) for a in addresses}

    with (
        patch(f"{MODULE}.read_words", fake_read_words),
        patch(f"{MODULE}.write_registers", mock_write),
    ):
        result = await executor.dispatch(
            "set_battery_charge_slot", {"slotIndex": 0, "slotStart": 800}
        )

    mock_write.assert_awaited_once_with(executor._hass, spec, _CFG, [(1, 148, 800)])
    assert result["written"] == [[1, 148, 800]]
    assert result["verified"] is True


# ---------------------------------------------------------------------------
# 10. Every top-level field is read before it is written, and reported
#
#     The island restores a work mode, a TOU master bit and a solar-sell bit
#     from these values. Without them it restored invented defaults.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "payload", "prior", "written", "key", "reported"),
    [
        ("set_work_mode", {"workMode": 0}, {142: 1}, (1, 142, 0), "prevWorkMode", 1),
        ("set_solar_sell", {"solarSell": 0}, {145: 0b1011}, (1, 145, 0b1010), "prevSolarSell", 1),
        (
            "set_grid_charge_toggle",
            {"gridChargeEnabled": False},
            {146: 0b1111_1110},
            (1, 146, 0b1111_1110),
            "prevGridChargeEnabled",
            0,
        ),
    ],
)
async def test_dispatch_reports_the_value_each_field_held(
    command, payload, prior, written, key, reported
):
    vector = _vector_for(command)
    executor, registers, fake_read, fake_write = _executor_for_vector(vector)
    registers.clear()
    registers.update(prior)

    with (
        patch(f"{MODULE}.read_word", fake_read),
        patch(f"{MODULE}.read_words", _words_via(fake_read)),
        patch(f"{MODULE}.write_registers", fake_write),
    ):
        result = await executor.dispatch(command, payload)

    assert result["written"] == [list(written)]
    assert result[key] == reported


@pytest.mark.asyncio
async def test_a_full_word_prior_that_cannot_be_read_writes_nothing():
    executor, mock_read, mock_write = _make_executor(_spec_work_mode())
    mock_read.return_value = None

    with (
        patch(f"{MODULE}.read_word", mock_read),
        patch(f"{MODULE}.read_words", _words_via(mock_read)),
        patch(f"{MODULE}.write_registers", mock_write),
        pytest.raises(RuntimeError, match="prior_read_failed:142"),
    ):
        await executor.dispatch("set_work_mode", {"workMode": 0})

    mock_write.assert_not_awaited()
