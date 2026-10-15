"""The whole-program form of set_battery_charge and restore_battery_charge.

The API sends ``set_battery_charge`` as a six-slot program when the inverter's
settings mirror is fresh and carries ``enableRaw``
(``buildBatteryChargePayload`` in services/api/src/calendar/
battery-charge-payload.ts). That payload carries ``prevSlots`` and no
``slotIndex``, and the executor raised ``KeyError: 'slotIndex'`` on it.

The edge firmware routes the same payload to ``tou_restore_all_slots``
(devices/edge-connector-v2/components/zigbee_poller/tou_writer.c): per slot, in
order, start, power, voltage (skipped when 0), SOC and the enable word. These
tests pin that sequence on the vendored deye_sg04lp3 recipe, using the slot
table the firmware's own test uses (``SSCHAPLIN_ORIGINAL`` in
test/test_tou_restore.c).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.svitgrid.harvest.register_spec import WriteCommand
from custom_components.svitgrid.harvest.write_compute import compute_register_writes

_VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "write-golden-vectors.json"
_CFG = {"ip": "192.168.1.100", "logger_serial": "99999", "port": "8899", "slave_id": "1"}
MODULE = "custom_components.svitgrid.harvest.write_executor"


def _sg04lp3_charge() -> WriteCommand:
    data = json.loads(_VECTORS.read_text())
    for v in data["vectors"]:
        if v["modelId"] == "deye_sg04lp3" and v["command"] == "set_battery_charge":
            return WriteCommand.from_dict(v["writeCommand"])
    raise AssertionError("no vendored deye_sg04lp3 set_battery_charge vector")


# The owner's table as the inverter holds it (firmware test SSCHAPLIN_ORIGINAL).
# `end` is the next slot's start: Deye has no end register.
ORIGINAL = [
    {"start": 0, "end": 700, "power": 3000, "voltage": 4900, "soc": 100, "enable": 960},
    {"start": 700, "end": 800, "power": 2000, "voltage": 4900, "soc": 30, "enable": 0},
    {"start": 800, "end": 900, "power": 3000, "voltage": 4900, "soc": 30, "enable": 0},
    {"start": 900, "end": 1100, "power": 5000, "voltage": 4900, "soc": 30, "enable": 0},
    {"start": 1100, "end": 2255, "power": 5000, "voltage": 4900, "soc": 30, "enable": 0},
    {"start": 2255, "end": 0, "power": 5000, "voltage": 4900, "soc": 100, "enable": 0},
]


def _registers_of(table: list[dict]) -> dict[int, int]:
    regs: dict[int, int] = {}
    for i, s in enumerate(table):
        regs[148 + i] = s["start"]
        regs[154 + i] = s["power"]
        regs[160 + i] = s["voltage"]
        regs[166 + i] = s["soc"]
        regs[172 + i] = s["enable"]
    return regs


def _api_program_payload() -> dict:
    """What buildBatteryChargePayload sends for a 02:00-05:00 grid charge to 90 %.

    The window overlaps slot 1 (00:00-07:00) only, so buildEventWindowProgram
    sets slot 1's SOC to 90, its voltage to `chargeVoltage` (0 here: no battery
    chemistry, so "skip the voltage write") and its grid bit, keeps its power
    (the event sets no limit), and clears the grid bit on the other five. The
    single-slot selectors `slotIndex`, `slotStart` and `slotEnd` are absent.
    """
    slots = [dict(s) for s in ORIGINAL]
    slots[0].update(soc=90, voltage=0, enable=(960 & ~1) | 1)
    for s in slots[1:]:
        s["enable"] &= ~1
    return {
        "inverterId": "inv-1",
        "gridChargeEnabled": True,
        "gridChargeSoc": 90,
        "chargeVoltage": 0,
        "ratedPowerW": 12000,
        "prevSlots": slots,
    }


def _firmware_writes(slots: list[dict], live_enable: list[int] | None = None) -> list:
    """The writes tou_restore_all_slots makes on REGS_3PHASE, in its order.

    Enable bits 0 and 1 come from the payload and every other bit from the live
    register (see the enable note in write_compute). With the mirror in step
    with the inverter, as here, that is the word the firmware writes.
    """
    out = []
    for i, s in enumerate(slots):
        out.append((1, 148 + i, s["start"]))
        out.append((1, 154 + i, s["power"]))
        if s["voltage"] > 0:
            out.append((1, 160 + i, s["voltage"]))
        out.append((1, 166 + i, s["soc"]))
        live = live_enable[i] if live_enable else s["enable"]
        out.append((1, 172 + i, (live & ~0x03) | (s["enable"] & 0x03)))
    return out


# ---------------------------------------------------------------------------
# compute_register_writes
# ---------------------------------------------------------------------------


def test_api_program_payload_writes_what_the_firmware_writes():
    payload = _api_program_payload()
    got = compute_register_writes(_sg04lp3_charge(), payload, _registers_of(ORIGINAL))

    assert got == _firmware_writes(payload["prevSlots"])
    # Spot checks against the firmware's per-slot order and skip rules.
    assert got[:4] == [(1, 148, 0), (1, 154, 3000), (1, 166, 90), (1, 172, 961)]
    assert (1, 160, 0) not in got  # voltage 0 is "skip", as tou_restore_all_slots does
    assert (1, 161, 4900) in got
    assert not any(addr == 149 and value == 0 for _, addr, value in got)


def test_program_ignores_top_level_single_slot_fields():
    """`gridChargeSoc` and friends stay on the payload for the API's own use.

    The firmware's per-slot branch writes only the table, so a top-level value
    must not become a slot write here either.
    """
    payload = _api_program_payload()
    payload.update(slotStart=200, slotEnd=500, powerLimit=1234)
    got = compute_register_writes(_sg04lp3_charge(), payload, _registers_of(ORIGINAL))
    assert got == _firmware_writes(payload["prevSlots"])


def test_program_wins_over_slot_index_when_both_are_present():
    """The custom-action restore sends `slotIndex` AND `prevSlots`.

    The firmware takes the per-slot branch whenever `prevSlots` has six entries
    (`!per_slot_restore` gates the single-slot one), so the table wins.
    """
    payload = {"slotIndex": 2, "disableOtherSlots": False, "prevSlots": ORIGINAL}
    got = compute_register_writes(_sg04lp3_charge(), payload, _registers_of(ORIGINAL))
    assert got == _firmware_writes(ORIGINAL)


def test_program_keeps_live_enable_bits_above_bit_1():
    """A mirror up to 48 h old can miss a bit the owner changed on the panel.

    Bits 0 and 1 (grid and generator charge) come from the payload, the rest
    from the live register, as the phone harvester's executor composes them.
    """
    live = _registers_of(ORIGINAL)
    live[172] = 960 | 0x04 | 0x02  # bit 2 appeared, generator charge was on
    payload = _api_program_payload()

    got = compute_register_writes(_sg04lp3_charge(), payload, live)

    assert (1, 172, (960 | 0x04) | 0x01) in got  # 0x3C5: bit 1 cleared, bit 0 set


def test_program_skips_a_field_an_entry_leaves_out():
    slots = [dict(s) for s in ORIGINAL]
    del slots[3]["power"]
    slots[4]["soc"] = None
    got = compute_register_writes(_sg04lp3_charge(), {"prevSlots": slots}, _registers_of(ORIGINAL))
    addrs = [a for _, a, _ in got]
    assert 157 not in addrs
    assert 170 not in addrs
    assert 151 in addrs and 175 in addrs


def test_program_clamps_to_the_recipe_limits():
    slots = [dict(s) for s in ORIGINAL]
    slots[0]["soc"] = 150
    got = compute_register_writes(_sg04lp3_charge(), {"prevSlots": slots}, _registers_of(ORIGINAL))
    assert (1, 166, 100) in got


@pytest.mark.parametrize("prev_slots", [ORIGINAL[:5], ORIGINAL + ORIGINAL[:1], "x", {}])
def test_program_of_the_wrong_shape_is_refused(prev_slots):
    """A partial table is never written: the phone executor refuses it too."""
    with pytest.raises(ValueError, match="prevSlots"):
        compute_register_writes(
            _sg04lp3_charge(), {"prevSlots": prev_slots}, _registers_of(ORIGINAL)
        )


def test_program_entry_that_is_not_an_object_is_refused():
    slots = [dict(s) for s in ORIGINAL]
    slots[2] = 5
    with pytest.raises(ValueError, match=r"prevSlots\[2\]"):
        compute_register_writes(_sg04lp3_charge(), {"prevSlots": slots}, _registers_of(ORIGINAL))


def test_slot_payload_with_neither_index_nor_program_is_a_value_error():
    """Raises a named ValueError, not KeyError: the restore of a snapshot with
    no known prior state sends only `gridChargeEnabled`, and guessing slot 1
    for it would write a slot the window may never have touched."""
    with pytest.raises(ValueError, match="slotIndex"):
        compute_register_writes(
            _sg04lp3_charge(), {"inverterId": "inv-1", "gridChargeEnabled": False}, {}
        )


def test_single_slot_vectors_are_unchanged():
    data = json.loads(_VECTORS.read_text())
    for v in data["vectors"]:
        if v["command"] != "set_battery_charge":
            continue
        cmd = WriteCommand.from_dict(v["writeCommand"])
        prior = {int(a): w for a, w in v["priorRegisters"].items()}
        exp = [(x["unitId"], x["address"], x["value"]) for x in v["expectedRegisterWrites"]]
        assert compute_register_writes(cmd, v["payload"], prior) == exp


# ---------------------------------------------------------------------------
# WriteExecutor
# ---------------------------------------------------------------------------


class _Inverter:
    """An in-memory register file standing in for the transport."""

    def __init__(self, registers: dict[int, int]) -> None:
        self.registers = dict(registers)
        self.writes: list[list[tuple[int, int, int]]] = []
        self.block_reads: list[list[int]] = []
        self.fail_reads = False

    async def read_word(self, hass, spec, cfg, unit, addr):
        return None if self.fail_reads else self.registers.get(int(addr), 0)

    async def read_words(self, hass, spec, cfg, unit, addresses):
        self.block_reads.append(sorted(addresses))
        if self.fail_reads:
            return {}
        return {a: self.registers.get(a, 0) for a in addresses}

    async def write_registers(self, hass, spec, cfg, writes):
        self.writes.append(list(writes))
        for _u, addr, value in writes:
            self.registers[int(addr)] = int(value)


def _executor(inv: _Inverter):
    from custom_components.svitgrid.harvest.write_executor import WriteExecutor

    spec = SimpleNamespace(writes=(_sg04lp3_charge(),), default_slave_id=1)
    return WriteExecutor(object(), SimpleNamespace(spec=spec), _CFG)


def _patches(inv: _Inverter):
    from unittest.mock import patch

    return (
        patch(f"{MODULE}.read_word", inv.read_word),
        patch(f"{MODULE}.read_words", inv.read_words),
        patch(f"{MODULE}.write_registers", inv.write_registers),
    )


async def test_dispatch_program_payload_no_longer_raises_key_error():
    inv = _Inverter(_registers_of(ORIGINAL))
    payload = _api_program_payload()
    p1, p2, p3 = _patches(inv)
    with p1, p2, p3:
        result = await _executor(inv).dispatch("set_battery_charge", payload)

    assert inv.writes == [_firmware_writes(payload["prevSlots"])]
    assert result["verified"] is True
    assert result["written"] == [list(w) for w in _firmware_writes(payload["prevSlots"])]


async def test_dispatch_reports_the_table_it_found_like_the_firmware():
    """The API captures `prevSlots` from the ACK and hands it back on restore.

    Without it the snapshot is `priorUnknown`, and the window end could only
    turn grid charging off — leaving the program the window wrote in place.
    """
    inv = _Inverter(_registers_of(ORIGINAL))
    p1, p2, p3 = _patches(inv)
    with p1, p2, p3:
        result = await _executor(inv).dispatch("set_battery_charge", _api_program_payload())

    assert result["prevSlots"] == ORIGINAL
    assert result["prevSlotStart"] == 0
    assert result["prevSlotEnd"] == 700
    assert result["prevPowerLimit"] == 3000
    assert result["prevChargeVoltage"] == 4900
    assert result["prevGridChargeSoc"] == 100
    assert result["prevGridChargeEnable"] == 960


async def test_dispatch_reads_the_table_in_one_block():
    inv = _Inverter(_registers_of(ORIGINAL))
    p1, p2, p3 = _patches(inv)
    with p1, p2, p3:
        await _executor(inv).dispatch("set_battery_charge", _api_program_payload())

    assert inv.block_reads[0] == list(range(148, 178))


async def test_single_slot_dispatch_reports_the_table_too():
    inv = _Inverter(_registers_of(ORIGINAL))
    payload = {
        "inverterId": "inv-1",
        "slotIndex": 0,
        "disableOtherSlots": True,
        "slotStart": 200,
        "slotEnd": 500,
        "gridChargeEnabled": True,
        "gridChargeSoc": 90,
        "chargeVoltage": 0,
    }
    p1, p2, p3 = _patches(inv)
    with p1, p2, p3:
        result = await _executor(inv).dispatch("set_battery_charge", payload)

    assert inv.writes == [[(1, 148, 200), (1, 149, 500), (1, 166, 90), (1, 172, 961)]]
    assert result["prevSlots"] == ORIGINAL


async def test_a_failed_table_read_aborts_before_any_write():
    """The firmware and the phone both refuse to write without the pre-read:
    the restore would have nothing to put back."""
    inv = _Inverter(_registers_of(ORIGINAL))
    inv.fail_reads = True
    p1, p2, p3 = _patches(inv)
    with p1, p2, p3, pytest.raises(RuntimeError, match="prior_read_failed"):
        await _executor(inv).dispatch("set_battery_charge", _api_program_payload())
    assert inv.writes == []


async def test_restore_battery_charge_puts_the_owner_table_back():
    """The window-end restore the API builds from the captured table.

    buildBatteryChargeRestorePayload (prior-charge-snapshot.ts) sends the
    table as `prevSlots` with slot 1's scalars beside it. The spec has no
    `restore_battery_charge` recipe, so it runs on the set recipe, as the
    firmware's single execute_battery_charge serves both names.
    """
    inv = _Inverter(_registers_of(ORIGINAL))
    executor = _executor(inv)
    p1, p2, p3 = _patches(inv)
    with p1, p2, p3:
        set_result = await executor.dispatch("set_battery_charge", _api_program_payload())
        assert inv.registers[166] == 90 and inv.registers[173] == 0

        restore_payload = {
            "inverterId": "inv-1",
            "gridChargeEnabled": (set_result["prevGridChargeEnable"] & 1) == 1,
            "gridChargeSoc": set_result["prevGridChargeSoc"],
            "chargeVoltage": set_result["prevChargeVoltage"],
            "slotStart": set_result["prevSlotStart"],
            "slotEnd": set_result["prevSlotEnd"],
            "powerLimit": set_result["prevPowerLimit"],
            "prevSlots": set_result["prevSlots"],
        }
        result = await executor.dispatch("restore_battery_charge", restore_payload)

    assert result["verified"] is True
    assert inv.registers == _registers_of(ORIGINAL)


async def test_restore_without_a_table_or_slot_is_refused_not_guessed():
    inv = _Inverter(_registers_of(ORIGINAL))
    p1, p2, p3 = _patches(inv)
    with p1, p2, p3, pytest.raises(ValueError, match="slotIndex"):
        await _executor(inv).dispatch(
            "restore_battery_charge", {"inverterId": "inv-1", "gridChargeEnabled": False}
        )
    assert inv.writes == []


async def test_restore_is_unsupported_without_a_charge_recipe():
    from custom_components.svitgrid.harvest.register_spec import RegisterSpec
    from custom_components.svitgrid.harvest.write_executor import WriteExecutor

    spec = RegisterSpec.from_dict(
        {
            "modelId": "m",
            "version": 1,
            "protocol": "modbus_tcp",
            "port": 502,
            "reads": [],
            "derivations": [],
            "writes": [
                {"command": "set_work_mode", "fields": [{"payloadField": "w", "address": 1}]}
            ],
        }
    )
    executor = WriteExecutor(object(), SimpleNamespace(spec=spec), _CFG)
    with pytest.raises(NotImplementedError):
        await executor.dispatch("restore_battery_charge", {"prevSlots": ORIGINAL})


def test_restore_battery_charge_is_dispatchable():
    from custom_components.svitgrid.const import DISPATCHABLE_COMMANDS

    assert "restore_battery_charge" in DISPATCHABLE_COMMANDS


def test_a_preset_without_a_recipe_still_acks_unsupported():
    """Adding restore_battery_charge to DISPATCHABLE_COMMANDS routes it to
    every executor. A preset without a recipe raises UnsupportedCommandError,
    and the poller ACKs that as `unsupported` only for NotImplementedError —
    otherwise the restore would come back `failed` and the API would re-issue
    it."""
    from custom_components.svitgrid.executors.yaml_dispatcher import UnsupportedCommandError

    assert issubclass(UnsupportedCommandError, NotImplementedError)
