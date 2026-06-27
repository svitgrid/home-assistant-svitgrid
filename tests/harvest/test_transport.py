"""Tests for harvest/transport.py — plan_ranges (pure) + client stubs."""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

from custom_components.svitgrid.harvest.register_spec import RegisterSpec
from custom_components.svitgrid.harvest.transport import plan_ranges


def _spec(reads, protocol="solarman_v5"):
    return RegisterSpec.from_dict({
        "modelId": "m", "version": 1, "protocol": protocol, "port": 8899,
        "defaultSlaveId": 1, "flags": {}, "reads": reads, "derivations": [], "writes": [],
    })


# ---------------------------------------------------------------------------
# plan_ranges — pure logic tests (5 from brief)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# plan_ranges — MAX_RANGE cap: a gap > MAX_RANGE forces a split
# ---------------------------------------------------------------------------

def test_max_range_cap_splits_long_run():
    """A contiguous block > MAX_RANGE (100) must be split."""
    # 101 addresses: 0..100 inclusive — should produce two ranges
    reads = [{"field": f"f{i}", "address": i} for i in range(101)]
    ranges = plan_ranges(_spec(reads))
    assert len(ranges) == 2
    counts = sorted(r[2] for r in ranges)
    assert sum(counts) == 101
    assert all(c <= 100 for c in counts)


# ---------------------------------------------------------------------------
# _read_solarman — happy path via monkeypatched stub
# ---------------------------------------------------------------------------

def test_read_solarman_assembles_raw_registers(monkeypatch):
    """Stub PySolarmanV5; assert RawRegisters maps addr->word correctly."""
    from custom_components.svitgrid.harvest.transport import _read_solarman

    fake_instance = MagicMock()
    # A 2-register range [100, 101] returns [0xAB, 0xCD]
    fake_instance.read_holding_registers.return_value = [0xAB, 0xCD]

    FakeClass = MagicMock(return_value=fake_instance)

    fake_mod = types.ModuleType("pysolarmanv5")
    fake_mod.PySolarmanV5 = FakeClass  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"pysolarmanv5": fake_mod}):
        cfg = {"ip": "192.168.1.1", "logger_serial": "123456789", "port": "8899", "slave_id": "1"}
        ranges = [(1, 100, 2, "FC03")]
        result = _read_solarman(cfg, ranges)

    # Constructor called with ip + serial as positionals
    FakeClass.assert_called_once_with(
        "192.168.1.1",
        123456789,
        port=8899,
        mb_slave_id=1,
        socket_timeout=8,
        auto_reconnect=False,
    )
    # read called with positional-style kwargs
    fake_instance.read_holding_registers.assert_called_once_with(
        register_addr=100, quantity=2
    )
    # Result assembled correctly
    assert result == {1: {100: 0xAB, 101: 0xCD}}
    # disconnect called
    fake_instance.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# _read_modbus — happy path via monkeypatched stub
# ---------------------------------------------------------------------------

def test_read_modbus_assembles_raw_registers(monkeypatch):
    """Stub ModbusTcpClient; assert RawRegisters maps addr->word correctly."""
    from custom_components.svitgrid.harvest.transport import _read_modbus

    fake_result = MagicMock()
    fake_result.isError.return_value = False
    fake_result.registers = [0x11, 0x22]

    fake_client = MagicMock()
    fake_client.read_holding_registers.return_value = fake_result

    FakeClientClass = MagicMock(return_value=fake_client)

    fake_client_mod = types.ModuleType("pymodbus.client")
    fake_client_mod.ModbusTcpClient = FakeClientClass  # type: ignore[attr-defined]

    fake_pymodbus = types.ModuleType("pymodbus")

    with patch.dict(sys.modules, {
        "pymodbus": fake_pymodbus,
        "pymodbus.client": fake_client_mod,
    }):
        cfg = {"ip": "10.0.0.1", "port": "502", "slave_id": "1"}
        ranges = [(247, 784, 2, "FC03")]
        result = _read_modbus(cfg, ranges)

    FakeClientClass.assert_called_once_with("10.0.0.1", port=502, timeout=8)
    fake_client.connect.assert_called_once()
    fake_client.read_holding_registers.assert_called_once_with(
        784, count=2, device_id=247
    )
    assert result == {247: {784: 0x11, 785: 0x22}}
    fake_client.close.assert_called_once()


def test_read_modbus_fc04_uses_input_registers(monkeypatch):
    """FC04 ranges must use read_input_registers."""
    from custom_components.svitgrid.harvest.transport import _read_modbus

    fake_result = MagicMock()
    fake_result.isError.return_value = False
    fake_result.registers = [0x55]

    fake_client = MagicMock()
    fake_client.read_input_registers.return_value = fake_result

    FakeClientClass = MagicMock(return_value=fake_client)

    fake_client_mod = types.ModuleType("pymodbus.client")
    fake_client_mod.ModbusTcpClient = FakeClientClass  # type: ignore[attr-defined]
    fake_pymodbus = types.ModuleType("pymodbus")

    with patch.dict(sys.modules, {
        "pymodbus": fake_pymodbus,
        "pymodbus.client": fake_client_mod,
    }):
        cfg = {"ip": "10.0.0.1", "port": "502", "slave_id": "1"}
        ranges = [(1, 10, 1, "FC04")]
        result = _read_modbus(cfg, ranges)

    fake_client.read_input_registers.assert_called_once_with(10, count=1, device_id=1)
    fake_client.read_holding_registers.assert_not_called()
    assert result == {1: {10: 0x55}}
