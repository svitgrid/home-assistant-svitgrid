"""Tests for the SMG II settings executor.

Mirrors `packages/inverter_protocol/test/protocol/eybond_at/smg_settings_executor_test.dart`.
Everything here runs against a `FakeLink` -- no real collector traffic -- and
exercises the contract shapes plus the three safety properties the brief
calls out: a cross-field constraint whose two addresses are not both present
is skipped, an unrecognised protocol publishes nothing, and a pack voltage
with no bounds table still publishes the twelve pack-independent settings
(the last two are covered by test_smg_settings.py; this file adds the
executor-level consequences: an unrecognised protocol makes read/apply see
an empty catalogue, and a group-unconfirmed setting still lets an unrelated
pack-independent write through).
"""

from __future__ import annotations

import pytest

from custom_components.svitgrid.eybond_at.smg_settings import smg_ii_protocol_number
from custom_components.svitgrid.executors.smg_settings_executor import SmgSettingsExecutor


class FakeLink:
    """A fake CollectorSession: async read_registers/write_register, matching
    `CollectorSession`'s real signature (`timeout_s`, not Dart's `timeout`)."""

    def __init__(self, registers: dict[int, int]):
        self.registers = dict(registers)
        self.writes: list[tuple[int, int]] = []
        self.read_only: set[int] = set()

    async def read_registers(self, address: int, count: int, timeout_s: float = 5.0) -> list[int]:
        return [self.registers.get(address + i, 0) for i in range(count)]

    async def write_register(self, address: int, value: int, timeout_s: float = 5.0) -> int:
        self.writes.append((address, value))
        if address not in self.read_only:
            self.registers[address] = value
        return value


def bench_registers() -> dict[int, int]:
    """The bench unit's factory profile."""
    return {
        324: 282, 325: 270, 332: 600, 333: 300,
        323: 320, 327: 230, 329: 210,
        341: 20, 342: 30, 343: 15,
        313: 0, 334: 292, 335: 60, 336: 120, 337: 30,
        320: 2300, 321: 5000, 303: 3,
    }


def bench_48v() -> dict[int, int]:
    """The bench profile with the six DC setpoints doubled -- a plausible 48 V unit."""
    r = bench_registers()
    for a in (324, 325, 323, 327, 329, 334):
        r[a] = r[a] * 2
    return r


def truncating(registers: dict[int, int], *, to: int) -> "TruncatingLink":
    return TruncatingLink(registers, keep=to)


class TruncatingLink(FakeLink):
    """Answers every block with at most `keep` words -- fewer than requested,
    the way a dropped connection or a truncated Modbus response would."""

    def __init__(self, registers: dict[int, int], *, keep: int):
        super().__init__(registers)
        self.keep = keep

    async def read_registers(self, address: int, count: int, timeout_s: float = 5.0) -> list[int]:
        full = await super().read_registers(address, count, timeout_s=timeout_s)
        return full[: self.keep]


class CountingLink(FakeLink):
    def __init__(self, registers: dict[int, int]):
        super().__init__(registers)
        self.reads: list[int] = []

    async def read_registers(self, address: int, count: int, timeout_s: float = 5.0) -> list[int]:
        self.reads.append(address)
        return await super().read_registers(address, count, timeout_s=timeout_s)


class ExecutorHandle:
    """Bundles the executor with the link it was built on, so tests can
    inspect `.link.writes` the way the brief's pseudocode does (`ex.link.writes`)."""

    def __init__(self, executor: SmgSettingsExecutor, link: FakeLink):
        self.executor = executor
        self.link = link

    async def dispatch(self, command_name: str, payload: dict) -> dict:
        return await self.executor.dispatch(command_name, payload)


def make_executor(
    *,
    registers: dict[int, int] | None = None,
    link: FakeLink | None = None,
    read_only: set[int] | None = None,
    protocol_number: int = smg_ii_protocol_number,
    pack_voltage: int = 24,
) -> ExecutorHandle:
    if link is None:
        link = FakeLink(registers or {})
    if read_only:
        link.read_only |= set(read_only)
    executor = SmgSettingsExecutor(
        link=link,
        protocol_number=protocol_number,
        nominal_pack_voltage=pack_voltage,
    )
    return ExecutorHandle(executor, link)


# ── contract shape ──────────────────────────────────────────────────────

async def test_read_returns_the_contract_shape():
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    result = await ex.dispatch("read_inverter_settings", {})
    assert result["protocolNumber"] == 11
    assert result["settings"]["buzzerMode"] == 3
    assert result["unconfirmed"] == []


async def test_set_reports_the_value_the_device_holds():
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    result = await ex.dispatch("set_inverter_setting", {"setting": "buzzerMode", "value": 0})
    assert result["ok"] is True
    assert result["readBack"] == 0


async def test_a_write_that_echoes_but_does_not_stick_reports_failure():
    ex = make_executor(
        registers=bench_registers(), read_only={303}, protocol_number=11, pack_voltage=24
    )
    result = await ex.dispatch("set_inverter_setting", {"setting": "buzzerMode", "value": 0})
    assert result["ok"] is False
    assert result["message"]


async def test_an_out_of_range_value_never_reaches_the_device():
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    result = await ex.dispatch(
        "set_inverter_setting", {"setting": "maxChargeVoltage", "value": 99.0}
    )
    assert result["ok"] is False
    assert ex.link.writes == []


async def test_a_pack_damaging_combination_is_refused():
    # float above bulk means the pack never leaves absorption
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    result = await ex.dispatch(
        "set_inverter_setting", {"setting": "floatChargeVoltage", "value": 29.0}
    )
    assert result["ok"] is False
    assert "floatBelowBulk" in result["message"]
    assert ex.link.writes == []


async def test_a_contradicted_derived_bound_locks_the_whole_group():
    # 324 at 48 V derives to 480..640; a unit holding 700 falsifies the
    # derivation the six share, so none of them may be written.
    regs = bench_48v()
    regs[324] = 700
    ex = make_executor(registers=regs, protocol_number=11, pack_voltage=48)
    result = await ex.dispatch("read_inverter_settings", {})
    assert set(result["unconfirmed"]) == {
        "maxChargeVoltage", "floatChargeVoltage", "batteryOverVoltage",
        "lowVoltageCutoffOnMains", "lowVoltageCutoffOffGrid", "equalizationVoltage",
    }
    w = await ex.dispatch(
        "set_inverter_setting", {"setting": "floatChargeVoltage", "value": 54.0}
    )
    assert w["ok"] is False
    assert ex.link.writes == []
    # ...but a pack-independent setting on the same device stays writable
    b = await ex.dispatch("set_inverter_setting", {"setting": "buzzerMode", "value": 0})
    assert b["ok"] is True


async def test_a_short_read_fails_closed():
    ex = make_executor(
        link=truncating(bench_48v(), to=10), protocol_number=11, pack_voltage=48
    )
    result = await ex.dispatch("read_inverter_settings", {})
    assert len(result["unconfirmed"]) == 6


# ── additional coverage mirroring the Dart suite ──────────────────────────

async def test_readall_reports_every_catalogued_setting_in_display_units():
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    result = await ex.dispatch("read_inverter_settings", {})
    settings = result["settings"]
    assert settings["maxChargeVoltage"] == pytest.approx(28.2, abs=0.001)
    assert settings["floatChargeVoltage"] == pytest.approx(27.0, abs=0.001)
    assert settings["socBackToUtility"] == 20
    assert settings["outputFrequency"] == pytest.approx(50.0, abs=0.001)


async def test_reads_in_blocks_rather_than_one_register_at_a_time():
    # One request is in flight at a time on this transport and there is no
    # transaction id, so each round trip costs a full turnaround. Eighteen
    # separate reads is a visibly slow screen; a handful of blocks is not.
    link = CountingLink(bench_registers())
    ex = make_executor(link=link, protocol_number=11, pack_voltage=24)
    await ex.dispatch("read_inverter_settings", {})
    assert len(link.reads) < 8, f"expected blocked reads, got {len(link.reads)} round trips"


async def test_publishes_the_pack_independent_settings_for_a_48v_pack_too():
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=48)
    result = await ex.dispatch("read_inverter_settings", {})
    assert result["settings"]
    assert "maxChargeVoltage" in result["settings"], "48 V has a derived bounds table"
    assert result["settings"]["socBackToUtility"] == 20


async def test_writes_a_valid_change_and_verifies_it():
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    result = await ex.dispatch(
        "set_inverter_setting", {"setting": "maxChargeVoltage", "value": 29.0}
    )
    assert result["ok"] is True
    assert ex.link.writes == [(324, 290)]
    assert ex.link.registers[324] == 290


async def test_refuses_a_value_outside_the_published_range_without_writing():
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    result = await ex.dispatch(
        "set_inverter_setting", {"setting": "maxChargeVoltage", "value": 40.0}
    )
    assert result["ok"] is False
    assert "range" in result["message"]
    assert ex.link.writes == []


async def test_refuses_an_unknown_setting_key():
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    result = await ex.dispatch("set_inverter_setting", {"setting": "turboMode", "value": 1})
    assert result["ok"] is False
    assert ex.link.writes == []


async def test_validates_against_what_the_device_holds_not_a_cached_copy():
    # Someone changed the bulk voltage at the inverter's own panel. A cached
    # value would let a float through that is now above it.
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    ex.link.registers[324] = 272  # 27.2 V
    result = await ex.dispatch(
        "set_inverter_setting", {"setting": "floatChargeVoltage", "value": 27.5}
    )
    assert result["ok"] is False, "float 27.5 now exceeds bulk 27.2"
    assert ex.link.writes == []


async def test_measured_24v_bounds_are_never_treated_as_unconfirmed():
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    result = await ex.dispatch("read_inverter_settings", {})
    assert result["unconfirmed"] == []


async def test_unrecognised_protocol_publishes_nothing():
    ex = make_executor(registers=bench_registers(), protocol_number=3, pack_voltage=24)
    result = await ex.dispatch("read_inverter_settings", {})
    assert result["settings"] == {}
    assert result["unconfirmed"] == []
    write = await ex.dispatch(
        "set_inverter_setting", {"setting": "buzzerMode", "value": 0}
    )
    assert write["ok"] is False
    assert ex.link.writes == []
