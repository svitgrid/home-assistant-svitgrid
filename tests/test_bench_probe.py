"""The bench probe's decision logic.

The tool is bench-only, but ONE thing in it must not be wrong: what it
concludes from a write. Reporting "confirmed" for a write the device ignored
would be worse than having no tool, because it would license shipping the
whole settings feature on a false premise.
"""

import pytest

from custom_components.svitgrid.eybond_at.modbus_rtu import (
    ModbusExceptionError,
    build_write_single,
    crc16,
    parse_write_response,
)
from tools.bench_probe import BUZZER_REG, write_probe


class StubSession:
    """Answers reads from a dict and writes according to its own rules."""

    slave_id = 1

    def __init__(self, registers, *, read_only=(), refuse=False):
        self.registers = dict(registers)
        self.read_only = set(read_only)
        self.refuse = refuse
        self.writes: list[tuple[int, int]] = []

    async def read_registers(self, address, count, timeout_s=5.0):
        return [self.registers.get(address + i, 0) for i in range(count)]

    async def _transact(self, payload, timeout_s):
        address, value = parse_write_response(payload)  # request and echo share a shape
        self.writes.append((address, value))
        if self.refuse:
            body = bytes([self.slave_id, 0x86, 0x02])
            crc = crc16(body)
            return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        if address not in self.read_only:
            self.registers[address] = value
        return build_write_single(self.slave_id, address, value)


async def test_reports_confirmed_when_the_value_sticks(capsys):
    session = StubSession({BUZZER_REG: 3})
    code = await write_probe(session)
    out = capsys.readouterr().out
    assert "WRITE CONFIRMED" in out
    assert code == 0


async def test_restores_the_original_value(capsys):
    session = StubSession({BUZZER_REG: 3})
    await write_probe(session)
    assert session.registers[BUZZER_REG] == 3, "the bench must be left as it was found"
    assert "(restored)" in capsys.readouterr().out


async def test_detects_a_device_that_echoes_but_does_not_store(capsys):
    # The failure mode the whole read-back exists for. A conforming device
    # echoes the request whether or not it honoured it.
    session = StubSession({BUZZER_REG: 3}, read_only={BUZZER_REG})
    code = await write_probe(session)
    out = capsys.readouterr().out
    assert "ECHOED BUT DID NOT STICK" in out
    assert "WRITE CONFIRMED" not in out
    assert code == 4


async def test_reports_a_modbus_refusal_distinctly(capsys):
    session = StubSession({BUZZER_REG: 3}, refuse=True)
    code = await write_probe(session)
    out = capsys.readouterr().out
    assert "REFUSED at the Modbus layer" in out
    assert "Option C is not available" in out
    assert code == 2


async def test_writes_a_different_value_than_it_found(capsys):
    # Writing back the same value would prove nothing at all.
    session = StubSession({BUZZER_REG: 0})
    await write_probe(session)
    first_write = session.writes[0]
    assert first_write[0] == BUZZER_REG
    assert first_write[1] != 0


async def test_touches_only_the_buzzer_register():
    session = StubSession({BUZZER_REG: 3, 324: 282})
    await write_probe(session)
    assert {addr for addr, _ in session.writes} == {BUZZER_REG}
    assert session.registers[324] == 282
