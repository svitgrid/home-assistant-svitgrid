"""The bench probe's decision logic.

The tool is bench-only, but ONE thing in it must not be wrong: what it
concludes from a write. Reporting "confirmed" for a write the device ignored
would be worse than having no tool, because it would license shipping the
whole settings feature on a false premise.
"""

from custom_components.svitgrid.eybond_at.modbus_rtu import FC_WRITE_MULTIPLE, crc16
from tools.bench_probe import BUZZER_REG, write_probe


class StubSession:
    """Answers reads from a dict and writes according to its own rules."""

    slave_id = 1

    def __init__(self, registers, *, read_only=(), refuse=False, refuse_code=0x01):
        self.registers = dict(registers)
        self.read_only = set(read_only)
        self.refuse = refuse
        # Which of the documented refusals to answer with. 0x01 (read-only) is
        # the default because it is the one that genuinely rules the register
        # out; 0x07 does not, and the tool must not say so.
        self.refuse_code = refuse_code
        self.writes: list[tuple[int, int]] = []

    async def read_registers(self, address, count, timeout_s=5.0):
        return [self.registers.get(address + i, 0) for i in range(count)]

    async def _transact(self, payload, timeout_s):
        # Decode the FC16 REQUEST the tool built. Unlike FC06, a request and
        # its acknowledgement do NOT share a shape here -- the request carries
        # a byte count and data, the ack carries neither -- so this stub has
        # to read the request as a request.
        assert payload[1] == FC_WRITE_MULTIPLE, (
            f"the bench tool must speak FC16; it sent function {payload[1]:#04x}"
        )
        address = int.from_bytes(payload[2:4], "big")
        quantity = int.from_bytes(payload[4:6], "big")
        value = int.from_bytes(payload[7:9], "big")
        self.writes.append((address, value))
        if self.refuse:
            body = bytes([self.slave_id, FC_WRITE_MULTIPLE | 0x80, self.refuse_code])
            crc = crc16(body)
            return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        if address not in self.read_only:
            self.registers[address] = value
        # The device answers with address and QUANTITY, never the value.
        body = bytes([self.slave_id, FC_WRITE_MULTIPLE]) + payload[2:6]
        assert quantity == 1
        return body + crc16(body).to_bytes(2, "little")


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
    session = StubSession({BUZZER_REG: 3}, refuse=True)  # 0x01, read-only
    code = await write_probe(session)
    out = capsys.readouterr().out
    assert "REFUSED by the device" in out
    assert "read-only" in out
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


async def test_the_probe_writes_with_function_code_16(capsys):
    """The bug this tool must not reproduce.

    `bench_probe` is what we use to prove things on the bench, so a tool
    speaking a function code the hardware ignores would produce a confident
    "refused" verdict about a device that simply never heard the question.
    The StubSession asserts the function code on every write.
    """
    session = StubSession({BUZZER_REG: 3})
    code = await write_probe(session)
    assert code == 0


async def test_the_probe_does_not_check_the_ack_against_the_value_written(capsys):
    """An FC16 ack carries the quantity, not the value.

    The probe writes 0 to the buzzer register when it finds a non-zero value.
    A tool comparing the ack's trailing word against 0 would call a perfectly
    good acknowledgement a mismatch.
    """
    session = StubSession({BUZZER_REG: 3})
    await write_probe(session)
    out = capsys.readouterr().out
    assert "WRITE CONFIRMED" in out
    assert session.writes[0] == (BUZZER_REG, 0)


async def test_the_probe_names_the_refusal_reason(capsys):
    """A bench refusal must say WHICH refusal.

    0x07 means the MODE is the obstacle, so the register may well be writable
    -- the opposite conclusion from 0x01. A tool that printed "not writable"
    for both would retire a perfectly good register on the strength of a
    temporary condition.
    """
    session = StubSession({BUZZER_REG: 3}, refuse=True, refuse_code=0x07)
    await write_probe(session)
    out = capsys.readouterr().out.lower()
    assert "mode" in out
    assert "not writable" not in out
