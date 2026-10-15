"""The API's restore_* commands, and the set_* fields WriteExecutor dropped.

Cloud mode. When an event ends, the API hands back the owner's values with
`restore_work_mode`, `restore_solar_sell`, `restore_grid_charge_toggle`,
`restore_sell_power_cap`, `restore_gen_force`, `restore_gen_port_mode` and
`restore_energy_pattern` (services/api/src/calendar/evaluator.ts,
custom-action-catalog.ts). None was in DISPATCHABLE_COMMANDS, so the poller
ACKed each as `unsupported` and an HA-run inverter stayed in Selling First, or
with grid charge off, after a sell_to_grid or use_battery window. Each runs on
its set_* recipe, as the edge firmware serves both names with one handler.

Two payload fields were dropped:
- `workMode` on set_solar_sell / restore_solar_sell. The API's sell_to_grid
  floor hold sends `{solarSell: 1, workMode: 1}` and its restore
  `{solarSell, workMode}`; the firmware's execute_solar_sell writes the work
  mode first. The recipe has only `solarSell`, so the inverter stayed in
  Selling First.
- `disableOtherSlots` on a single-slot set_battery_charge. The firmware clears
  bit 0 of every other slot whose bit 0 is set, keeping the rest of the word.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.svitgrid.const import DISPATCHABLE_COMMANDS
from custom_components.svitgrid.harvest.register_spec import WriteCommand
from custom_components.svitgrid.harvest.write_compute import compute_register_writes

_VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "write-golden-vectors.json"
_CFG = {"ip": "192.168.1.100", "logger_serial": "99999", "port": "8899", "slave_id": "1"}
MODULE = "custom_components.svitgrid.harvest.write_executor"


def _sg04lp3_writes() -> tuple[WriteCommand, ...]:
    out = {}
    for v in json.loads(_VECTORS.read_text())["vectors"]:
        if v["modelId"] == "deye_sg04lp3":
            out.setdefault(v["command"], WriteCommand.from_dict(v["writeCommand"]))
    return tuple(out.values())


class _Inverter:
    def __init__(self, registers: dict[int, int]) -> None:
        self.registers = dict(registers)
        self.writes: list[tuple[int, int, int]] = []

    async def read_word(self, hass, spec, cfg, unit, addr):
        return self.registers.get(int(addr), 0)

    async def read_words(self, hass, spec, cfg, unit, addresses):
        return {a: self.registers.get(a, 0) for a in addresses}

    async def write_registers(self, hass, spec, cfg, writes):
        for w in writes:
            self.writes.append(tuple(w))
            self.registers[int(w[1])] = int(w[2])


def _executor(writes=None):
    from custom_components.svitgrid.harvest.write_executor import WriteExecutor

    spec = SimpleNamespace(writes=writes or _sg04lp3_writes(), default_slave_id=1)
    return WriteExecutor(object(), SimpleNamespace(spec=spec), _CFG)


async def _run(inv: _Inverter, command: str, payload: dict, writes=None) -> dict:
    with (
        patch(f"{MODULE}.read_word", inv.read_word),
        patch(f"{MODULE}.read_words", inv.read_words),
        patch(f"{MODULE}.write_registers", inv.write_registers),
    ):
        return await _executor(writes).dispatch(command, payload)


# ─────────────────────────── restore_* commands ──────────────────────────────

# (command, the API's payload, owner register before, expected writes)
_API_RESTORES = [
    ("restore_work_mode", {"inverterId": "inv-1", "workMode": 1}, {142: 0}, [(1, 142, 1)]),
    (
        "restore_grid_charge_toggle",
        # dispatchTouToggle sends 1/0, not a bool.
        {"inverterId": "inv-1", "gridChargeEnabled": 1},
        {146: 0b1111_1110},
        [(1, 146, 0b1111_1111)],
    ),
    (
        "restore_sell_power_cap",
        {"inverterId": "inv-1", "sellPowerCapW": 9000, "upperBoundW": 24000, "ratedPowerW": 24000},
        {143: 4000},
        [(1, 143, 9000)],
    ),
    ("restore_gen_force", {"inverterId": "inv-1", "on": False}, {132: 1}, [(1, 132, 0)]),
    ("restore_gen_port_mode", {"inverterId": "inv-1", "genPortMode": 2}, {133: 0}, [(1, 133, 2)]),
    (
        "restore_energy_pattern",
        {"inverterId": "inv-1", "energyPattern": 1},
        {141: 0},
        [(1, 141, 1)],
    ),
]


@pytest.mark.parametrize(("command", "payload", "before", "expected"), _API_RESTORES)
async def test_each_api_restore_runs_on_its_set_recipe(command, payload, before, expected):
    inv = _Inverter(before)
    result = await _run(inv, command, payload)
    assert inv.writes == expected
    assert result["verified"] is True


@pytest.mark.parametrize("command", [c for c, *_ in _API_RESTORES] + ["restore_solar_sell"])
def test_each_api_restore_is_dispatchable(command):
    assert command in DISPATCHABLE_COMMANDS


def test_set_energy_pattern_is_dispatchable():
    # Every Deye spec carries the recipe; the API sends it for a custom
    # energy_pattern action. It was missing from the allowlist too.
    assert "set_energy_pattern" in DISPATCHABLE_COMMANDS


async def test_restore_sell_power_cap_puts_back_a_cap_above_the_set_limit():
    """The API restores the cap the device reported, not a clamped one (#52).

    The recipe clamps a SET to 15000 W. A Deye accepts 24000 on register 143,
    and 11 live units hold more than their rating, so a restore clamped to
    15000 would cut the owner's export limit.
    """
    inv = _Inverter({143: 5000})
    await _run(inv, "restore_sell_power_cap", {"sellPowerCapW": 24000})
    assert inv.writes == [(1, 143, 24000)]


async def test_set_sell_power_cap_keeps_the_recipe_limit():
    inv = _Inverter({143: 5000})
    await _run(inv, "set_sell_power_cap", {"sellPowerCapW": 24000})
    assert inv.writes == [(1, 143, 15000)]


async def test_restore_sell_power_cap_is_a_16_bit_register():
    inv = _Inverter({143: 5000})
    await _run(inv, "restore_sell_power_cap", {"sellPowerCapW": 80000})
    assert inv.writes == [(1, 143, 0xFFFF)]


# ─────────────────────────── workMode on solar sell ──────────────────────────


async def test_restore_solar_sell_writes_the_work_mode_first():
    """deactivateSellToGrid after a floor hold: both values in one command."""
    inv = _Inverter({142: 1, 145: 0b1011})
    result = await _run(
        inv, "restore_solar_sell", {"inverterId": "inv-1", "solarSell": 0, "workMode": 0}
    )
    assert inv.writes == [(1, 142, 0), (1, 145, 0b1010)]
    assert result["prevWorkMode"] == 1
    assert result["prevSolarSell"] == 1


async def test_set_solar_sell_with_a_work_mode_leaves_selling_first():
    """The API's floor hold: sell the surplus in a zero-export mode."""
    inv = _Inverter({142: 0, 145: 0})
    result = await _run(inv, "set_solar_sell", {"solarSell": 1, "workMode": 1})
    assert inv.registers[142] == 1
    assert inv.registers[145] == 1
    assert result["prevWorkMode"] == 0
    assert result["prevSolarSell"] == 0


async def test_set_solar_sell_without_a_work_mode_writes_only_the_bit():
    inv = _Inverter({142: 0, 145: 0})
    await _run(inv, "set_solar_sell", {"solarSell": 1})
    assert inv.writes == [(1, 145, 1)]


async def test_solar_sell_with_a_work_mode_and_no_work_mode_recipe_writes_nothing():
    """Writing only the bit would leave the inverter in Selling First."""
    only_sell = tuple(c for c in _sg04lp3_writes() if c.command == "set_solar_sell")
    inv = _Inverter({142: 0, 145: 0})
    with pytest.raises(NotImplementedError):
        await _run(inv, "restore_solar_sell", {"solarSell": 0, "workMode": 1}, only_sell)
    assert inv.writes == []


# ─────────────────────────── disableOtherSlots ───────────────────────────────


def _charge_recipe() -> WriteCommand:
    return next(c for c in _sg04lp3_writes() if c.command == "set_battery_charge")


def _slot_payload(**kw) -> dict:
    payload = {
        "slotIndex": 0,
        "slotStart": 200,
        "slotEnd": 500,
        "gridChargeEnabled": True,
        "gridChargeSoc": 90,
        "disableOtherSlots": True,
    }
    payload.update(kw)
    return payload


def test_disable_other_slots_clears_bit_0_of_every_enabled_slot():
    # Slots 2 and 4 grid-charge (bit 0) with generator/day bits above it.
    prior = {172: 0, 173: 0b10, 174: 0b1101, 175: 0, 176: 0b11, 177: 0}
    writes = compute_register_writes(_charge_recipe(), _slot_payload(), prior)
    assert (1, 174, 0b1100) in writes
    assert (1, 176, 0b10) in writes
    # Slots whose bit 0 is already clear are not touched, as in the firmware.
    assert not any(addr in (173, 175, 177) for _u, addr, _v in writes)
    # The target slot is written as before.
    assert (1, 172, 1) in writes


def test_disable_other_slots_false_leaves_the_other_slots():
    prior = {174: 0b1101}
    writes = compute_register_writes(
        _charge_recipe(), _slot_payload(disableOtherSlots=False), prior
    )
    assert not any(addr == 174 for _u, addr, _v in writes)


def test_disable_other_slots_is_ignored_on_a_program():
    """The firmware applies it only on its single-slot branch."""
    table = [
        {"start": i * 400, "end": 0, "power": 0, "voltage": 0, "soc": 50, "enable": 1}
        for i in range(6)
    ]
    writes = compute_register_writes(
        _charge_recipe(), {"prevSlots": table, "disableOtherSlots": True}, {}
    )
    assert [w for w in writes if 172 <= w[1] <= 177] == [(1, 172 + i, 1) for i in range(6)]


async def test_executor_disables_other_slots_and_reports_them_in_prev_slots():
    regs = {148 + i: i * 400 for i in range(6)}
    regs.update({166 + i: 30 for i in range(6)})
    regs.update({172: 0, 173: 0, 174: 1, 175: 0, 176: 0, 177: 0})
    inv = _Inverter(regs)
    result = await _run(inv, "set_battery_charge", _slot_payload())
    assert inv.registers[174] == 0
    assert inv.registers[172] == 1
    # The restore's table still says slot 3 was charging.
    assert result["prevSlots"][2]["enable"] == 1


# ─────────────────────────── through the command poller ──────────────────────


def _api_command(command: str, payload: dict, admin_priv) -> dict:
    from custom_components.svitgrid.signing import sign_payload

    signed = {"commandId": f"c-{command}", "command": command}
    return {
        "commandId": f"c-{command}",
        "command": command,
        "payload": payload,
        "signature": sign_payload(signed, admin_priv),
        "signingKeyId": "admin",
        "signedEventData": signed,
    }


@pytest.mark.parametrize(
    ("command", "payload", "before", "after"),
    [
        # deactivateSellToGrid, plain
        ("restore_work_mode", {"inverterId": "inv-1", "workMode": 1}, {142: 0}, {142: 1}),
        # deactivateSellToGrid after a floor hold
        (
            "restore_solar_sell",
            {"inverterId": "inv-1", "solarSell": 0, "workMode": 1},
            {142: 1, 145: 1},
            {142: 1, 145: 0},
        ),
        # deactivateUseBattery / disableTouDuringEvent
        (
            "restore_grid_charge_toggle",
            {"inverterId": "inv-1", "gridChargeEnabled": 1},
            {146: 0},
            {146: 1},
        ),
        (
            "restore_sell_power_cap",
            {
                "inverterId": "inv-1",
                "sellPowerCapW": 24000,
                "upperBoundW": 24000,
                "ratedPowerW": 24000,
            },
            {143: 5000},
            {143: 24000},
        ),
    ],
)
async def test_poller_dispatches_the_api_restore_and_acks_success(command, payload, before, after):
    from unittest.mock import AsyncMock

    from custom_components.svitgrid.command_poller import process_command
    from custom_components.svitgrid.signing import generate_keypair

    admin_priv, admin_pub = generate_keypair()
    our_priv, _ = generate_keypair()
    inv = _Inverter(before)
    api = AsyncMock()
    with (
        patch(f"{MODULE}.read_word", inv.read_word),
        patch(f"{MODULE}.read_words", inv.read_words),
        patch(f"{MODULE}.write_registers", inv.write_registers),
    ):
        await process_command(
            command=_api_command(command, payload, admin_priv),
            api_client=api,
            api_key="k",
            trusted_public_keys_hex={"admin": admin_pub},
            our_private_key=our_priv,
            our_signing_key_id="ours",
            executor_version="0.22.7",
            keystore=None,
            executors_by_inverter={"inv-1": _executor()},
        )

    body = api.ack_command.call_args.kwargs["body"]
    assert body["success"] is True, body
    assert {a: inv.registers[a] for a in after} == after
