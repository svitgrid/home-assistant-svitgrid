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
from custom_components.svitgrid.executors.smg_settings_executor import (
    EybondSmgSettingsExecutor,
    NoCollectorConnected,
    SmgSettingsExecutor,
)


class FakeLink:
    """A fake CollectorSession: async read_registers/write_register, matching
    `CollectorSession`'s real signature (`timeout_s`, not Dart's `timeout`)."""

    def __init__(self, registers: dict[int, int]):
        self.registers = dict(registers)
        self.writes: list[tuple[int, int]] = []
        self.read_only: set[int] = set()

    async def read_registers(self, address: int, count: int, timeout_s: float = 5.0) -> list[int]:
        """Answers only for addresses this device actually has.

        An address MISSING from `registers` is one the device did not answer
        for, and the block stops there -- a short read, exactly what a
        truncated frame or a dropped connection produces. It is emphatically
        NOT `.get(address + i, 0)`: defaulting an absent register to 0 hands
        the executor a fully-populated snapshot no matter what arrived, which
        is how a truncated read at 24 V was able to let an unvalidated
        protective write through with every test in this file green.
        """
        words: list[int] = []
        for i in range(count):
            value = self.registers.get(address + i)
            if value is None:
                break
            words.append(value)
        return words

    async def write_register(self, address: int, value: int, timeout_s: float = 5.0) -> int:
        self.writes.append((address, value))
        if address not in self.read_only:
            self.registers[address] = value
        return value


# The device answers for its whole configuration block, not only the addresses
# the catalogue names -- `_read_raw` reads contiguous blocks and gets words back
# for the gaps too. The fixtures cover the span so that an address DELETED from
# one means "this word did not arrive", which is the only thing `FakeLink`'s
# short-read semantics should ever be expressing.
_CONFIG_BLOCK_SPAN = range(303, 344)


def bench_registers() -> dict[int, int]:
    """The bench unit's factory profile."""
    space = dict.fromkeys(_CONFIG_BLOCK_SPAN, 0)
    space.update({
        324: 282, 325: 270, 332: 600, 333: 300,
        323: 320, 327: 230, 329: 210,
        341: 20, 342: 30, 343: 15,
        313: 0, 334: 292, 335: 60, 336: 120, 337: 30,
        320: 2300, 321: 5000, 303: 3,
    })
    return space


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
    # Someone changed the bulk voltage at the inverter's own panel -- AFTER we
    # had already read the block once. The order matters: mutating the register
    # before the executor's only dispatch proves `apply` reads at all, which a
    # snapshot memoised on the instance also satisfies. Reading first and
    # mutating afterwards is what proves it RE-reads, and a stale snapshot is
    # how a pack-damaging combination gets approved by validation that ran
    # against numbers the device no longer holds.
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    first = await ex.dispatch("read_inverter_settings", {})
    assert first["settings"]["maxChargeVoltage"] == pytest.approx(28.2, abs=0.001)

    ex.link.registers[324] = 272  # 27.2 V from now on

    result = await ex.dispatch(
        "set_inverter_setting", {"setting": "floatChargeVoltage", "value": 27.5}
    )
    assert result["ok"] is False, "float 27.5 now exceeds bulk 27.2"
    assert ex.link.writes == []


async def test_a_setting_already_at_its_target_is_not_written_again():
    """Pre-read, skip if already there. Not an optimisation: no write on this
    family has ever been confirmed on real hardware, so the cheapest way to be
    safe is to send nothing when nothing needs to change."""
    ex = make_executor(registers=bench_registers(), protocol_number=11, pack_voltage=24)
    result = await ex.dispatch("set_inverter_setting", {"setting": "buzzerMode", "value": 3})
    assert result["ok"] is True
    assert result["readBack"] == 3
    assert ex.link.writes == [], "register 303 already holds 3; nothing should be sent"


async def test_a_truncated_read_refuses_a_protective_write_at_24v():
    """The 24 V table is the MEASURED one, so nothing in it is bounds_derived
    and the unconfirmed group lock has nothing to lock: `unconfirmed` comes
    back empty however little of the block arrived.

    `validate_smg_settings` then SKIPS every constraint whose partner register
    is absent -- correct when validating a partial snapshot, and catastrophic
    when authorising a write, because the skipped comparison is the one
    protecting the pack. Here the bulk voltage never arrives and a float of
    30.0 V, above the bench unit's 28.2 V bulk, sails through `floatBelowBulk`
    without it ever being evaluated.

    Reachable in production: `CollectorSession.read_registers` returns whatever
    word count a well-formed frame carries and never checks it against the
    count requested.
    """
    ex = make_executor(
        link=truncating(bench_registers(), to=3), protocol_number=11, pack_voltage=24
    )
    snapshot = await ex.dispatch("read_inverter_settings", {})
    assert "maxChargeVoltage" not in snapshot["settings"], "the bulk voltage did not arrive"
    assert snapshot["unconfirmed"] == [], "24 V derives no bounds, so nothing locks the group"

    result = await ex.dispatch(
        "set_inverter_setting", {"setting": "floatChargeVoltage", "value": 30.0}
    )
    assert result["ok"] is False, "floatBelowBulk could not be evaluated -- refuse, do not skip"
    assert ex.link.writes == []
    assert "324" in result["message"], "name the register that did not arrive"


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


# ── EybondSmgSettingsExecutor: the class __init__.py actually wires up ────────
#
# The transport-agnostic SmgSettingsExecutor above takes a fixed link and a
# fixed protocol number at construction. The one in production takes neither:
# it is built once at config-entry setup and outlives every collector
# connection, so it re-resolves the session AND re-reads register 184 on every
# dispatch. Gutting its entire dispatch body -- dropping the
# NoCollectorConnected raise and hardcoding the protocol to 11 -- left the full
# suite green, because the only coverage it had asserted isinstance and two
# private attributes.


class FakeHub:
    """Routes by serial, like the real hub (cf. FakeHub in test_eybond_at_harvest.py)."""

    def __init__(self, sessions: dict[str | None, FakeLink]):
        self.sessions = dict(sessions)
        self.asked: list[str | None] = []

    def session_for(self, serial):
        self.asked.append(serial)
        return self.sessions.get(serial)


def _collector_registers(**overrides: int) -> dict[int, int]:
    """A bench unit reachable over a collector: the config block plus the
    protocol number at register 184."""
    return {**bench_registers(), 184: smg_ii_protocol_number, **overrides}


async def test_reads_and_writes_over_the_session_the_hub_holds_for_its_own_serial():
    mine = FakeLink(_collector_registers())
    someone_else = FakeLink(_collector_registers())
    hub = FakeHub({"99432604107106": mine, "00000000000000": someone_else})
    ex = EybondSmgSettingsExecutor(hub=hub, inverter_serial="99432604107106")

    result = await ex.dispatch("read_inverter_settings", {})
    assert hub.asked == ["99432604107106"]
    assert result["protocolNumber"] == smg_ii_protocol_number
    assert result["settings"]["buzzerMode"] == 3

    write = await ex.dispatch("set_inverter_setting", {"setting": "buzzerMode", "value": 0})
    assert write["ok"] is True
    assert mine.writes == [(303, 0)]
    assert someone_else.writes == [], "a write must never land on another inverter's session"


async def test_refuses_every_command_when_no_collector_is_connected():
    """The collector comes and goes. With no live session there is nothing to
    read and nothing to write to, and saying so is the whole point: an executor
    that quietly returned ok would report a setpoint applied to a device it
    never reached."""
    session = FakeLink(_collector_registers())
    hub = FakeHub({"someone-else": session})
    ex = EybondSmgSettingsExecutor(hub=hub, inverter_serial="99432604107106")

    with pytest.raises(NoCollectorConnected):
        await ex.dispatch("read_inverter_settings", {})
    with pytest.raises(NoCollectorConnected):
        await ex.dispatch(
            "set_inverter_setting", {"setting": "maxChargeVoltage", "value": 29.0}
        )
    assert session.writes == []


async def test_refuses_when_the_inverter_has_no_serial_to_resolve():
    ex = EybondSmgSettingsExecutor(hub=FakeHub({}), inverter_serial=None)
    with pytest.raises(NoCollectorConnected):
        await ex.dispatch("read_inverter_settings", {})


async def test_rereads_the_protocol_number_on_every_dispatch():
    """Register 184 selects the register map, and the collector can reconnect
    to a DIFFERENT unit between commands. Trusting the number from the last
    command is how the SMG II map gets applied to registers that mean something
    else -- on a write path whose six DC setpoints are protective."""
    link = FakeLink(_collector_registers())
    hub = FakeHub({"99432604107106": link})
    ex = EybondSmgSettingsExecutor(hub=hub, inverter_serial="99432604107106")

    first = await ex.dispatch("read_inverter_settings", {})
    assert first["protocolNumber"] == smg_ii_protocol_number
    assert first["settings"], "the SMG II catalogue publishes for protocol 11"

    # The collector is now talking to a unit on a different map.
    link.registers[184] = 3

    second = await ex.dispatch("read_inverter_settings", {})
    assert second["protocolNumber"] == 3
    assert second["settings"] == {}, "an unrecognised protocol publishes nothing"

    write = await ex.dispatch(
        "set_inverter_setting", {"setting": "floatChargeVoltage", "value": 27.0}
    )
    assert write["ok"] is False
    assert link.writes == []


async def test_publishes_nothing_when_the_protocol_register_does_not_answer():
    """A short read of register 184 yields no words. Unknown protocol, not
    protocol 11: fail closed."""
    registers = _collector_registers()
    del registers[184]
    link = FakeLink(registers)
    ex = EybondSmgSettingsExecutor(
        hub=FakeHub({"99432604107106": link}), inverter_serial="99432604107106"
    )

    result = await ex.dispatch("read_inverter_settings", {})
    assert result["protocolNumber"] == 0
    assert result["settings"] == {}

    write = await ex.dispatch("set_inverter_setting", {"setting": "buzzerMode", "value": 0})
    assert write["ok"] is False
    assert link.writes == []
